"""Tests for hook runner."""

import asyncio
import stat

import pytest

import src.config as config
import src.hooks as hooks_mod
from src.hooks import run_hooks


def _make_script(path, content="#!/bin/bash\nexit 0\n"):
    """Create an executable script."""
    path.write_text(content)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


@pytest.fixture
def hook_env(tmp_path):
    """Set up a data dir with hook directories."""
    config.init(tmp_path)
    hook_dir = tmp_path / "system" / "hooks" / "test-hook"
    hook_dir.mkdir(parents=True, exist_ok=True)
    return hook_dir


class TestRunHooks:
    def test_empty_dir(self, hook_env):
        asyncio.run(run_hooks("test-hook", {}))

    def test_no_dir(self, tmp_path):
        config.init(tmp_path)
        # No hooks directory at all
        asyncio.run(run_hooks("nonexistent-hook", {}))

    def test_runs_script(self, hook_env, tmp_path):
        marker = tmp_path / "ran.txt"
        _make_script(hook_env / "01-test", f"#!/bin/bash\ntouch {marker}\n")
        asyncio.run(run_hooks("test-hook", {}))
        assert marker.exists()

    def test_sorted_order(self, hook_env, tmp_path):
        order_file = tmp_path / "order.txt"
        _make_script(hook_env / "02-second", f"#!/bin/bash\necho second >> {order_file}\n")
        _make_script(hook_env / "01-first", f"#!/bin/bash\necho first >> {order_file}\n")
        asyncio.run(run_hooks("test-hook", {}))
        lines = order_file.read_text().strip().splitlines()
        assert lines == ["first", "second"]

    def test_skips_dotfiles(self, hook_env, tmp_path):
        marker = tmp_path / "ran.txt"
        _make_script(hook_env / ".hidden", f"#!/bin/bash\ntouch {marker}\n")
        asyncio.run(run_hooks("test-hook", {}))
        assert not marker.exists()

    def test_skips_backup_files(self, hook_env, tmp_path):
        marker = tmp_path / "ran.txt"
        _make_script(hook_env / "script~", f"#!/bin/bash\ntouch {marker}\n")
        asyncio.run(run_hooks("test-hook", {}))
        assert not marker.exists()

    def test_skips_non_executable(self, hook_env, tmp_path):
        marker = tmp_path / "ran.txt"
        script = hook_env / "script"
        script.write_text(f"#!/bin/bash\ntouch {marker}\n")
        # Don't set executable bit
        asyncio.run(run_hooks("test-hook", {}))
        assert not marker.exists()

    def test_env_passed(self, hook_env, tmp_path):
        output = tmp_path / "env.txt"
        _make_script(hook_env / "01-env", f"#!/bin/bash\necho $DATA_DIR $MY_VAR > {output}\n")
        data_dir = str(config.data_dir())
        asyncio.run(run_hooks("test-hook", {"MY_VAR": "hello"}))
        content = output.read_text().strip()
        assert data_dir in content
        assert "hello" in content

    def test_failure_logged_not_fatal(self, hook_env):
        _make_script(hook_env / "01-fail", "#!/bin/bash\nexit 1\n")
        # Should not raise
        asyncio.run(run_hooks("test-hook", {}))

    def test_timeout(self, hook_env):
        old_timeout = hooks_mod.HOOK_TIMEOUT
        hooks_mod.HOOK_TIMEOUT = 1  # 1 second timeout
        try:
            _make_script(hook_env / "01-slow", "#!/bin/bash\nsleep 30\n")
            # Should not raise, just log warning
            asyncio.run(run_hooks("test-hook", {}))
        finally:
            hooks_mod.HOOK_TIMEOUT = old_timeout
