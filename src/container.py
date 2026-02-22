"""Container management — build, create, start, destroy.

Replaces container/setup.sh with Python. Manages podman containers and
content-addressed images for agent data repos.

Image naming: agent-kernel-img-{sha256(build_dir_contents)[:12]}
  - Content-addressed: identical Containerfiles share one image.
  - When Containerfile changes, a new image is built. Old images are
    pruned if no container references them.

Container naming: agent-kernel-{instance_id}
  - One container per instance (data repo path).
  - Registered instances use their registry name as instance_id.
  - Ad-hoc paths use {slug}-{sha256(abspath)[:8]}.
"""

import asyncio
import hashlib
import re
import shutil
from pathlib import Path

from .logging_config import get_logger

logger = get_logger(__name__)


def _has_podman() -> bool:
    """Check if podman is available on the host."""
    return shutil.which("podman") is not None


def derive_instance_id(data_dir: Path) -> str:
    """Derive a stable, unique instance ID from a data directory path.

    Returns {slug}-{hash[:8]} where slug is a sanitized version of the
    directory basename and hash is derived from the absolute path.
    For registered instances, use the registry name instead.
    """
    name = data_dir.name
    slug = re.sub(r"[^a-z0-9]", "-", name.lower()).strip("-")
    if not slug:
        slug = "data"
    # Collapse multiple hyphens
    slug = re.sub(r"-+", "-", slug)
    path_hash = hashlib.sha256(str(data_dir.resolve()).encode()).hexdigest()[:8]
    return f"{slug}-{path_hash}"


def get_container_name(instance_id: str) -> str:
    """Get the podman container name for an instance."""
    return f"agent-kernel-{instance_id}"


def compute_image_name(data_dir: Path) -> str:
    """Compute a content-addressed image name from the build directory.

    Hashes all files in $DATA_DIR/system/container/ (sorted by name for
    stability). Returns agent-kernel-img-{hash[:12]}.

    If no build directory exists, returns a fallback name based on
    the data dir path.
    """
    build_dir = data_dir / "system" / "container"
    if not build_dir.is_dir():
        path_hash = hashlib.sha256(str(data_dir.resolve()).encode()).hexdigest()[:12]
        return f"agent-kernel-img-{path_hash}"

    h = hashlib.sha256()
    for path in sorted(build_dir.iterdir()):
        if path.is_file():
            h.update(path.name.encode())
            h.update(path.read_bytes())
    return f"agent-kernel-img-{h.hexdigest()[:12]}"


