"""Crash / failure notifications — file-based, for external consumers (e.g. Discord)."""

import hashlib
import json
from datetime import datetime

from .config import data_dir
from .logging_config import get_logger

logger = get_logger(__name__)


def send_crash_notification(error: str) -> None:
    """Write crash notification for external consumers to send.

    Rate-limited: same error hash within 30 minutes is suppressed.
    """
    now = datetime.now()

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
