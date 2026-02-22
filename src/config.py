"""Configuration and state helpers — safe to import from anywhere.

Call init(data_dir) once at startup before accessing any paths.
"""

import json
import shutil
from pathlib import Path
from typing import Any

from .types import State


_data_dir: Path | None = None


def init(data_dir: Path) -> None:
    """Set the data directory. Must be called before any other config access."""
    global _data_dir
    _data_dir = data_dir


def data_dir() -> Path:
    """Get the data directory. Raises if init() hasn't been called."""
    if _data_dir is None:
        raise RuntimeError("config.init() not called")
    return _data_dir


def ensure_dirs() -> None:
    """Ensure all required directories exist."""
    dd = data_dir()
    (dd / "notes").mkdir(parents=True, exist_ok=True)
    (dd / "system").mkdir(parents=True, exist_ok=True)
    (dd / "system" / "notifications").mkdir(parents=True, exist_ok=True)
    (dd / "sandbox").mkdir(parents=True, exist_ok=True)

    # Wipe and recreate tmp/ — catches stale state from crashed ticks
    tmp_dir = dd / "tmp"
    if tmp_dir.exists():
        shutil.rmtree(tmp_dir, ignore_errors=True)
    tmp_dir.mkdir(parents=True, exist_ok=True)


def get_state() -> State:
    """Load agent state from disk."""
    return State.load(data_dir() / "system" / "state.json")


def save_state(state: State) -> None:
    """Save agent state to disk."""
    state.save(data_dir() / "system" / "state.json")


# Agent config — read from data repo, cached with mtime check
_agent_config_cache: dict[str, Any] | None = None
_agent_config_mtime: float = 0.0

# Defaults if agent_config.json is missing or incomplete.
# These are GENERIC defaults — agent-specific values live in the data repo's agent_config.json.
_AGENT_CONFIG_DEFAULTS: dict[str, Any] = {
    # SDK / model
    "model": "claude-opus-4-6",
    "max_thinking_tokens": 16000,
    # Tick behavior
    "initial_query": "Tick {tick} starting. Call login() to begin.",
    "hook_env_prefix": "AGENT",
}


def get_agent_config() -> dict[str, Any]:
    """Load agent config from data repo, with mtime caching and defaults."""
    global _agent_config_cache, _agent_config_mtime
    config_file = data_dir() / "system" / "agent_config.json"
    try:
        mtime = config_file.stat().st_mtime
    except OSError:
        mtime = 0.0
    if _agent_config_cache is None or mtime != _agent_config_mtime:
        config = dict(_AGENT_CONFIG_DEFAULTS)
        if config_file.exists():
            try:
                loaded = json.loads(config_file.read_text())
                config.update(loaded)
            except (OSError, json.JSONDecodeError):
                pass
        _agent_config_cache = config
        _agent_config_mtime = mtime
    return _agent_config_cache
