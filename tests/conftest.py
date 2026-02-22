"""Shared fixtures for agent kernel tests."""

import asyncio
import json
import shutil
import tempfile
import uuid
from pathlib import Path

import pytest

import src.registry as registry
from src.config import init as _config_init


def _use_tmp_registry(tmp_path: Path) -> None:
    """Point the registry at a temp dir so tests don't touch the real one."""
    registry.REGISTRY_DIR = tmp_path / "config"
    registry.REGISTRY_FILE = registry.REGISTRY_DIR / "instances.json"
    registry.DATA_BASE_DIR = tmp_path / "data"


# Snapshot the real registry file so we can detect accidental writes.
_REAL_REGISTRY_FILE = Path("~/.config/agent-kernel/instances.json").expanduser()
_REAL_REGISTRY_SNAPSHOT: bytes | None = _REAL_REGISTRY_FILE.read_bytes() if _REAL_REGISTRY_FILE.exists() else None
_REAL_REGISTRY_EXISTED = _REAL_REGISTRY_FILE.exists()


@pytest.fixture(autouse=True)
def _guard_real_registry():
    """Fail the test if it accidentally wrote to the real registry."""
    yield
    now_exists = _REAL_REGISTRY_FILE.exists()
    if not _REAL_REGISTRY_EXISTED and now_exists:
        pytest.fail(f"Test created the real registry file: {_REAL_REGISTRY_FILE}")
    if _REAL_REGISTRY_EXISTED and now_exists:
        current = _REAL_REGISTRY_FILE.read_bytes()
        if current != _REAL_REGISTRY_SNAPSHOT:
            pytest.fail(f"Test modified the real registry file: {_REAL_REGISTRY_FILE}")


# Module-level setup: register a dummy "test" instance so config.init works
# at import time for modules that need it.
_tmp = Path(tempfile.mkdtemp(prefix="agent-test-"))
_use_tmp_registry(_tmp)
_data = _tmp / "data" / "test"
_data.mkdir(parents=True)
registry.register("test", _data)
_config_init("test")


@pytest.fixture
def tmp_registry(tmp_path):
    """Redirect the registry to a temp dir for the duration of a test.

    Saves/restores the real registry globals so tests are isolated.
    """
    orig_dir = registry.REGISTRY_DIR
    orig_file = registry.REGISTRY_FILE
    orig_data = registry.DATA_BASE_DIR
    _use_tmp_registry(tmp_path)
    yield tmp_path
    registry.REGISTRY_DIR = orig_dir
    registry.REGISTRY_FILE = orig_file
    registry.DATA_BASE_DIR = orig_data


@pytest.fixture
def data_dir(tmp_path, tmp_registry):
    """Create a temporary data directory registered as "test" and init config."""
    d = tmp_path / "data"
    (d / "system").mkdir(parents=True)
    (d / "system" / "notifications").mkdir(parents=True)
    (d / "sandbox").mkdir(parents=True)
    (d / "sessions").mkdir(parents=True)
    (d / ".config").mkdir(parents=True)

    # Write a default state.json
    state = {
        "tick_count": 42,
        "last_tick": "2026-02-06T10:00:00",
        "last_tick_end": "2026-02-06T10:05:00",
        "first_tick_date": "2026-01-01",
    }
    (d / "system" / "state.json").write_text(json.dumps(state, indent=2))

    # Write a default schedule.json
    (d / "system" / "schedule.json").write_text(json.dumps({"wakes": []}, indent=2))

    # Register and init config
    registry.register("test", d)
    _config_init("test")

    return d


# --- Ephemeral test containers for TTY integration tests ---

_TEST_IMAGE = "localhost/agent-test"
_CONTAINERFILE = Path(__file__).parent / "Containerfile.test"


def _has_podman() -> bool:
    return shutil.which("podman") is not None


async def _ensure_test_image() -> None:
    """Build the test container image if it doesn't exist."""
    check = await asyncio.create_subprocess_exec(
        "podman",
        "image",
        "exists",
        _TEST_IMAGE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    await check.communicate()
    if check.returncode == 0:
        return

    proc = await asyncio.create_subprocess_exec(
        "podman",
        "build",
        "-t",
        _TEST_IMAGE,
        "-f",
        str(_CONTAINERFILE),
        str(_CONTAINERFILE.parent),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        pytest.fail(f"Failed to build test image: {stderr.decode()}")


@pytest.fixture(scope="class")
async def test_container():
    """Create an ephemeral podman container per test class, tear it down after.

    Builds the test image if needed, then creates a container.
    All tests in a class share the same container (they use separate tmux sessions).
    """
    if not _has_podman():
        pytest.fail("podman is required for integration tests but not found in PATH")

    await _ensure_test_image()

    name = f"agent-test-{uuid.uuid4().hex[:8]}"

    # Create and start
    proc = await asyncio.create_subprocess_exec(
        "podman",
        "run",
        "-d",
        "--name",
        name,
        _TEST_IMAGE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        pytest.fail(f"Failed to start test container: {stderr.decode()}")

    # Wait for container to be ready
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

    yield name

    # Teardown
    proc = await asyncio.create_subprocess_exec(
        "podman",
        "rm",
        "-f",
        name,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    await proc.communicate()
