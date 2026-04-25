"""SessionStore adapter that streams the tick transcript to ``system/logs/tick-NNN.jsonl``.

The SDK still writes its own copy under ``~/.claude/projects/.../session.jsonl`` —
this adapter is a parallel mirror that lands in the data repo as the tick
runs, so external tooling never has to reach into ``~/.claude``.

Each tick gets a fresh adapter bound to its tick file. Entries arrive in
batches (~100ms cadence during active turns); we serialize them to JSONL
in append order. ``load`` / ``list_sessions`` etc. are unimplemented since
the kernel runs stateless ticks (no resume).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from claude_agent_sdk.types import (
    SessionKey,
    SessionListSubkeysKey,
    SessionStoreEntry,
    SessionStoreListEntry,
    SessionSummaryEntry,
)


class TickJsonlStore:
    """SessionStore that writes the per-tick transcript to a single JSONL file.

    Conforms to the ``SessionStore`` Protocol in ``claude_agent_sdk.types``.
    The kernel runs stateless ticks (no resume, no listing), so only
    ``append`` does real work — every other method raises
    ``NotImplementedError`` to match the SDK's documented default-body
    contract.
    """

    def __init__(self, tick_path: Path) -> None:
        self.path = tick_path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    async def append(self, key: SessionKey, entries: list[SessionStoreEntry]) -> None:
        if not entries:
            return
        # to_thread avoids blocking the event loop on disk I/O. Batches are
        # small (≤500 entries / ≤1 MiB) so the work itself is microseconds.
        await asyncio.to_thread(self._write_sync, entries)

    def _write_sync(self, entries: list[SessionStoreEntry]) -> None:
        # Compact, UTF-8-clean encoding to match the CLI's own JSONL format
        # under ~/.claude/projects/ — keeps the two files byte-comparable for
        # entries that flow through the mirror.
        with self.path.open("a", encoding="utf-8") as f:
            for entry in entries:
                f.write(json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n")

    async def load(self, key: SessionKey) -> list[SessionStoreEntry] | None:
        # Stateless ticks never resume.
        return None

    async def list_sessions(self, project_key: str) -> list[SessionStoreListEntry]:
        raise NotImplementedError

    async def list_session_summaries(self, project_key: str) -> list[SessionSummaryEntry]:
        raise NotImplementedError

    async def delete(self, key: SessionKey) -> None:
        raise NotImplementedError

    async def list_subkeys(self, key: SessionListSubkeysKey) -> list[str]:
        raise NotImplementedError
