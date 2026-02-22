"""Hook system — run scripts from data_dir/system/hooks/{hook_type}/."""

import asyncio
import os
import subprocess
from pathlib import Path

from .config import data_dir
from .logging_config import get_logger

logger = get_logger(__name__)

HOOK_TIMEOUT = 60  # seconds per script


async def run_hooks(hook_type: str, env: dict[str, str]) -> None:
    """Run executable scripts from data_dir/system/hooks/{hook_type}/ in sorted order."""
    hook_dir = data_dir() / "system" / "hooks" / hook_type
    if not hook_dir.is_dir():
        return

    # Discover executable scripts (skip dotfiles, backups, non-executable)
    scripts: list[Path] = sorted(
        p
        for p in hook_dir.iterdir()
        if p.is_file() and not p.name.startswith(".") and not p.name.endswith("~") and os.access(p, os.X_OK)
    )

    if not scripts:
        return

    # Merge caller env with os.environ (caller env wins)
    full_env = {**os.environ, "DATA_DIR": str(data_dir()), **env}

    for script in scripts:
        try:
            result = await asyncio.to_thread(
                subprocess.run,
                [str(script)],
                env=full_env,
                capture_output=True,
                timeout=HOOK_TIMEOUT,
                text=True,
            )
            if result.returncode != 0:
                logger.warning(
                    "Hook %s/%s failed (exit %d): %s",
                    hook_type,
                    script.name,
                    result.returncode,
                    result.stderr.strip()[:500],
                )
            else:
                logger.debug("Hook %s/%s ok", hook_type, script.name)
        except subprocess.TimeoutExpired:
            logger.warning("Hook %s/%s timed out after %ds", hook_type, script.name, HOOK_TIMEOUT)
        except Exception:
            logger.exception("Hook %s/%s error", hook_type, script.name)