async def _run(
    *cmd: str,
    timeout: float = 300,
    stream_output: bool = False,
) -> tuple[int, str, str]:
    """Run a subprocess and return (returncode, stdout, stderr).

    If stream_output is True, streams stdout to the logger in real-time
    (useful for build output).
    """
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE if not stream_output else asyncio.subprocess.STDOUT,
    )

    if stream_output:
        output_lines: list[str] = []
        stdout = proc.stdout
        if stdout is None:
            raise RuntimeError("Failed to capture stdout from subprocess")
        try:

            async def _stream():
                async for raw in stdout:
                    line = raw.decode("utf-8", errors="replace").rstrip()
                    logger.info(f"[build] {line}")
                    output_lines.append(line)

            await asyncio.wait_for(_stream(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            return -1, "\n".join(output_lines), "Build timed out"
        await proc.wait()
        return proc.returncode or 0, "\n".join(output_lines), ""

    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return -1, "", "Command timed out"
    return (
        proc.returncode or 0,
        stdout_bytes.decode("utf-8", errors="replace"),
        stderr_bytes.decode("utf-8", errors="replace"),
    )


async def image_exists(image_name: str) -> bool:
    """Check if a podman image exists."""
    rc, _, _ = await _run("podman", "image", "exists", image_name)
    return rc == 0


async def container_exists(container_name: str) -> bool:
    """Check if a podman container exists."""
    rc, _, _ = await _run("podman", "container", "exists", container_name)
    return rc == 0


async def container_running(container_name: str) -> bool:
    """Check if a podman container is running."""
    rc, stdout, _ = await _run("podman", "inspect", "--format", "{{.State.Running}}", container_name)
    return rc == 0 and stdout.strip() == "true"


async def build_image(data_dir: Path, force: bool = False) -> str:
    """Build the container image if it doesn't already exist.

    Uses content-addressed naming: the image name is derived from the
    hash of all files in the build directory. If an image with that
    name already exists, the build is skipped (unless force=True).

    Returns the image name.
    """
    image_name = compute_image_name(data_dir)
    containerfile = data_dir / "system" / "container" / "Containerfile"
    build_context = data_dir / "system" / "container"

    if not containerfile.exists():
        raise FileNotFoundError(
            f"Containerfile not found at {containerfile}. "
            f"The Containerfile should be at $DATA_DIR/system/container/Containerfile."
        )

    if not force and await image_exists(image_name):
        logger.debug(f"Image {image_name} already exists, skipping build")
        return image_name

    logger.info(f"Building image {image_name} from {containerfile}...")
    rc, output, stderr = await _run(
        "podman",
        "build",
        "-t",
        image_name,
        "-f",
        str(containerfile),
        str(build_context),
        stream_output=True,
        timeout=600,
    )

    if rc != 0:
        raise RuntimeError(f"Image build failed (exit {rc}): {stderr or output[-500:]}")

    logger.info(f"Image {image_name} built successfully")
    return image_name


async def create_container(
    data_dir: Path,
    container_name: str,
    image_name: str,
) -> None:
    """Create a podman container for an agent instance.

    Mounts data_dir at the same path inside the container so host and
    container paths match (SDK tools and terminal tools see the same paths).
    """
    # Ensure data directories exist
    (data_dir / "sandbox").mkdir(parents=True, exist_ok=True)
    (data_dir / "system").mkdir(parents=True, exist_ok=True)
    (data_dir / "system" / "notifications").mkdir(parents=True, exist_ok=True)

    resolved = str(data_dir.resolve())
    volumes = [
        "--volume",
        f"{resolved}:{resolved}:Z,rw",
    ]

    rc, stdout, stderr = await _run(
        "podman",
        "create",
        "--name",
        container_name,
        "--systemd=always",
        *volumes,
        "--workdir",
        f"{resolved}/sandbox",
        image_name,
    )

    if rc != 0:
        raise RuntimeError(f"Container creation failed: {stderr.strip()}")

    logger.info(f"Container {container_name} created")


async def ensure_running(container_name: str) -> None:
    """Start the container if it's not already running."""
    if not await container_exists(container_name):
        raise RuntimeError(f"Container {container_name} does not exist. Run setup first.")

    rc, _, stderr = await _run("podman", "start", container_name)
    if rc != 0:
        logger.warning(f"Container start returned {rc}: {stderr.strip()}")


async def destroy(container_name: str) -> None:
    """Force-remove a container."""
    rc, _, stderr = await _run("podman", "rm", "-f", container_name)
    if rc != 0:
        logger.warning(f"Container remove returned {rc}: {stderr.strip()}")
    else:
        logger.info(f"Container {container_name} destroyed")


async def dns_works(container_name: str) -> bool:
    """Quick DNS check inside the container."""
    try:
        rc, _, _ = await _run(
            "podman",
            "exec",
            container_name,
            "python3",
            "-c",
            "import socket; socket.getaddrinfo('api.anthropic.com', 443)",
            timeout=10,
        )
        return rc == 0
    except Exception:
        return False


async def get_container_image(container_name: str) -> str | None:
    """Get the image name a container was created from."""
    rc, stdout, _ = await _run(
        "podman",
        "inspect",
        "--format",
        "{{.ImageName}}",
        container_name,
    )
    if rc == 0 and stdout.strip():
        return stdout.strip()
    return None


async def prune_stale(keep_container: str | None = None) -> None:
    """Remove stopped agent-kernel containers and unused agent-kernel images.

    After a rebuild, old containers and images can accumulate. This finds
    all agent-kernel-* containers that are stopped (excluding keep_container)
    and removes them, then removes any agent-kernel-img-* images that no
    remaining container references.
    """
    # Find all agent-kernel containers
    rc, stdout, _ = await _run(
        "podman",
        "ps",
        "-a",
        "--format",
        "{{.Names}}",
        "--filter",
        "name=^agent-kernel-",
    )
    if rc != 0:
        return

    all_containers = [name.strip() for name in stdout.strip().splitlines() if name.strip()]

    # Remove stopped containers (except the one we want to keep)
    for name in all_containers:
        if name == keep_container:
            continue
        if not await container_running(name):
            logger.info(f"Removing stale container: {name}")
            await destroy(name)

    # Find images in use by remaining containers
    images_in_use: set[str] = set()
    rc, stdout, _ = await _run(
        "podman",
        "ps",
        "-a",
        "--format",
        "{{.Image}}",
        "--filter",
        "name=^agent-kernel-",
    )
    if rc == 0:
        images_in_use = {img.strip() for img in stdout.strip().splitlines() if img.strip()}

    # Find all agent-kernel images
    rc, stdout, _ = await _run(
        "podman",
        "images",
        "--format",
        "{{.Repository}}:{{.Tag}}",
        "--filter",
        "reference=agent-kernel-img-*",
    )
    if rc != 0:
        return

    all_images = [img.strip() for img in stdout.strip().splitlines() if img.strip()]

    # Remove unused images
    for img in all_images:
        if img not in images_in_use:
            logger.info(f"Removing unused image: {img}")
            await _run("podman", "rmi", img)


async def check_rebuild(data_dir: Path, container_name: str) -> None:
    """Rebuild the container image if the Containerfile has changed.

    Compares the current content-addressed image name with the image
    the container was built from. If they differ, rebuilds and recreates
    the container, then prunes stale containers and unused images.
    """
    build_dir = data_dir / "system" / "container"
    if not build_dir.is_dir():
        return

    # Compute what the image name should be now
    current_image = compute_image_name(data_dir)

    # Check if the running container already uses this image
    if await container_exists(container_name):
        running_image = await get_container_image(container_name)
        if running_image and current_image in running_image:
            return

    # Check if the image exists — if so, just need to recreate the container
    image_ready = await image_exists(current_image)

    if not image_ready:
        # Image doesn't exist (new content hash) — build it
        logger.info("Containerfile changed, rebuilding image...")

    try:
        new_image = await build_image(data_dir, force=not image_ready)

        # Recreate container with new image
        if await container_exists(container_name):
            await destroy(container_name)

        await create_container(data_dir, container_name, new_image)
        await ensure_running(container_name)
        logger.info("Container recreated with new image")

        # Clean up stale containers and unused images
        await prune_stale(keep_container=container_name)
    except Exception as e:
        logger.error(f"Image rebuild failed: {e}")


async def setup(
    data_dir: Path,
    instance_id: str | None = None,
    rebuild: bool = False,
) -> str:
    """Full container setup: build image, create container, start it.

    Returns the container name.
    """
    if not _has_podman():
        raise RuntimeError("podman is not installed")

    if instance_id is None:
        instance_id = derive_instance_id(data_dir)

    container_name = get_container_name(instance_id)
    image_name = await build_image(data_dir, force=rebuild)

    # If container exists with a different image, recreate
    if await container_exists(container_name):
        if rebuild:
            logger.info(f"Destroying existing container {container_name} for rebuild...")
            await destroy(container_name)
        else:
            await ensure_running(container_name)
            return container_name

    await create_container(data_dir, container_name, image_name)
    await ensure_running(container_name)

    # Verify container is working
    rc, stdout, _ = await _run(
        "podman",
        "exec",
        container_name,
        "bash",
        "-c",
        "echo ok",
        timeout=10,
    )
    if rc != 0:
        raise RuntimeError(f"Container {container_name} created but not responding")

    logger.info(f"Container {container_name} ready (image: {image_name})")
    return container_name
