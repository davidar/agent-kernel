"""Hook system — run scripts from data_dir/system/hooks/{hook_type}/ inside the container."""

import asyncio
import os
import subprocess
from pathlib import Path

from .config import data_dir
from .logging_config import get_logger
from .notifications import send_crash_notification

logger = get_logger(__name__)

HOOK_TIMEOUT = 60  # seconds per script


def _discover_scripts(hook_type: str) -> list[Path]:
    """Find executable scripts in system/hooks/{hook_type}/, sorted by name."""
    hook_dir = data_dir() / "system" / "hooks" / hook_type
    if not hook_dir.is_dir():
        return []
    return sorted(
        p
        for p in hook_dir.iterdir()
        if p.is_file() and not p.name.startswith(".") and not p.name.endswith("~") and os.access(p, os.X_OK)
    )


def _build_env(env: dict[str, str]) -> dict[str, str]:
    """Build hook env vars: DATA_DIR + caller-provided vars (no host os.environ)."""
    return {"DATA_DIR": str(data_dir()), **env}


def _run_script(
    hook_type: str,
    script: Path,
    env: dict[str, str],
    timeout: int,
    container: str,
) -> subprocess.CompletedProcess[str] | None:
    """Run one hook script via podman exec. Returns CompletedProcess on success, None on error/timeout."""
    try:
        cmd: list[str] = ["podman", "exec"]
        for k, v in env.items():
            cmd.extend(["-e", f"{k}={v}"])
        cmd.extend([container, str(script)])

        result = subprocess.run(
            cmd,
            capture_output=True,
            timeout=timeout,
            text=True,
        )
        if result.returncode != 0:
            stderr = result.stderr.strip()[:500]
            logger.warning(
                "Hook %s/%s failed (exit %d): %s",
                hook_type,
                script.name,
                result.returncode,
                stderr,
            )
            send_crash_notification(f"Hook {hook_type}/{script.name} failed (exit {result.returncode})\n{stderr}")
            return None
        logger.debug("Hook %s/%s ok", hook_type, script.name)
        return result
    except subprocess.TimeoutExpired:
        logger.warning("Hook %s/%s timed out after %ds", hook_type, script.name, timeout)
        send_crash_notification(f"Hook {hook_type}/{script.name} timed out after {timeout}s")
        return None
    except Exception as exc:
        logger.exception("Hook %s/%s error", hook_type, script.name)
        send_crash_notification(f"Hook {hook_type}/{script.name} error: {exc}")
        return None


async def run_hooks(hook_type: str, env: dict[str, str], *, container: str, timeout: int = HOOK_TIMEOUT) -> None:
    """Run executable scripts from data_dir/system/hooks/{hook_type}/ in sorted order."""
    scripts = _discover_scripts(hook_type)
    if not scripts:
        return

    full_env = _build_env(env)

    for script in scripts:
        await asyncio.to_thread(_run_script, hook_type, script, full_env, timeout, container)


async def run_hooks_collect(
    hook_type: str, env: dict[str, str], *, container: str, timeout: int = HOOK_TIMEOUT
) -> list[str]:
    """Run hook scripts and collect stdout lines from successful (exit 0) scripts.

    Failed or timed-out scripts return no lines (fail-open).
    """
    scripts = _discover_scripts(hook_type)
    if not scripts:
        return []

    full_env = _build_env(env)
    lines: list[str] = []

    for script in scripts:
        result = await asyncio.to_thread(_run_script, hook_type, script, full_env, timeout, container)
        if result is not None:
            for line in result.stdout.splitlines():
                stripped = line.strip()
                if stripped:
                    lines.append(stripped)

    return lines
