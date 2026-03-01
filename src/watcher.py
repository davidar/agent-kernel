"""Watcher — polls for trigger files and runs agent ticks.

This is the system's heartbeat: watches for trigger files, runs ticks,
and manages crash notifications.
"""

import asyncio
import shutil
import signal
import sys
import time
import traceback

from .agent import main as run_agent
from .config import data_dir, ensure_dirs, get_container_name, get_state
from .container import ensure_ready
from .logging_config import setup_process_logging, get_logger
from .notifications import send_crash_notification

logger = get_logger(__name__)


def run_watcher(poll_interval: float = 2.0) -> None:
    """Main watcher loop — poll for triggers, run ticks."""
    setup_process_logging("watcher")
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

    # Clean up stale tmp/ from a previous crashed tick
    tmp_dir = data_dir() / "tmp"
    if tmp_dir.exists():
        logger.info("Cleaning up stale tmp/")
        shutil.rmtree(tmp_dir, ignore_errors=True)

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
