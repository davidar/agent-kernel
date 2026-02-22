"""Tests for config module."""

import json
import time

import pytest

import src.config as config
import src.registry as registry


def _register_and_init(tmp_path, name="test"):
    """Register a temp dir and init config."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    registry.register(name, tmp_path)
    config.init(name)


class TestInit:
    def test_init_and_data_dir(self, tmp_registry, tmp_path):
        d = tmp_path / "data"
        _register_and_init(d)
        assert config.data_dir() == d.resolve()

    def test_init_and_instance_name(self, tmp_registry, tmp_path):
        d = tmp_path / "data"
        _register_and_init(d, "my-agent")
        assert config.instance_name() == "my-agent"

    def test_init_unknown_name_raises(self, tmp_registry):
        with pytest.raises(ValueError, match="not found"):
            config.init("nonexistent")

    def test_data_dir_raises_before_init(self):
        old = config._data_dir
        try:
            config._data_dir = None
            with pytest.raises(RuntimeError):
                config.data_dir()
        finally:
            config._data_dir = old

    def test_instance_name_raises_before_init(self):
        old = config._instance_name
        try:
            config._instance_name = None
            with pytest.raises(RuntimeError):
                config.instance_name()
        finally:
            config._instance_name = old


class TestEnsureDirs:
    def test_creates_structure(self, tmp_registry, tmp_path):
        d = tmp_path / "data"
        _register_and_init(d)
        config.ensure_dirs()
        assert (d / "notes").is_dir()
        assert (d / "system").is_dir()
        assert (d / "system" / "notifications").is_dir()
        assert (d / "sandbox").is_dir()
        assert (d / "tmp").is_dir()

    def test_wipes_tmp(self, tmp_registry, tmp_path):
        d = tmp_path / "data"
        _register_and_init(d)
        tmp_dir = d / "tmp"
        tmp_dir.mkdir()
        stale_file = tmp_dir / "stale.txt"
        stale_file.write_text("old data")
        config.ensure_dirs()
        assert not stale_file.exists()
        assert tmp_dir.is_dir()


class TestAgentConfig:
    def setup_method(self):
        # Reset cache between tests
        config._agent_config_cache = None
        config._agent_config_mtime = 0.0

    def test_defaults_when_no_file(self, tmp_registry, tmp_path):
        d = tmp_path / "data"
        _register_and_init(d)
        (d / "system").mkdir(parents=True, exist_ok=True)
        cfg = config.get_agent_config()
        assert cfg["model"] == "claude-opus-4-6"
        assert cfg["max_thinking_tokens"] == 16000

    def test_reads_from_file(self, tmp_registry, tmp_path):
        d = tmp_path / "data"
        _register_and_init(d)
        config_dir = d / "system"
        config_dir.mkdir(parents=True, exist_ok=True)
        config_file = config_dir / "agent_config.json"
        config_file.write_text(json.dumps({"model": "claude-sonnet-4-6", "custom_key": "val"}))
        cfg = config.get_agent_config()
        assert cfg["model"] == "claude-sonnet-4-6"
        assert cfg["custom_key"] == "val"
        # Defaults still present for unset keys
        assert cfg["max_thinking_tokens"] == 16000

    def test_caching(self, tmp_registry, tmp_path):
        d = tmp_path / "data"
        _register_and_init(d)
        config_dir = d / "system"
        config_dir.mkdir(parents=True, exist_ok=True)
        config_file = config_dir / "agent_config.json"
        config_file.write_text(json.dumps({"model": "claude-sonnet-4-6"}))
        cfg1 = config.get_agent_config()
        cfg2 = config.get_agent_config()
        assert cfg1 is cfg2  # Same object (cached)

    def test_reloads_on_change(self, tmp_registry, tmp_path):
        d = tmp_path / "data"
        _register_and_init(d)
        config_dir = d / "system"
        config_dir.mkdir(parents=True, exist_ok=True)
        config_file = config_dir / "agent_config.json"
        config_file.write_text(json.dumps({"model": "claude-sonnet-4-6"}))
        cfg1 = config.get_agent_config()
        assert cfg1["model"] == "claude-sonnet-4-6"
        # Change file (ensure mtime differs)
        time.sleep(0.05)
        config_file.write_text(json.dumps({"model": "claude-haiku-4-5-20251001"}))
        cfg2 = config.get_agent_config()
        assert cfg2["model"] == "claude-haiku-4-5-20251001"
