"""Tests for State dataclass."""

from src.types import State


class TestStateDefaults:
    def test_defaults(self):
        s = State()
        assert s.tick_count == 0
        assert s.last_tick is None
        assert s.last_tick_end is None
        assert s.first_tick_date is None


class TestStateJsonRoundtrip:
    def test_roundtrip(self):
        s = State(
            tick_count=42,
            last_tick="2025-01-01T00:00:00",
            last_tick_end="2025-01-01T01:00:00",
            first_tick_date="2025-01-01",
        )
        json_str = s.to_json()
        restored = State.from_json(json_str)
        assert restored.tick_count == 42
        assert restored.last_tick == "2025-01-01T00:00:00"
        assert restored.first_tick_date == "2025-01-01"


class TestStateFromDict:
    def test_missing_fields_default(self):
        s = State.from_dict({"tick_count": 5})
        assert s.tick_count == 5
        assert s.last_tick is None

    def test_empty_dict(self):
        s = State.from_dict({})
        assert s.tick_count == 0


class TestStatePersistence:
    def test_load_nonexistent(self, tmp_path):
        s = State.load(tmp_path / "nonexistent.json")
        assert s.tick_count == 0

    def test_save_and_load(self, tmp_path):
        path = tmp_path / "state.json"
        original = State(tick_count=7, first_tick_date="2025-06-15")
        original.save(path)
        loaded = State.load(path)
        assert loaded.tick_count == 7
        assert loaded.first_tick_date == "2025-06-15"
