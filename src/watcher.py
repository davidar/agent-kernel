"""Watcher — polls for triggers and runs agent ticks.

This is the system's heartbeat: watches for trigger files and schedule
wakes, runs ticks, and manages crash notifications.
"""

import asyncio
import hashlib
import json
import shutil
import signal
import sys
import time
import traceback
from datetime import datetime

from .agent import main as run_agent
from .config import data_dir, ensure_dirs, get_container_name, get_state
from .container import ensure_ready
from .logging_config import setup_process_logging, get_logger
from .tools.schedule import get_pending_wakes, mark_wake_fulfilled, cleanup_old_wakes

logger = get_logger(__name__)


def send_crash_notification(error: str) -> None:
    """Write crash notification for external consumers to send.

    Rate-limited: same error hash within 30 minutes is suppressed.
    """
    now = datetime.now()

    # Load crash state for rate limiting
    crash_state_file = data_dir() / "system" / "crash_state.json"
    crash_notify_file = data_dir() / "system" / "crash_notify.txt"

    state: dict = {"last_notify": None, "error_hash": None}
    if crash_state_file.exists():
        try:
            state = json.loads(crash_state_file.read_text())
        except (json.JSONDecodeError, OSError):
            pass

    error_hash = hashlib.md5(error.encode()).hexdigest()[:8]
    last_notify = state.get("last_notify")
    if state.get("error_hash") == error_hash and isinstance(last_notify, str):
        try:
            last = datetime.fromisoformat(last_notify)
            if (now - last).total_seconds() < 1800:
                logger.debug("Crash notification suppressed (duplicate within 30m)")
                return
        except (ValueError, TypeError):
            pass

    crash_notify_file.parent.mkdir(parents=True, exist_ok=True)
    crash_notify_file.write_text(error[:1500])
    logger.info("Crash notification written")

    crash_state_file.write_text(json.dumps({"last_notify": now.isoformat(), "error_hash": error_hash}))


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

    pause_logged = False

    while running:
        try:
            # Check for manual trigger file
            manual_trigger = trigger_file.exists()
            trigger_reason = None
            if manual_trigger:
                try:
                    trigger_reason = trigger_file.read_text().strip()
                except OSError:
                    pass
                trigger_file.unlink(missing_ok=True)

            # Check scheduled wakes
            pending_wakes = get_pending_wakes()
            scheduled_wake = None
            if pending_wakes:
                scheduled_wake = pending_wakes[0]
                mark_wake_fulfilled(scheduled_wake["time"])
                cleanup_old_wakes()

            # Pause file prevents crash loops
            pause_file = data_dir() / "system" / "paused"
            if pause_file.exists():
                if not pause_logged:
                    logger.warning(f"Paused due to fatal error. Delete {pause_file} to resume.")
                    pause_logged = True
                time.sleep(poll_interval)
                continue
            else:
                pause_logged = False

            if manual_trigger or scheduled_wake:
                if manual_trigger:
                    logger.info(f"Tick triggered: {trigger_reason or 'manual'}")
                elif scheduled_wake:
                    reason = scheduled_wake.get("reason", "No reason")
                    logger.info(f"Scheduled wake: {reason}")

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
