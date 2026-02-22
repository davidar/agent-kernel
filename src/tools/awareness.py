"""Login tool and tick-end condition checks.

login() runs the configured startup command in tty 0 and returns the output.
Tick-end conditions are checked by the harness (agent.py) — no logout tool.

Kernel checks: login called, TTYs closed.
Data repo checks: delegated to system/tick-end-check script (see run_tick_end_script).
"""

import asyncio
import json
import os
from typing import Any

from claude_agent_sdk import tool

from ..config import data_dir

from ..logging_config import get_logger

logger = get_logger(__name__)


class _TickState:
    """Per-tick state, reset at the start of each tick."""

    def __init__(self):
        self.logged_in: bool = False


_tick = _TickState()


def reset_tick_state() -> None:
    """Reset tick-level state. Called at start of each tick."""
    global _tick
    _tick = _TickState()


def is_logged_in() -> bool:
    """Check if login() has been called this tick."""
    return _tick.logged_in


def check_tick_end_conditions() -> list[str]:
    """Check kernel-level conditions for tick end.

    Returns a list of blocking issues. Empty list = kernel checks pass.
    Always checks: login called, TTYs closed.
    """
    from ..tty import get_tty_manager

    issues = []

    if not _tick.logged_in:
        issues.append("You haven't called login() yet. Call login() first to get your situational awareness.")
        return issues

    try:
        mgr = get_tty_manager()
        open_ttys = list(mgr.ttys.keys())
        if open_ttys:
            tty_list = ", ".join(str(t) for t in sorted(open_ttys))
            issues.append(f"Open TTYs: {tty_list}. Close them with close(tty=N) or exit the shell.")
    except RuntimeError:
        pass

    return issues


async def run_tick_end_script(env: dict[str, str]) -> list[str]:
    """Run the data repo's tick-end check script.

    Returns list of issue strings (one per stdout line).
    Returns empty list if script doesn't exist or fails (fail-open).
    """
    script_path = data_dir() / "system" / "tick-end-check"

    if not script_path.exists() or not os.access(script_path, os.X_OK):
        return []

    try:
        proc = await asyncio.create_subprocess_exec(
            str(script_path),
            env={**os.environ, "DATA_DIR": str(data_dir()), **env},
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)

        if proc.returncode != 0:
            stderr_text = stderr.decode().strip()
            logger.warning("tick-end-check exited %d: %s", proc.returncode, stderr_text)
            return []

        return [line.strip() for line in stdout.decode().splitlines() if line.strip()]
    except asyncio.TimeoutError:
        logger.warning("tick-end-check timed out (30s)")
        return []
    except Exception as e:
        logger.warning("tick-end-check failed: %s", e)
        return []


def _load_startup_config() -> dict:
    """Load startup TTY config from startup.json."""
    startup_file = data_dir() / "system" / "startup.json"
    default = {"ttys": [{"tty": 0, "command": "bash", "label": "bash"}]}
    if not startup_file.exists():
        return default
    try:
        startup = json.loads(startup_file.read_text())
        ttys = startup.get("ttys", [])
        if not any(t.get("tty") == 0 for t in ttys):
            ttys.insert(0, {"tty": 0, "command": "bash", "label": "bash"})
        startup["ttys"] = ttys
        return startup
    except (OSError, json.JSONDecodeError):
        return default


def _format_lost_ttys(tty_mgr) -> str | None:
    """Format a report of TTYs lost to container restart."""
    lost = tty_mgr.get_lost_ttys()
    if not lost:
        return None
    parts = []
    for lt in lost:
        scrollback_note = " (scrollback saved to scrollback.prev)" if lt.get("has_scrollback") else ""
        parts.append(f"  - {lt['name']} ({lt['command']}){scrollback_note}")
    tty_mgr.cleanup_stale()
    return "Lost TTYs (container restarted):\n" + "\n".join(parts)


async def _launch_startup_ttys(tty_mgr) -> list[str]:
    """Launch TTYs per startup config and return output sections."""
    startup = _load_startup_config()

    for entry in sorted(startup["ttys"], key=lambda t: t["tty"]):
        tty_num = entry["tty"]
        if tty_num != 0:
            await tty_mgr.get_or_create_tty(tty_num)
        await tty_mgr.send_keys(tty_num, entry["command"])
        await tty_mgr.send_keys(tty_num, "Enter")

    await tty_mgr.wait_for_activity(timeout=15, build_summary=False)

    sections = []

    # TTY 0: full output without elision (agent needs complete startup output)
    tty0 = tty_mgr.ttys.get(0)
    if tty0:
        startup_lines = tty0.get_new_lines()
        if startup_lines:
            sections.append("\n".join(startup_lines))

    # Other TTYs: normal diff summary (elision is fine)
    other_tty_ids = sorted(t for t in tty_mgr.ttys if t != 0)
    if other_tty_ids:
        other_parts = []
        for tid in other_tty_ids:
            tty = tty_mgr.ttys[tid]
            new_lines = tty.get_new_lines()
            if new_lines:
                other_parts.append(tty_mgr._format_tty_diff(tid, new_lines))
            else:
                other_parts.append(f"[tty {tid}: {tty_mgr._tty_label(tty)}] no change")
        sections.append("\n".join(other_parts))

    # Mark all seen so first type() doesn't fail
    for tty in tty_mgr.ttys.values():
        tty.mark_seen()

    return sections


@tool(
    "login",
    "Log in to your workstation. Call this FIRST at the start of every tick. Returns startup output.",
    {},
)
async def login(args: dict[str, Any]) -> dict[str, Any]:
    """Login: report lost TTYs, launch startup TTYs, return output."""
    from ..tty import get_tty_manager

    _tick.logged_in = True

    sections = []

    try:
        tty_mgr = get_tty_manager()

        # Close any existing TTYs (relogin after compaction — start fresh)
        existing_ttys = list(tty_mgr.ttys.keys())
        if existing_ttys:
            for tid in existing_ttys:
                await tty_mgr.close_tty(tid)

        lost_report = _format_lost_ttys(tty_mgr)
        if lost_report:
            sections.append(lost_report)

        if tty_mgr.build_error:
            sections.append(
                "Container image rebuild FAILED (your Containerfile changes did not take effect):\n"
                f"  {tty_mgr.build_error}\n"
                "Fix system/container/Containerfile and it will retry next tick."
            )

        tty_sections = await _launch_startup_ttys(tty_mgr)
        sections.extend(tty_sections)

    except Exception as e:
        sections.append(f"(TTY setup error: {e})")

    return {
        "content": [
            {
                "type": "text",
                "text": "\n\n".join(sections),
            }
        ]
    }
