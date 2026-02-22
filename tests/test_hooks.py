"""Tests for hook runner."""

import asyncio
import shutil
import stat
import uuid

import pytest

import src.config as config
from src.hooks import run_hooks, run_hooks_collect
from tests.conftest import _TEST_IMAGE, _ensure_test_image


def _has_podman() -> bool:
    return shutil.which("podman") is not None


def _make_script(path, content="#!/bin/bash\nexit 0\n"):
    """Create an executable script."""
    path.write_text(content)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


@pytest.fixture(scope="class")
def hook_container(tmp_path_factory):
    """Create a container with a shared temp dir mounted for hook tests."""
    if not _has_podman():
        pytest.fail("podman is required for hook tests but not found in PATH")

    tmp = tmp_path_factory.mktemp("hooks")
    name = f"agent-test-hooks-{uuid.uuid4().hex[:8]}"
    resolved = str(tmp.resolve())

    asyncio.run(_ensure_test_image())

    asyncio.run(_create_container(name, resolved))

    yield name, tmp

    # Teardown
    asyncio.run(_rm_container(name))


async def _create_container(name: str, mount_path: str):
    proc = await asyncio.create_subprocess_exec(
        "podman",
        "run",
        "-d",
        "--name",
        name,
        "--volume",
        f"{mount_path}:{mount_path}:Z,rw",
        _TEST_IMAGE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        pytest.fail(f"Failed to start hook test container: {stderr.decode()}")

    # Wait for container readiness
    for _ in range(10):
        check = await asyncio.create_subprocess_exec(
            "podman",
            "exec",
            name,
            "true",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await check.communicate()
        if check.returncode == 0:
            break
        await asyncio.sleep(0.2)


async def _rm_container(name: str):
    proc = await asyncio.create_subprocess_exec(
        "podman",
        "rm",
        "-f",
        name,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    await proc.communicate()


@pytest.fixture
def hook_env(hook_container):
    """Set up a data dir with hook directories inside the mounted volume."""
    name, tmp = hook_container
    # Create a unique subdir per test to avoid collisions
    data = tmp / f"data-{uuid.uuid4().hex[:8]}"
    data.mkdir()
    config.init(data)
    hook_dir = data / "system" / "hooks" / "test-hook"
    hook_dir.mkdir(parents=True, exist_ok=True)
    return hook_dir, name


class TestRunHooks:
    def test_empty_dir(self, hook_env):
        hook_dir, container = hook_env
        asyncio.run(run_hooks("test-hook", {}, container=container))

    def test_no_dir(self, hook_container):
        name, tmp = hook_container
        data = tmp / f"data-{uuid.uuid4().hex[:8]}"
        data.mkdir()
        config.init(data)
        asyncio.run(run_hooks("nonexistent-hook", {}, container=name))

    def test_runs_script(self, hook_env):
        hook_dir, container = hook_env
        data_dir = hook_dir.parent.parent.parent
        marker = data_dir / "ran.txt"
        _make_script(hook_dir / "01-test", f"#!/bin/bash\ntouch {marker}\n")
        asyncio.run(run_hooks("test-hook", {}, container=container))
        assert marker.exists()

    def test_sorted_order(self, hook_env):
        hook_dir, container = hook_env
        data_dir = hook_dir.parent.parent.parent
        order_file = data_dir / "order.txt"
        _make_script(hook_dir / "02-second", f"#!/bin/bash\necho second >> {order_file}\n")
        _make_script(hook_dir / "01-first", f"#!/bin/bash\necho first >> {order_file}\n")
        asyncio.run(run_hooks("test-hook", {}, container=container))
        lines = order_file.read_text().strip().splitlines()
        assert lines == ["first", "second"]

    def test_skips_dotfiles(self, hook_env):
        hook_dir, container = hook_env
        data_dir = hook_dir.parent.parent.parent
        marker = data_dir / "ran.txt"
        _make_script(hook_dir / ".hidden", f"#!/bin/bash\ntouch {marker}\n")
        asyncio.run(run_hooks("test-hook", {}, container=container))
        assert not marker.exists()

    def test_skips_backup_files(self, hook_env):
        hook_dir, container = hook_env
        data_dir = hook_dir.parent.parent.parent
        marker = data_dir / "ran.txt"
        _make_script(hook_dir / "script~", f"#!/bin/bash\ntouch {marker}\n")
        asyncio.run(run_hooks("test-hook", {}, container=container))
        assert not marker.exists()

    def test_skips_non_executable(self, hook_env):
        hook_dir, container = hook_env
        data_dir = hook_dir.parent.parent.parent
        marker = data_dir / "ran.txt"
        script = hook_dir / "script"
        script.write_text(f"#!/bin/bash\ntouch {marker}\n")
        # Don't set executable bit
        asyncio.run(run_hooks("test-hook", {}, container=container))
        assert not marker.exists()

    def test_env_passed(self, hook_env):
        hook_dir, container = hook_env
        data_dir = hook_dir.parent.parent.parent
        output = data_dir / "env.txt"
        _make_script(hook_dir / "01-env", f"#!/bin/bash\necho $DATA_DIR $MY_VAR > {output}\n")
        asyncio.run(run_hooks("test-hook", {"MY_VAR": "hello"}, container=container))
        content = output.read_text().strip()
        assert str(config.data_dir()) in content
        assert "hello" in content

    def test_failure_logged_not_fatal(self, hook_env):
        hook_dir, container = hook_env
        _make_script(hook_dir / "01-fail", "#!/bin/bash\nexit 1\n")
        # Should not raise
        asyncio.run(run_hooks("test-hook", {}, container=container))

    def test_timeout(self, hook_env):
        hook_dir, container = hook_env
        _make_script(hook_dir / "01-slow", "#!/bin/bash\nsleep 30\n")
        # Should not raise, just log warning
        asyncio.run(run_hooks("test-hook", {}, container=container, timeout=1))

    def test_custom_timeout(self, hook_env):
        hook_dir, container = hook_env
        data_dir = hook_dir.parent.parent.parent
        marker = data_dir / "ran.txt"
        _make_script(hook_dir / "01-sleep", f"#!/bin/bash\nsleep 3 && touch {marker}\n")
        asyncio.run(run_hooks("test-hook", {}, container=container, timeout=1))
        assert not marker.exists()


class TestRunHooksCollect:
    def test_collects_stdout(self, hook_env):
        hook_dir, container = hook_env
        _make_script(hook_dir / "01-check", '#!/bin/bash\necho "issue one"\necho "issue two"\n')
        lines = asyncio.run(run_hooks_collect("test-hook", {}, container=container))
        assert lines == ["issue one", "issue two"]

    def test_multiple_scripts_aggregate(self, hook_env):
        hook_dir, container = hook_env
        _make_script(hook_dir / "01-first", '#!/bin/bash\necho "from first"\n')
        _make_script(hook_dir / "02-second", '#!/bin/bash\necho "from second"\n')
        lines = asyncio.run(run_hooks_collect("test-hook", {}, container=container))
        assert lines == ["from first", "from second"]

    def test_failed_script_returns_no_lines(self, hook_env):
        hook_dir, container = hook_env
        _make_script(hook_dir / "01-fail", '#!/bin/bash\necho "should not appear"\nexit 1\n')
        _make_script(hook_dir / "02-ok", '#!/bin/bash\necho "visible"\n')
        lines = asyncio.run(run_hooks_collect("test-hook", {}, container=container))
        assert lines == ["visible"]

    def test_skips_blank_lines(self, hook_env):
        hook_dir, container = hook_env
        _make_script(hook_dir / "01-blanks", '#!/bin/bash\necho "real"\necho ""\necho "  "\necho "also real"\n')
        lines = asyncio.run(run_hooks_collect("test-hook", {}, container=container))
        assert lines == ["real", "also real"]

    def test_timeout(self, hook_env):
        hook_dir, container = hook_env
        _make_script(hook_dir / "01-slow", '#!/bin/bash\necho "slow"\nsleep 30\n')
        _make_script(hook_dir / "02-ok", '#!/bin/bash\necho "fast"\n')
        lines = asyncio.run(run_hooks_collect("test-hook", {}, container=container, timeout=1))
        # Slow script times out (no lines), fast script succeeds
        assert lines == ["fast"]

    def test_empty_dir(self, hook_env):
        hook_dir, container = hook_env
        lines = asyncio.run(run_hooks_collect("test-hook", {}, container=container))
        assert lines == []

    def test_no_dir(self, hook_container):
        name, tmp = hook_container
        data = tmp / f"data-{uuid.uuid4().hex[:8]}"
        data.mkdir()
        config.init(data)
        lines = asyncio.run(run_hooks_collect("nonexistent-hook", {}, container=name))
        assert lines == []

    def test_env_vars_in_container(self, hook_env):
        """Verify hook-specific env vars arrive inside the container."""
        hook_dir, container = hook_env
        data_dir = hook_dir.parent.parent.parent
        output = data_dir / "env_check.txt"
        _make_script(
            hook_dir / "01-env",
            f'#!/bin/bash\necho "DATA=$DATA_DIR TICK=$AGENT_TICK" > {output}\n',
        )
        asyncio.run(run_hooks("test-hook", {"AGENT_TICK": "42"}, container=container))
        content = output.read_text().strip()
        assert f"DATA={config.data_dir()}" in content
        assert "TICK=42" in content
