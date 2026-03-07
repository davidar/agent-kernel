"""Watcher — polls for trigger files and runs agent ticks.

This is the system's heartbeat: watches for trigger files, runs ticks,
and manages crash notifications.
"""

import asyncio
import json
import signal
import sys
import time
import traceback

from .agent import main as run_agent
from .config import data_dir, ensure_dirs, get_agent_config, get_container_name, get_state
from .container import ensure_ready
from .hooks import run_hooks
from .logging_config import setup_process_logging, get_logger
from .notifications import send_crash_notification

logger = get_logger(__name__)


def _recover_crashed_tick() -> None:
    """If a previous tick crashed mid-way (live_status.json left behind), run post-tick hooks.

    Must be called BEFORE ensure_dirs() since that wipes tmp/.
    """
    live_status_file = data_dir() / "tmp" / "live_status.json"
    if not live_status_file.exists():
        return

    try:
        status_data = json.loads(live_status_file.read_text())
    except (OSError, json.JSONDecodeError):
        logger.warning("Stale live_status.json found but unreadable, skipping recovery")
        return

    tick = status_data.get("tick")
    logger.warning("Detected crashed tick %s (stale live_status.json) — running post-tick hooks", tick)

    agent_config = get_agent_config()
    hook_env_prefix = agent_config.get("hook_env_prefix", "AGENT")
    container_name = get_container_name()

    try:
        asyncio.run(ensure_ready())
    except Exception as e:
        logger.error("Container startup failed during recovery: %s", e)
        return

    env = {
        f"{hook_env_prefix}_TICK": str(tick or 0),
        f"{hook_env_prefix}_TICK_DURATION": "",
        f"{hook_env_prefix}_TICK_LOG": "",
        f"{hook_env_prefix}_LAST_MESSAGE": "",
        f"{hook_env_prefix}_SESSION_ID": "",
        f"{hook_env_prefix}_TICK_STATUS": "abnormal",
    }

    try:
        asyncio.run(run_hooks("post-tick", env, container=container_name))
    except Exception as e:
        logger.error("Post-tick recovery hooks failed: %s", e)


def run_watcher(poll_interval: float = 2.0) -> None:
    """Main watcher loop — poll for triggers, run ticks."""
    setup_process_logging("watcher")

    # Check for crashed tick before ensure_dirs() wipes tmp/
    _recover_crashed_tick()

    ensure_dirs()

    # Start container immediately so daemons can run before first tick
    try:
        asyncio.run(ensure_ready())
        logger.info(f"Container {get_container_name()} ready")
    except Exception as e:
        logger.error(f"Container startup failed: {e}")

    trigger_file = data_dir() / "system" / "tick_trigger"

    # Graceful shutdown
    running = True

    def handle_signal(_signum, _frame):
        nonlocal running
        logger.info("Shutting down watcher...")
        running = False
        # Exit immediately — SystemExit propagates through asyncio.run(),
        # finally blocks still execute for cleanup.
        sys.exit(0)

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    state = get_state()
    logger.info("=== Agent Watcher ===")
    logger.info(f"Tick count: {state.tick_count}")
    logger.info(f"Watching for triggers (poll every {poll_interval}s)")
    logger.info("Press Ctrl+C to stop")

    while running:
        try:
            # Check for trigger file
            trigger = trigger_file.exists()
            trigger_reason = None
            if trigger:
                try:
                    trigger_reason = trigger_file.read_text().strip()
                except OSError:
                    pass
                trigger_file.unlink(missing_ok=True)

            if trigger:
                logger.info(f"Tick triggered: {trigger_reason or 'manual'}")
                logger.info("Starting tick...")
                try:
                    run_agent()
                except Exception as e:
                    error_text = f"{type(e).__name__}: {e}\n{traceback.format_exc()}"
                    logger.error(f"Tick error: {error_text}")
                    send_crash_notification(error_text)

                logger.info("Waiting for triggers...")

            time.sleep(poll_interval)

        except Exception as e:
            error_text = f"Watch error: {type(e).__name__}: {e}\n{traceback.format_exc()}"
            logger.error(error_text)
            send_crash_notification(error_text)
            time.sleep(poll_interval)

    logger.info("Watcher stopped.")
