"""Shared fixtures for agent kernel tests."""

import asyncio
import json
import shutil
import tempfile
import uuid
from pathlib import Path

import pytest

# Initialize config before any src imports
from src.config import init as _config_init

_config_init(Path(tempfile.mkdtemp(prefix="agent-test-data-")))


@pytest.fixture
def data_dir(tmp_path):
    """Create a temporary data directory with standard structure."""
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
