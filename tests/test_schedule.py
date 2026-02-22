"""Tests for wake scheduling."""

from datetime import datetime, timedelta

import src.config as config
import src.registry as registry
from src.tools.schedule import (
    _load_schedule,
    _save_schedule,
    cleanup_old_wakes,
    get_pending_wakes,
    mark_wake_fulfilled,
)


def _setup(tmp_path):
    """Register a temp dir and init config for schedule tests."""
    (tmp_path / "system").mkdir(parents=True, exist_ok=True)
    registry.register("test", tmp_path)
    config.init("test")


class TestSchedule:
    def test_no_pending_wakes(self, tmp_path, tmp_registry):
        _setup(tmp_path)
        assert get_pending_wakes() == []

    def test_pending_wake_due(self, tmp_path, tmp_registry):
        _setup(tmp_path)
        past = (datetime.now() - timedelta(minutes=5)).isoformat()
        _save_schedule({"wakes": [{"time": past, "reason": "test"}]})
        due = get_pending_wakes()
        assert len(due) == 1
        assert due[0]["reason"] == "test"

    def test_pending_wake_future(self, tmp_path, tmp_registry):
        _setup(tmp_path)
        future = (datetime.now() + timedelta(hours=1)).isoformat()
        _save_schedule({"wakes": [{"time": future, "reason": "later"}]})
        assert get_pending_wakes() == []

    def test_mark_wake_fulfilled(self, tmp_path, tmp_registry):
        _setup(tmp_path)
        wake_time = (datetime.now() - timedelta(minutes=1)).isoformat()
        _save_schedule({"wakes": [{"time": wake_time, "reason": "test"}]})
        mark_wake_fulfilled(wake_time)
        schedule = _load_schedule()
        assert schedule["wakes"][0]["fulfilled"] is True
        assert "fulfilled_at" in schedule["wakes"][0]

    def test_fulfilled_wake_not_pending(self, tmp_path, tmp_registry):
        _setup(tmp_path)
        past = (datetime.now() - timedelta(minutes=5)).isoformat()
        _save_schedule(
            {"wakes": [{"time": past, "reason": "done", "fulfilled": True, "fulfilled_at": datetime.now().isoformat()}]}
        )
        assert get_pending_wakes() == []

    def test_cleanup_old_wakes(self, tmp_path, tmp_registry):
        _setup(tmp_path)
        old_time = (datetime.now() - timedelta(hours=25)).isoformat()
        recent_time = (datetime.now() - timedelta(hours=1)).isoformat()
        future_time = (datetime.now() + timedelta(hours=1)).isoformat()
        _save_schedule(
            {
                "wakes": [
                    {"time": old_time, "fulfilled": True, "fulfilled_at": old_time},
                    {"time": recent_time, "fulfilled": True, "fulfilled_at": recent_time},
                    {"time": future_time, "reason": "upcoming"},
                ]
            }
        )
        cleanup_old_wakes()
        schedule = _load_schedule()
        # Old fulfilled wake removed, recent fulfilled and unfulfilled kept
        assert len(schedule["wakes"]) == 2
