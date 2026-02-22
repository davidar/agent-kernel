"""Tests for instance registry."""

import src.registry as registry


class TestRegistry:
    def setup_method(self, tmp_path=None):
        """Save original paths to restore after test."""
        self._orig_dir = registry.REGISTRY_DIR
        self._orig_file = registry.REGISTRY_FILE
        self._orig_data = registry.DATA_BASE_DIR

    def teardown_method(self):
        registry.REGISTRY_DIR = self._orig_dir
        registry.REGISTRY_FILE = self._orig_file
        registry.DATA_BASE_DIR = self._orig_data

    def _use_tmp(self, tmp_path):
        registry.REGISTRY_DIR = tmp_path / "config"
        registry.REGISTRY_FILE = registry.REGISTRY_DIR / "instances.json"
        registry.DATA_BASE_DIR = tmp_path / "data"

    def test_load_empty(self, tmp_path):
        self._use_tmp(tmp_path)
        assert registry.load_registry() == {}

    def test_register_and_load(self, tmp_path):
        self._use_tmp(tmp_path)
        path = tmp_path / "my-agent"
        path.mkdir()
        registry.register("test-agent", path, remote="git@example.com:repo.git")
        loaded = registry.load_registry()
        assert "test-agent" in loaded
        assert loaded["test-agent"]["path"] == str(path.resolve())
        assert loaded["test-agent"]["remote"] == "git@example.com:repo.git"
        assert "created" in loaded["test-agent"]

    def test_unregister(self, tmp_path):
        self._use_tmp(tmp_path)
        path = tmp_path / "agent"
        path.mkdir()
        registry.register("agent", path)
        registry.unregister("agent")
        assert registry.load_registry() == {}

    def test_resolve_by_name(self, tmp_path):
        self._use_tmp(tmp_path)
        path = tmp_path / "agent"
        path.mkdir()
        registry.register("agent", path)
        resolved = registry.resolve("agent")
        assert resolved == path.resolve()

    def test_resolve_by_path(self, tmp_path):
        self._use_tmp(tmp_path)
        path = tmp_path / "agent"
        path.mkdir()
        resolved = registry.resolve(str(path))
        assert resolved == path.resolve()

    def test_resolve_unknown(self, tmp_path):
        self._use_tmp(tmp_path)
        assert registry.resolve("nonexistent") is None

    def test_list_instances(self, tmp_path):
        self._use_tmp(tmp_path)
        p1 = tmp_path / "a1"
        p1.mkdir()
        p2 = tmp_path / "a2"
        p2.mkdir()
        registry.register("agent1", p1)
        registry.register("agent2", p2)
        instances = registry.list_instances()
        assert "agent1" in instances
        assert "agent2" in instances
        assert len(instances) == 2
