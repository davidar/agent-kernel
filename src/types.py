"""Type definitions for the agent."""

from dataclasses import asdict, dataclass
import json
from pathlib import Path
from typing_extensions import Self


@dataclass
class State:
    """Agent state persisted across ticks."""

    tick_count: int = 0
    last_tick: str | None = None  # When the last tick started
    last_tick_end: str | None = None  # When the last tick ended
    first_tick_date: str | None = None  # When tick 1 happened

    def to_json(self) -> str:
        """Serialize to JSON string."""
        return json.dumps(asdict(self), indent=2)

    @classmethod
    def from_json(cls, data: str) -> Self:
        """Deserialize from JSON string, handling missing fields gracefully."""
        return cls.from_dict(json.loads(data))

    @classmethod
    def from_dict(cls, data: dict) -> Self:
        """Create from dict, handling missing fields gracefully."""
        return cls(
            tick_count=data.get("tick_count", 0),
            last_tick=data.get("last_tick"),
            last_tick_end=data.get("last_tick_end"),
            first_tick_date=data.get("first_tick_date"),
        )

    @classmethod
    def load(cls, path: Path) -> Self:
        """Load state from a JSON file."""
        if path.exists():
            return cls.from_json(path.read_text())
        return cls()

    def save(self, path: Path) -> None:
        """Save state to a JSON file."""
        path.write_text(self.to_json())
