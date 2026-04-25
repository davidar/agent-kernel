"""Tests for the TickJsonlStore SessionStore adapter."""

import json
from pathlib import Path

import pytest

from src.session_store import TickJsonlStore


@pytest.fixture
def tick_path(tmp_path: Path) -> Path:
    return tmp_path / "logs" / "tick-001.jsonl"


async def test_append_writes_jsonl_lines(tick_path: Path):
    store = TickJsonlStore(tick_path)
    entries = [
        {"type": "user", "uuid": "a", "timestamp": "t1", "content": "hello"},
        {"type": "assistant", "uuid": "b", "timestamp": "t2", "content": "world"},
    ]
    await store.append({"project_key": "p", "session_id": "s"}, entries)

    lines = tick_path.read_text().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0]) == entries[0]
    assert json.loads(lines[1]) == entries[1]


async def test_append_is_additive_across_calls(tick_path: Path):
    store = TickJsonlStore(tick_path)
    await store.append(
        {"project_key": "p", "session_id": "s"},
        [{"type": "user", "uuid": "a", "timestamp": "t1"}],
    )
    await store.append(
        {"project_key": "p", "session_id": "s"},
        [{"type": "assistant", "uuid": "b", "timestamp": "t2"}],
    )

    lines = tick_path.read_text().splitlines()
    assert [json.loads(line)["uuid"] for line in lines] == ["a", "b"]


async def test_append_empty_batch_is_noop(tick_path: Path):
    store = TickJsonlStore(tick_path)
    await store.append({"project_key": "p", "session_id": "s"}, [])
    # Empty batch must not even create the file — phantom-key avoidance.
    assert not tick_path.exists()


async def test_constructor_creates_parent_directory(tmp_path: Path):
    nested = tmp_path / "deep" / "nested" / "tick-001.jsonl"
    TickJsonlStore(nested)
    assert nested.parent.is_dir()


async def test_load_returns_none(tick_path: Path):
    store = TickJsonlStore(tick_path)
    result = await store.load({"project_key": "p", "session_id": "s"})
    assert result is None


async def test_optional_methods_raise(tick_path: Path):
    store = TickJsonlStore(tick_path)
    with pytest.raises(NotImplementedError):
        await store.list_sessions("p")
    with pytest.raises(NotImplementedError):
        await store.list_session_summaries("p")
    with pytest.raises(NotImplementedError):
        await store.delete({"project_key": "p", "session_id": "s"})
    with pytest.raises(NotImplementedError):
        await store.list_subkeys({"project_key": "p", "session_id": "s"})
