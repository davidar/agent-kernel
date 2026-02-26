"""Terminal multiplexer — numbered TTYs backed by tmux.

Each TTY is a tmux session in the container. Output is captured via
tmux capture-pane and written to session files. The agent reads these files
with the SDK's built-in Read/Grep/Glob tools.

TTY files:
  tmp/sessions/tty_N/screen       — plain text screen (visible portion)
  tmp/sessions/tty_N/screen.ansi  — screen with ANSI colors
  tmp/sessions/tty_N/raw          — raw ANSI output (from pipe-pane)
  tmp/sessions/tty_N/scrollback   — append-only plain text history
  tmp/sessions/tty_N/status       — one line: "idle", "exited (N)"
  tmp/sessions/registry.json       — TTY metadata for recovery

Diff tracking uses high-water marks (line counts) to show what changed.
The agent must call wait() to observe output before type() will accept
new input (observe-before-act pattern).
"""

from __future__ import annotations

import asyncio
import json
import re
import shlex
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import data_dir, get_container_name
from .logging_config import get_logger

logger = get_logger(__name__)


async def _podman_exec(*cmd: str, container: str) -> str:
    """Run a command in the container, return stdout."""
    proc = await asyncio.create_subprocess_exec(
        "podman",
        "exec",
        container,
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        err = stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"podman exec failed ({proc.returncode}): {err}")
    return stdout.decode("utf-8", errors="replace")


# Configuration defaults (matching the design spec table)
INLINE_THRESHOLD = 20  # Max new lines to include directly in tty diff
ELISION_HEAD = 10  # Lines to show at start of elided diff
ELISION_TAIL = 10  # Lines to show at end of elided diff
MAX_WAIT_TIMEOUT = 60  # Maximum allowed value for wait(timeout)
SETTLE_TIME = 1.5  # Settlement timeout — no new output for this long = settled
CAPTURE_INTERVAL = 0.5  # How often to capture tty buffers to log files
MAX_TTYS = 20  # Maximum concurrent TTYs

DEFAULT_ROWS = 40
DEFAULT_COLS = 120
SCROLLBACK_LINES = 5000  # tmux scrollback buffer size
RAW_MAX_BYTES = 2_000_000  # Rotate raw file at ~2MB


# tmux key names recognized by send-keys (sent without -l flag)
TMUX_KEY_NAMES = frozenset(
    {
        "Enter",
        "Escape",
        "Space",
        "Tab",
        "BSpace",
        "DC",
        "IC",
        "Up",
        "Down",
        "Left",
        "Right",
        "Home",
        "End",
        "PPage",
        "NPage",
        "F1",
        "F2",
        "F3",
        "F4",
        "F5",
        "F6",
        "F7",
        "F8",
        "F9",
        "F10",
        "F11",
        "F12",
    }
)

# Pattern for Ctrl/Alt key combos: C-a through C-z, C-\\, M-a through M-z
_TMUX_KEY_COMBO_RE = re.compile(r"^[CM]-.{1,2}$")


def _is_tmux_key(text: str) -> bool:
    """Check if text is a tmux key name (not literal text)."""
    if text in TMUX_KEY_NAMES:
        return True
    if _TMUX_KEY_COMBO_RE.match(text):
        return True
    return False


class TTY:
    """A terminal TTY backed by a tmux session in the container."""

    def __init__(self, tty_id: int, sessions_dir: Path):
        self.id = tty_id
        self.tmux_name = f"tty_{tty_id}"

        # Directory and file paths
        self.tty_dir = sessions_dir / self.tmux_name
        self.screen_file = self.tty_dir / "screen"
        self.screen_ansi_file = self.tty_dir / "screen.ansi"
        self.raw_file = self.tty_dir / "raw"
        self.scrollback_file = self.tty_dir / "scrollback"
        self.status_file = self.tty_dir / "status"
        # log_file referenced in diff messages (points to scrollback for full output)
        self.log_file = self.scrollback_file

        # Diff tracking
        self.high_water_mark: int = 0  # Line count last observed by LLM
        self.previous_lines: list[str] = []  # Content at last capture

        # Process state
        self.process_dead: bool = False
        self.exit_code: int | None = None
        self.command: str = "bash"
        self.current_command: str = ""  # Auto-detected from pane_current_command
        self.created: str = ""

    def get_new_lines(self) -> list[str]:
        """Get lines since the high-water mark."""
        if self.high_water_mark >= len(self.previous_lines):
            return []
        return self.previous_lines[self.high_water_mark :]

    def mark_seen(self) -> None:
        """Update high-water mark to current content end."""
        self.high_water_mark = len(self.previous_lines)


