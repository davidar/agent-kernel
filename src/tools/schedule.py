"""Schedule helpers — reads schedule.json for the watcher and agent.

Wake scheduling is done via the `wake` CLI in the container. These helpers
are host-side code that reads schedule state for the watcher/agent harness.
"""

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from ..config import data_dir


def _schedule_file() -> Path:
    return data_dir() / "system" / "schedule.json"


def _load_schedule() -> dict[str, Any]:
    """Load schedule from disk."""
    f = _schedule_file()
    if not f.exists():
        return {"wakes": []}
    try:
        return json.loads(f.read_text())
    except (OSError, json.JSONDecodeError):
        return {"wakes": []}


def _save_schedule(schedule: dict[str, Any]) -> None:
    """Save schedule to disk."""
    f = _schedule_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(schedule, indent=2))


def get_pending_wakes() -> list[dict[str, Any]]:
    """Get wakes that are due (for watcher to check)."""
    schedule = _load_schedule()
    now = datetime.now()
    due = []

    for wake in schedule.get("wakes", []):
        if wake.get("fulfilled"):
            continue
        try:
            wake_time = datetime.fromisoformat(wake["time"])
            if wake_time <= now:
                due.append(wake)
        except (KeyError, ValueError):
            continue

    return due


def mark_wake_fulfilled(wake_time: str) -> None:
    """Mark a wake as fulfilled."""
    schedule = _load_schedule()
    for wake in schedule.get("wakes", []):
        if wake.get("time") == wake_time:
            wake["fulfilled"] = True
            wake["fulfilled_at"] = datetime.now().isoformat()
    _save_schedule(schedule)


def cleanup_old_wakes() -> None:
    """Remove fulfilled wakes older than 24 hours."""
    schedule = _load_schedule()
    now = datetime.now()
    cutoff = now - timedelta(hours=24)

    schedule["wakes"] = [
        w
        for w in schedule.get("wakes", [])
        if not w.get("fulfilled") or datetime.fromisoformat(w.get("fulfilled_at", now.isoformat())) > cutoff
    ]
    _save_schedule(schedule)