class TTYManager:
    """Manages numbered terminal TTYs with diff tracking.

    TTYs are tmux sessions in the container, continuously captured to
    session files. The agent reads these with Read/Grep/Glob and interacts
    via type/wait tools. The observe-before-act pattern requires the agent
    to call wait() before type() will accept new input.
    """

    def __init__(
        self,
        sessions_dir: Path | None = None,
        tick_number: int = 0,
        container_name: str | None = None,
        archive_dir: Path | None = None,
    ):
        if sessions_dir is None:
            sessions_dir = data_dir() / "tmp" / "sessions"
        if archive_dir is None:
            archive_dir = data_dir() / "system" / "logs" / "sessions"
        if container_name is None:
            container_name = get_container_name()
        self.sessions_dir = sessions_dir
        self.archive_dir = archive_dir
        self.tick_number = tick_number
        self.container_name = container_name
        self.ttys: dict[int, TTY] = {}
        self._capture_task: asyncio.Task | None = None
        self._activity_event: asyncio.Event = asyncio.Event()
        self._interrupt_event: asyncio.Event = asyncio.Event()
        self._running = False
        self._stale_ttys: list[dict] = []
        self.build_error: str | None = None

    async def start(self) -> None:
        """Start the background capture loop.

        Caller must ensure the container is running before calling start()
        (e.g. via container.ensure_ready()). start() only detects stale TTYs.
        """
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        self._running = True
        self._capture_task = asyncio.create_task(self._capture_loop())

        await self._detect_stale_ttys()

    async def stop(self) -> None:
        """Stop the capture loop."""
        self._running = False
        if self._capture_task:
            self._capture_task.cancel()
            try:
                await self._capture_task
            except asyncio.CancelledError:
                pass
            self._capture_task = None

    async def get_or_create_tty(self, tty_id: int, command: str = "bash") -> TTY:
        """Get an existing TTY or create a new one."""
        if tty_id in self.ttys:
            tty = self.ttys[tty_id]
            if not tty.process_dead:
                return tty

        # Check capacity
        if tty_id not in self.ttys and len(self.ttys) >= MAX_TTYS:
            raise RuntimeError(f"TTY limit reached ({MAX_TTYS}).")

        # Create new TTY
        tty = TTY(tty_id, self.sessions_dir)
        tty.command = command
        tty.created = datetime.now().isoformat()
        tty.tty_dir.mkdir(parents=True, exist_ok=True)

        # Check for surviving tmux session (hot-reload recovery)
        if await self._tmux_session_exists(tty.tmux_name):
            logger.info(f"TTY {tty_id}: reconnecting to surviving tmux session")
            await self._setup_pipe(tty)
        else:
            # Create tmux session
            await self._create_tmux_session(tty, command)
            await self._setup_pipe(tty)

        # Do initial capture
        await self._capture_tty(tty)
        tty.mark_seen()  # Don't diff existing content

        self.ttys[tty_id] = tty
        self._save_registry()
        return tty

    async def _exec(self, *cmd: str) -> str:
        """Run a command in this manager's container."""
        return await _podman_exec(*cmd, container=self.container_name)

    async def send_keys(self, tty_id: int, text: str) -> None:
        """Send keystrokes to a TTY via tmux send-keys.

        If text is a recognized tmux key name (e.g., "Enter", "C-c"),
        it is sent as a key. Otherwise it is sent as literal text.
        """
        tty = await self.get_or_create_tty(tty_id)

        if _is_tmux_key(text):
            await self._exec("tmux", "send-keys", "-t", tty.tmux_name, text)
        else:
            await self._exec("tmux", "send-keys", "-t", tty.tmux_name, "-l", text)

    def interrupt(self) -> None:
        """Interrupt a blocking wait_for_activity() call (e.g. notification arrived)."""
        self._interrupt_event.set()

    def has_unseen_changes(self) -> bool:
        """Check if any TTY has output not yet seen by the agent."""
        for tty in self.ttys.values():
            if tty.get_new_lines():
                return True
            if tty.process_dead and tty.high_water_mark < len(tty.previous_lines):
                return True
        return False

    async def wait_for_activity(self, timeout: float = 30, build_summary: bool = True) -> str:
        """Block until terminal output settles, then return a summary of all TTYs.

        Waits for the first activity event (new output or process exit), then
        continues polling until output settles (no new output for SETTLE_TIME).
        Returns the TTY status summary showing diffs for all open TTYs.

        Can be interrupted early by calling interrupt() (e.g. when a notification
        arrives mid-wait). Interrupted waits return current output immediately.

        If build_summary=False, just waits for settle without building/consuming
        the summary (caller reads diffs manually). Used by login().
        """
        timeout = min(timeout, MAX_WAIT_TIMEOUT)
        deadline = asyncio.get_event_loop().time() + timeout
        self._activity_event.clear()
        self._interrupt_event.clear()
        interrupted = False

        # Phase 1: Wait for first activity or interrupt
        remaining = deadline - asyncio.get_event_loop().time()
        if remaining > 0:
            activity_task = asyncio.create_task(self._activity_event.wait())
            interrupt_task = asyncio.create_task(self._interrupt_event.wait())
            done, pending = await asyncio.wait(
                [activity_task, interrupt_task],
                timeout=remaining,
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            if self._interrupt_event.is_set():
                interrupted = True
                self._interrupt_event.clear()

        # Phase 2: Settle — keep polling until no TTY produces new output
        # for SETTLE_TIME, or until we hit the settle deadline.
        # Skip settle if interrupted — return immediately with current output.
        if not interrupted:
            # Ensure Phase 2 always has enough time to settle, even if Phase 1
            # consumed the entire timeout waiting for the activity event.
            settle_deadline = max(deadline, asyncio.get_event_loop().time() + SETTLE_TIME + 1)
            settle_start = asyncio.get_event_loop().time()
            while asyncio.get_event_loop().time() < settle_deadline:
                if self._interrupt_event.is_set():
                    self._interrupt_event.clear()
                    break
                await asyncio.sleep(0.3)
                any_changed = False
                for tty in list(self.ttys.values()):
                    changed = await self._capture_tty(tty)
                    if changed:
                        any_changed = True
                if any_changed:
                    settle_start = asyncio.get_event_loop().time()
                elif asyncio.get_event_loop().time() - settle_start >= SETTLE_TIME:
                    break

        if not build_summary:
            return ""

        summary = self.build_tty_status_summary()

        # Auto-close dead TTYs after reporting their exit to the agent
        dead_ids = [tid for tid, tty in self.ttys.items() if tty.process_dead]
        for tid in dead_ids:
            try:
                await self.close_tty(tid)
            except Exception as e:
                logger.warning("Auto-close TTY %d failed: %s", tid, e)

        return summary or "No activity, timeout reached."

    def build_tty_status_summary(self) -> str:
        """Build the TTY status summary showing all open TTYs.

        Always reports all TTYs — TTYs with no new output show "no change".
        Returns empty string only when there are no TTYs at all.
        """
        if not self.ttys:
            return ""

        parts = []

        for tty_id in sorted(self.ttys.keys()):
            tty = self.ttys[tty_id]
            new_lines = tty.get_new_lines()

            if tty.process_dead:
                exit_str = f"process exited (code {tty.exit_code})" if tty.exit_code is not None else "process exited"
                if new_lines:
                    parts.append(self._format_tty_diff(tty_id, new_lines, exit_str))
                else:
                    parts.append(f"[tty {tty_id}: {self._tty_label(tty)}] {exit_str}, no new output")
                tty.mark_seen()
            elif new_lines:
                parts.append(self._format_tty_diff(tty_id, new_lines))
                tty.mark_seen()
            else:
                parts.append(f"[tty {tty_id}: {self._tty_label(tty)}] no change")

        return "\n".join(parts)

    def _format_tty_diff(self, tty_id: int, new_lines: list[str], prefix: str = "") -> str:
        """Format a TTY diff with optional middle elision."""
        tty = self.ttys[tty_id]
        count = len(new_lines)
        label = self._tty_label(tty)

        header_parts = [f"[tty {tty_id}: {label}]"]
        if prefix:
            header_parts.append(f"{prefix},")

        if count <= INLINE_THRESHOLD:
            # Short output: include inline in full
            header_parts.append(f"{count} new line{'s' if count != 1 else ''}:")
            header = " ".join(header_parts)
            content = "\n".join(f"  {line}" for line in new_lines)
            return f"{header}\n{content}"
        else:
            # Long output: elide middle, show head + tail
            header_parts.append(f"{count} new lines:")
            header = " ".join(header_parts)

            head = new_lines[:ELISION_HEAD]
            tail = new_lines[-ELISION_TAIL:]
            omitted = count - ELISION_HEAD - ELISION_TAIL

            head_content = "\n".join(f"  {line}" for line in head)
            tail_content = "\n".join(f"  {line}" for line in tail)

            return f"{header}\n{head_content}\n  ... ({omitted} lines omitted — full scrollback: {tty.log_file}) ...\n{tail_content}"

    def _tty_label(self, tty: TTY) -> str:
        """Human-readable label for a TTY (auto-detected current command)."""
        return tty.current_command or tty.command

    def get_lost_ttys(self) -> list[dict]:
        """Get TTYs lost due to container restart. For login reporting."""
        return self._stale_ttys

    def cleanup_stale(self) -> None:
        """Clear stale TTY tracking. Called after reporting at login."""
        self._stale_ttys = []

    # --- Background capture loop ---

    async def _capture_loop(self) -> None:
        """Periodically capture all TTY buffers to session files."""
        while self._running:
            try:
                await asyncio.sleep(CAPTURE_INTERVAL)
                if not self._running:
                    break

                any_changed = False
                for tty in list(self.ttys.values()):
                    changed = await self._capture_tty(tty)
                    if changed:
                        any_changed = True

                if any_changed:
                    self._activity_event.set()
                    self._save_registry()

            except asyncio.CancelledError:
                return
            except Exception as e:
                logger.error(f"Capture loop error: {e}")
                await asyncio.sleep(1)

    async def _capture_tty(self, tty: TTY) -> bool:
        """Capture a TTY's buffer to session files. Returns True if content changed."""
        try:
            # Check if process has exited and track command changes
            prev_command = tty.current_command
            await self._check_tty_status(tty)
            command_changed = tty.current_command != prev_command

            # 1. Full scrollback capture for diff tracking
            content = await self._exec(
                "tmux",
                "capture-pane",
                "-p",
                "-t",
                tty.tmux_name,
                "-S",
                f"-{SCROLLBACK_LINES}",
            )

            # Strip trailing empty lines
            lines = content.split("\n")
            while lines and not lines[-1].strip():
                lines.pop()

            # Check if content changed
            if lines == tty.previous_lines:
                return command_changed

            old_len = len(tty.previous_lines)
            new_len = len(lines)

            if new_len < tty.high_water_mark:
                # Screen cleared or buffer shrunk drastically — reset
                tty.high_water_mark = 0
            elif old_len > 0 and new_len == old_len and lines != tty.previous_lines:
                # Buffer full and sliding — old lines fell off the top,
                # new lines appeared at the bottom. Detect the shift so we
                # can adjust the high-water mark.
                shift = self._detect_buffer_shift(tty.previous_lines, lines)
                tty.high_water_mark = max(0, tty.high_water_mark - shift)

            tty.previous_lines = lines

            # Write scrollback file (full tmux buffer dump — tmux handles the size limit)
            try:
                tty.scrollback_file.write_text("\n".join(lines) + "\n" if lines else "")
            except OSError as e:
                logger.debug("TTY %d: write scrollback failed: %s", tty.id, e)

            # 2. Write screen file (visible portion — last DEFAULT_ROWS lines)
            visible_lines = lines[-DEFAULT_ROWS:] if len(lines) > DEFAULT_ROWS else lines
            try:
                tty.screen_file.write_text("\n".join(visible_lines) + "\n" if visible_lines else "")
            except OSError as e:
                logger.debug("TTY %d: write screen failed: %s", tty.id, e)

            # 3. Capture screen with ANSI colors
            try:
                ansi_content = await self._exec(
                    "tmux",
                    "capture-pane",
                    "-p",
                    "-e",
                    "-t",
                    tty.tmux_name,
                )
                ansi_lines = ansi_content.split("\n")
                while ansi_lines and not ansi_lines[-1].strip():
                    ansi_lines.pop()
                tty.screen_ansi_file.write_text("\n".join(ansi_lines) + "\n" if ansi_lines else "")
            except Exception as e:
                logger.debug("TTY %d: ANSI capture failed: %s", tty.id, e)

            # 4. Write status and rotate raw if needed
            self._write_status(tty)
            self._rotate_raw_if_needed(tty)

            return True

        except RuntimeError:
            # tmux session may be dead
            if not tty.process_dead:
                tty.process_dead = True
                self._write_status(tty)
                return True
            return False
        except Exception as e:
            logger.error(f"TTY {tty.id}: capture error: {e}")
            return False

    async def _check_tty_status(self, tty: TTY) -> None:
        """Check if a TTY's process has exited and update current command."""
        if tty.process_dead:
            return
        try:
            result = await self._exec(
                "tmux",
                "list-panes",
                "-t",
                tty.tmux_name,
                "-F",
                "#{pane_dead}|#{pane_dead_status}|#{pane_current_command}|#{pane_pid}",
            )
            parts = result.strip().split("|", 3)
            if len(parts) >= 3:
                dead, exit_status, cmd = parts[0], parts[1], parts[2]
                pane_pid = parts[3] if len(parts) >= 4 else ""
                if dead == "1":
                    tty.process_dead = True
                    tty.exit_code = int(exit_status) if exit_status else None
                    logger.info(f"TTY {tty.id}: process exited (code {tty.exit_code})")
                elif cmd:
                    tty.current_command = await self._resolve_command_name(cmd, pane_pid)
        except RuntimeError:
            # Session doesn't exist = process is dead
            tty.process_dead = True
        except Exception as e:
            logger.debug("TTY %d: status check failed: %s", tty.id, e)

    _INTERPRETERS = frozenset({"python3", "python", "node", "ruby", "perl", "bash", "sh"})

    async def _resolve_command_name(self, cmd: str, pane_pid: str) -> str:
        """Resolve a script name from /proc when tmux reports an interpreter.

        When a script runs via shebang (e.g. #!/usr/bin/env python3), tmux
        reports 'python3' as the command. We look at the child process's
        cmdline to extract the actual script name (e.g. 'chat').
        """
        if cmd not in self._INTERPRETERS or not pane_pid:
            return cmd
        try:
            # Find the foreground child of the pane's shell
            children = await self._exec("ps", "-o", "pid=", "--ppid", pane_pid)
            child_pid = children.strip().split()[0] if children.strip() else ""
            if not child_pid or not child_pid.isdigit():
                return cmd
            # Read its cmdline to find the script path
            cmdline = await self._exec("cat", f"/proc/{child_pid}/cmdline")
            # cmdline is null-delimited; argv[1] is typically the script path
            argv = cmdline.split("\x00")
            if len(argv) >= 2 and "/" in argv[1]:
                return argv[1].rsplit("/", 1)[-1]
        except Exception as e:
            logger.debug("TTY: resolve command name failed for pid %s: %s", pane_pid, e)
        return cmd

    @staticmethod
    def _detect_buffer_shift(old_lines: list[str], new_lines: list[str]) -> int:
        """Detect how many lines fell off the top of a full tmux buffer.

        When the buffer is full, new output pushes old lines off the top.
        We find the shift by looking for the start of new_lines in old_lines.
        If new_lines[0] == old_lines[k], then k lines fell off.
        """
        if not old_lines or not new_lines:
            return len(new_lines)

        target = new_lines[0]
        for i, line in enumerate(old_lines):
            if line == target:
                # Verify a few more lines to avoid false matches on repeated content
                verify = min(5, len(new_lines), len(old_lines) - i)
                if all(new_lines[j] == old_lines[i + j] for j in range(1, verify)):
                    return i

        # No overlap found — entire buffer is new (massive output burst)
        return len(new_lines)

    def _write_status(self, tty: TTY) -> None:
        """Write TTY status to file."""
        if tty.process_dead:
            if tty.exit_code is not None:
                status = f"exited ({tty.exit_code})"
            else:
                status = "exited"
        else:
            status = "idle"
        try:
            tty.status_file.write_text(status + "\n")
        except OSError as e:
            logger.debug("TTY %d: write status failed: %s", tty.id, e)

    def _rotate_raw_if_needed(self, tty: TTY) -> None:
        """Truncate raw file if it exceeds size limit."""
        try:
            if tty.raw_file.exists():
                size = tty.raw_file.stat().st_size
                if size > RAW_MAX_BYTES:
                    tty.raw_file.write_text("")
        except OSError as e:
            logger.debug("TTY %d: raw rotation failed: %s", tty.id, e)

    # --- Pipe-pane (raw ANSI output capture) ---

    async def _setup_pipe(self, tty: TTY) -> None:
        """Set up tmux pipe-pane to capture raw output to a file."""
        dd = data_dir()
        container_raw_path = f"{dd}/tmp/sessions/{tty.tmux_name}/raw"
        # Ensure the tty directory exists inside the container
        await self._exec("mkdir", "-p", str(dd / "tmp" / "sessions" / tty.tmux_name))
        # Close any existing pipe first, then start a new one.
        await self._exec("tmux", "pipe-pane", "-t", tty.tmux_name)
        await self._exec(
            "tmux",
            "pipe-pane",
            "-t",
            tty.tmux_name,
            f"cat >> {container_raw_path}",
        )
        # Touch raw file on host side
        tty.raw_file.touch()

    async def _tmux_session_exists(self, name: str) -> bool:
        """Check if a tmux session exists in the container."""
        try:
            await self._exec("tmux", "has-session", "-t", name)
            return True
        except RuntimeError:
            return False

    async def _create_tmux_session(self, tty: TTY, command: str = "bash") -> None:
        """Create a tmux session in the container for a TTY."""
        parts = ["podman", "exec"]
        parts.extend(["--env", "TERM=xterm-256color"])
        parts.extend(["--env", f"DATA_DIR={data_dir()}"])
        parts.append(self.container_name)

        parts.extend(
            [
                "tmux",
                "new-session",
                "-d",
                "-s",
                tty.tmux_name,
                "-x",
                str(DEFAULT_COLS),
                "-y",
                str(DEFAULT_ROWS),
            ]
        )

        if command != "bash":
            parts.append(f"bash -c {shlex.quote(command)}")

        logger.info(f"TTY {tty.id}: creating tmux session '{tty.tmux_name}'")
        proc = await asyncio.create_subprocess_exec(
            *parts, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(f"Failed to create TTY {tty.id}: {stderr.decode().strip()}")

        # Set scrollback limit
        try:
            await self._exec(
                "tmux",
                "set-option",
                "-t",
                tty.tmux_name,
                "history-limit",
                str(SCROLLBACK_LINES),
            )
        except Exception as e:
            logger.debug("TTY %d: set history-limit failed: %s", tty.id, e)

    # --- TTY lifecycle ---

    async def close_tty(self, tty_id: int) -> bool:
        """Close a TTY, archive its directory, and update registry."""
        tty = self.ttys.pop(tty_id, None)
        if not tty:
            return False

        try:
            await self._exec("tmux", "kill-session", "-t", tty.tmux_name)
        except RuntimeError as e:
            logger.debug("TTY %d: kill-session failed (already dead?): %s", tty_id, e)

        # Archive the TTY directory (scrollback is valuable for reference)
        self._archive_dir(tty.tty_dir)
        self._save_registry()
        return True

    def _archive_dir(self, tty_dir: Path) -> None:
        """Move a TTY directory to the archive, named by tick number.

        Archives go to system/logs/sessions/ (persistent, survives tmp/ wipe).
        Removes the raw file (large, not useful for reference), handles name
        collisions, and falls back to rmtree on error.
        """
        if not tty_dir.exists():
            return
        try:
            self.archive_dir.mkdir(parents=True, exist_ok=True)
            dest = self.archive_dir / f"{tty_dir.name}-tick-{self.tick_number}"
            if dest.exists():
                suffix = 1
                while dest.with_name(f"{dest.name}-{suffix}").exists():
                    suffix += 1
                dest = dest.with_name(f"{dest.name}-{suffix}")
            raw = tty_dir / "raw"
            if raw.exists():
                raw.unlink()
            shutil.move(str(tty_dir), str(dest))
        except OSError as e:
            logger.warning(f"Archive {tty_dir.name} failed ({e}), removing")
            shutil.rmtree(tty_dir, ignore_errors=True)

    async def close_all(self) -> None:
        """Close all TTYs."""
        for tty_id in list(self.ttys.keys()):
            await self.close_tty(tty_id)

    # --- Registry ---

    def _save_registry(self) -> None:
        """Flush TTY metadata to registry.json (atomic write)."""
        registry = {}
        for tty in self.ttys.values():
            status = "idle"
            if tty.process_dead:
                status = f"exited ({tty.exit_code})" if tty.exit_code is not None else "exited"
            registry[tty.tmux_name] = {
                "command": tty.command,
                "current_command": tty.current_command or tty.command,
                "created": tty.created,
                "status": status,
                "last_activity": datetime.now().isoformat(),
            }
        try:
            self.sessions_dir.mkdir(parents=True, exist_ok=True)
            registry_file = self.sessions_dir / "registry.json"
            tmp = registry_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(registry, indent=2))
            tmp.rename(registry_file)
        except OSError as e:
            logger.error(f"Failed to save TTY registry: {e}")

    def _load_registry(self) -> dict[str, Any]:
        """Load registry.json if it exists."""
        registry_file = self.sessions_dir / "registry.json"
        if registry_file.exists():
            try:
                return json.loads(registry_file.read_text())
            except (json.JSONDecodeError, OSError) as e:
                logger.debug("Failed to load TTY registry: %s", e)
        return {}

    # --- Stale TTY detection ---

    async def _detect_stale_ttys(self) -> None:
        """Check for TTYs in registry that are now dead (container restart).

        Populates _stale_ttys for reporting at login.
        """
        registry = self._load_registry()
        if not registry:
            return

        self._stale_ttys = []

        for name, meta in registry.items():
            alive = await self._tmux_session_exists(name)

            if alive:
                logger.info(f"TTY '{name}' survived (tmux still alive)")
            else:
                logger.info(f"TTY '{name}' lost (tmux session gone)")

                tty_dir = self.sessions_dir / name
                scrollback = tty_dir / "scrollback"
                has_scrollback = scrollback.exists()

                # Save scrollback.prev for agent reference, then archive
                if has_scrollback:
                    try:
                        scrollback.rename(tty_dir / "scrollback.prev")
                    except OSError as e:
                        logger.debug("Failed to rename scrollback for %s: %s", name, e)

                self._stale_ttys.append(
                    {
                        "name": name,
                        "command": meta.get("command", "unknown"),
                        "status": meta.get("status", "unknown"),
                        "has_scrollback": has_scrollback,
                    }
                )

                self._archive_dir(tty_dir)

        # Archive orphan directories (tty_* dirs not in registry, no live tmux)
        if self.sessions_dir.exists():
            registry_names = set(registry.keys())
            for entry in self.sessions_dir.iterdir():
                if not entry.is_dir() or not entry.name.startswith("tty_") or entry.name in registry_names:
                    continue
                # Check if there's a live tmux session for this orphan
                if await self._tmux_session_exists(entry.name):
                    continue
                logger.info(f"Archiving orphan TTY directory '{entry.name}'")
                self._archive_dir(entry)

        # Clear old registry (rebuilt as TTYs are created/reconnected)
        if self._stale_ttys:
            try:
                registry_file = self.sessions_dir / "registry.json"
                registry_file.unlink(missing_ok=True)
            except OSError as e:
                logger.debug("Failed to clear stale registry: %s", e)


# Module-level reference to the current tick's TTYManager.
# Set by init_tty_manager() at tick start, cleared by shutdown_tty_manager() at tick end.
# Tool functions access this via get_tty_manager() (synchronous, never auto-creates).
_tty_manager: TTYManager | None = None


async def init_tty_manager(tick_number: int = 0, build_error: str | None = None) -> TTYManager:
    """Create and start a new TTYManager for this tick.

    Each tick runs in a new asyncio.run() event loop, so the manager
    (with its asyncio.Event and capture Task) must be recreated per tick.
    Called from run_tick() at the start of each tick.

    Caller must ensure the container is running before calling this
    (e.g. via container.ensure_ready()). build_error is passed through
    for reporting via login().
    """
    global _tty_manager
    if _tty_manager is not None:
        _tty_manager._running = False
    _tty_manager = TTYManager(tick_number=tick_number)
    if build_error:
        _tty_manager.build_error = build_error
    await _tty_manager.start()
    return _tty_manager


async def shutdown_tty_manager() -> None:
    """Stop and clear the TTYManager at tick end."""
    global _tty_manager
    if _tty_manager is not None:
        await _tty_manager.stop()
        _tty_manager = None


def get_tty_manager() -> TTYManager:
    """Get the current tick's TTYManager.

    Returns the manager set by init_tty_manager(). Raises if not initialized.
    """
    if _tty_manager is None:
        raise RuntimeError("TTYManager not initialized — tick not started?")
    return _tty_manager
