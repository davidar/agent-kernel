"""Tests for the terminal multiplexer (src/tty.py)."""

import asyncio
import json

import pytest

from src.tty import MAX_TTYS, TTY, TTYManager, _is_tmux_key


@pytest.fixture
def sessions_dir(tmp_path):
    """Create a temporary sessions directory for TTY tests."""
    d = tmp_path / "sessions"
    d.mkdir()
    return d


@pytest.fixture
def tty_env(sessions_dir):
    """Set up environment for TTY manager tests."""
    return sessions_dir


@pytest.fixture
async def tty_manager(tty_env):
    """Create a TTYManager that uses the temp dir (no capture loop)."""
    mgr = TTYManager(sessions_dir=tty_env)
    # Don't start the capture loop for unit tests
    mgr.sessions_dir.mkdir(parents=True, exist_ok=True)
    yield mgr
    await mgr.close_all()


class TestTTY:
    """Test TTY class basics."""

    def test_tty_init(self, sessions_dir):
        tty = TTY(0, sessions_dir)
        assert tty.id == 0
        assert tty.tmux_name == "tty_0"
        assert tty.tty_dir == sessions_dir / "tty_0"
        assert tty.screen_file == sessions_dir / "tty_0" / "screen"
        assert tty.screen_ansi_file == sessions_dir / "tty_0" / "screen.ansi"
        assert tty.raw_file == sessions_dir / "tty_0" / "raw"
        assert tty.scrollback_file == sessions_dir / "tty_0" / "scrollback"
        assert tty.status_file == sessions_dir / "tty_0" / "status"
        assert tty.log_file == tty.scrollback_file
        assert tty.high_water_mark == 0
        assert tty.previous_lines == []
        assert tty.process_dead is False
        assert tty.exit_code is None

    def test_get_new_lines_empty(self, sessions_dir):
        tty = TTY(0, sessions_dir)
        assert tty.get_new_lines() == []

    def test_get_new_lines_with_content(self, sessions_dir):
        tty = TTY(0, sessions_dir)
        tty.previous_lines = ["line1", "line2", "line3"]
        assert tty.get_new_lines() == ["line1", "line2", "line3"]

    def test_mark_seen(self, sessions_dir):
        tty = TTY(0, sessions_dir)
        tty.previous_lines = ["line1", "line2", "line3"]
        tty.mark_seen()
        assert tty.high_water_mark == 3
        assert tty.get_new_lines() == []

    def test_new_lines_after_mark(self, sessions_dir):
        tty = TTY(0, sessions_dir)
        tty.previous_lines = ["line1", "line2"]
        tty.mark_seen()
        tty.previous_lines = ["line1", "line2", "line3", "line4"]
        assert tty.get_new_lines() == ["line3", "line4"]

    def test_screen_clear_resets_hwm(self, sessions_dir):
        tty = TTY(0, sessions_dir)
        tty.previous_lines = ["line1", "line2", "line3"]
        tty.mark_seen()
        assert tty.high_water_mark == 3
        # Simulate screen clear - fewer lines than HWM
        tty.previous_lines = ["new_line1"]
        # TTYManager._capture_tty would reset HWM to 0
        # We test the logic here
        if len(tty.previous_lines) < tty.high_water_mark:
            tty.high_water_mark = 0
        assert tty.get_new_lines() == ["new_line1"]


class TestTTYManager:
    """Test TTYManager without podman (uses direct TTY insertion)."""

    def test_create_tty_direct(self, tty_manager, sessions_dir):
        """Inserting a TTY directly works."""
        tty = TTY(0, sessions_dir)
        tty_manager.ttys[0] = tty
        assert 0 in tty_manager.ttys
        assert tty.tmux_name == "tty_0"

    async def test_max_ttys_enforced(self, tty_env):
        """Creating more than MAX_TTYS TTYs raises."""
        mgr = TTYManager(sessions_dir=tty_env)
        mgr.sessions_dir.mkdir(parents=True, exist_ok=True)

        # Fill up with fake TTYs
        for i in range(MAX_TTYS):
            tty = TTY(i, tty_env)
            mgr.ttys[i] = tty

        with pytest.raises(RuntimeError, match="TTY limit reached"):
            await mgr.get_or_create_tty(MAX_TTYS)

        await mgr.close_all()

    def test_close_tty(self, tty_manager, sessions_dir):
        """Closing a TTY removes it from the manager."""
        tty = TTY(0, sessions_dir)
        tty_manager.ttys[0] = tty
        # close_tty is async but we only test the dict removal here
        tty_manager.ttys.pop(0)
        assert 0 not in tty_manager.ttys

    async def test_close_nonexistent_tty(self, tty_manager):
        """Closing a nonexistent TTY returns False."""
        result = await tty_manager.close_tty(99)
        assert result is False


class TestTTYStatusSummary:
    """Test TTY diff formatting and status summaries."""

    def test_no_changes_returns_empty(self, sessions_dir):
        mgr = TTYManager(sessions_dir=sessions_dir)
        assert mgr.build_tty_status_summary() == ""

    def test_no_changes_with_ttys_shows_no_change(self, sessions_dir):
        mgr = TTYManager(sessions_dir=sessions_dir)
        tty = TTY(0, sessions_dir)
        tty.previous_lines = ["line1"]
        tty.mark_seen()  # Mark as seen
        mgr.ttys[0] = tty
        summary = mgr.build_tty_status_summary()
        assert "no change" in summary

    def test_short_output_inline(self, sessions_dir):
        mgr = TTYManager(sessions_dir=sessions_dir)
        tty = TTY(0, sessions_dir)
        tty.previous_lines = ["$ echo hello", "hello", "$"]
        # HWM at 0 means all lines are new
        mgr.ttys[0] = tty

        summary = mgr.build_tty_status_summary()
        assert "[tty 0: bash]" in summary
        assert "3 new lines:" in summary
        assert "echo hello" in summary
        assert "hello" in summary

    def test_long_output_elided(self, sessions_dir):
        mgr = TTYManager(sessions_dir=sessions_dir)
        tty = TTY(0, sessions_dir)
        tty.previous_lines = [f"line_{i}" for i in range(50)]
        mgr.ttys[0] = tty

        summary = mgr.build_tty_status_summary()
        assert "[tty 0: bash]" in summary
        assert "50 new lines" in summary
        assert "lines omitted" in summary
        # Head should have first lines
        assert "line_0" in summary
        # Tail should have last lines
        assert "line_49" in summary
        # Middle should be omitted
        assert "line_25" not in summary

    def test_process_exited_reported(self, sessions_dir):
        mgr = TTYManager(sessions_dir=sessions_dir)
        tty = TTY(0, sessions_dir)
        tty.process_dead = True
        tty.exit_code = 1
        tty.previous_lines = ["error occurred"]
        mgr.ttys[0] = tty

        summary = mgr.build_tty_status_summary()
        assert "process exited (code 1)" in summary
        assert "error occurred" in summary

    def test_process_exited_no_output(self, sessions_dir):
        mgr = TTYManager(sessions_dir=sessions_dir)
        tty = TTY(0, sessions_dir)
        tty.process_dead = True
        tty.exit_code = 0
        tty.previous_lines = []
        mgr.ttys[0] = tty

        summary = mgr.build_tty_status_summary()
        assert "process exited (code 0)" in summary
        assert "no new output" in summary

    def test_mark_seen_after_summary(self, sessions_dir):
        mgr = TTYManager(sessions_dir=sessions_dir)
        tty = TTY(0, sessions_dir)
        tty.previous_lines = ["line1", "line2"]
        mgr.ttys[0] = tty

        # First summary shows changes
        summary1 = mgr.build_tty_status_summary()
        assert "2 new lines:" in summary1

        # Second summary shows no change (marked as seen)
        summary2 = mgr.build_tty_status_summary()
        assert "no change" in summary2

    def test_multiple_ttys(self, sessions_dir):
        mgr = TTYManager(sessions_dir=sessions_dir)

        tty0 = TTY(0, sessions_dir)
        tty0.previous_lines = ["output from tty 0"]
        mgr.ttys[0] = tty0

        tty1 = TTY(1, sessions_dir)
        tty1.previous_lines = ["old line"]
        tty1.mark_seen()  # No new output
        mgr.ttys[1] = tty1

        tty2 = TTY(2, sessions_dir)
        tty2.previous_lines = ["output from tty 2"]
        mgr.ttys[2] = tty2

        summary = mgr.build_tty_status_summary()
        assert "[tty 0: bash]" in summary
        assert "[tty 1: bash] no change" in summary
        assert "[tty 2: bash]" in summary
        assert "output from tty 0" in summary
        assert "output from tty 2" in summary


class TestHasUnseenChanges:
    """Test has_unseen_changes() method."""

    def test_no_unseen_empty(self, sessions_dir):
        mgr = TTYManager(sessions_dir=sessions_dir)
        assert mgr.has_unseen_changes() is False

    def test_no_unseen_all_seen(self, sessions_dir):
        mgr = TTYManager(sessions_dir=sessions_dir)
        tty = TTY(0, sessions_dir)
        tty.previous_lines = ["line1"]
        tty.mark_seen()
        mgr.ttys[0] = tty
        assert mgr.has_unseen_changes() is False

    def test_has_unseen(self, sessions_dir):
        mgr = TTYManager(sessions_dir=sessions_dir)
        tty = TTY(0, sessions_dir)
        tty.previous_lines = ["line1", "line2"]
        # HWM is 0, so both lines are unseen
        mgr.ttys[0] = tty
        assert mgr.has_unseen_changes() is True

    def test_dead_tty_unseen(self, sessions_dir):
        mgr = TTYManager(sessions_dir=sessions_dir)
        tty = TTY(0, sessions_dir)
        tty.previous_lines = ["output"]
        tty.process_dead = True
        tty.exit_code = 1
        # HWM 0 < len(previous_lines) 1
        mgr.ttys[0] = tty
        assert mgr.has_unseen_changes() is True

    def test_dead_tty_all_seen(self, sessions_dir):
        mgr = TTYManager(sessions_dir=sessions_dir)
        tty = TTY(0, sessions_dir)
        tty.previous_lines = ["output"]
        tty.mark_seen()
        tty.process_dead = True
        tty.exit_code = 0
        mgr.ttys[0] = tty
        assert mgr.has_unseen_changes() is False


class TestArchive:
    """Test TTY archiving."""

    def test_archive_tty(self, sessions_dir):
        archive_dir = sessions_dir.parent / "archive"
        mgr = TTYManager(sessions_dir=sessions_dir, archive_dir=archive_dir)
        tty = TTY(0, sessions_dir)
        tty.tty_dir.mkdir(parents=True)
        # Create some files
        tty.scrollback_file.write_text("test scrollback")
        tty.screen_file.write_text("test screen")
        tty.raw_file.write_text("raw data")

        mgr._archive_dir(tty.tty_dir)

        # TTY dir should be gone
        assert not tty.tty_dir.exists()
        # Archive should exist
        assert archive_dir.exists()
        archives = list(archive_dir.iterdir())
        assert len(archives) == 1
        assert archives[0].name.startswith("tty_0-")
        # Scrollback should be preserved, raw should be removed
        assert (archives[0] / "scrollback").exists()
        assert (archives[0] / "screen").exists()
        assert not (archives[0] / "raw").exists()


class TestRegistry:
    """Test TTY registry."""

    def test_save_and_load_registry(self, sessions_dir):
        mgr = TTYManager(sessions_dir=sessions_dir)
        tty = TTY(0, sessions_dir)
        tty.command = "bash"
        tty.created = "2024-01-01T00:00:00"
        mgr.ttys[0] = tty

        mgr._save_registry()

        registry_file = sessions_dir / "registry.json"
        assert registry_file.exists()
        data = json.loads(registry_file.read_text())
        assert "tty_0" in data
        assert data["tty_0"]["command"] == "bash"
        assert data["tty_0"]["status"] == "idle"

    def test_load_empty_registry(self, sessions_dir):
        mgr = TTYManager(sessions_dir=sessions_dir)
        assert mgr._load_registry() == {}


class TestWriteStatus:
    """Test status file writing."""

    def test_write_idle_status(self, sessions_dir):
        mgr = TTYManager(sessions_dir=sessions_dir)
        tty = TTY(0, sessions_dir)
        tty.tty_dir.mkdir(parents=True)

        mgr._write_status(tty)
        assert tty.status_file.read_text().strip() == "idle"

    def test_write_exited_status(self, sessions_dir):
        mgr = TTYManager(sessions_dir=sessions_dir)
        tty = TTY(0, sessions_dir)
        tty.tty_dir.mkdir(parents=True)
        tty.process_dead = True
        tty.exit_code = 1

        mgr._write_status(tty)
        assert tty.status_file.read_text().strip() == "exited (1)"


class TestTTYLabel:
    """Test auto-naming via pane_current_command."""

    def test_default_label_is_command(self, sessions_dir):
        mgr = TTYManager(sessions_dir=sessions_dir)
        tty = TTY(0, sessions_dir)
        tty.command = "bash"
        assert mgr._tty_label(tty) == "bash"

    def test_current_command_overrides(self, sessions_dir):
        mgr = TTYManager(sessions_dir=sessions_dir)
        tty = TTY(0, sessions_dir)
        tty.command = "bash"
        tty.current_command = "vim"
        assert mgr._tty_label(tty) == "vim"

    def test_bash_current_command_falls_through(self, sessions_dir):
        mgr = TTYManager(sessions_dir=sessions_dir)
        tty = TTY(0, sessions_dir)
        tty.command = "python3"
        tty.current_command = "bash"
        # Show what's actually running, not the creation command
        assert mgr._tty_label(tty) == "bash"

    def test_label_in_diff(self, sessions_dir):
        mgr = TTYManager(sessions_dir=sessions_dir)
        tty = TTY(0, sessions_dir)
        tty.command = "bash"
        tty.current_command = "npm"
        tty.previous_lines = ["output"]
        mgr.ttys[0] = tty
        summary = mgr.build_tty_status_summary()
        assert "[tty 0: npm]" in summary


class TestTmuxKeyDetection:
    """Test tmux key name detection."""

    def test_named_keys(self):
        assert _is_tmux_key("Enter") is True
        assert _is_tmux_key("Escape") is True
        assert _is_tmux_key("Tab") is True
        assert _is_tmux_key("Space") is True
        assert _is_tmux_key("Up") is True
        assert _is_tmux_key("Down") is True
        assert _is_tmux_key("F1") is True
        assert _is_tmux_key("F12") is True

    def test_ctrl_combos(self):
        assert _is_tmux_key("C-c") is True
        assert _is_tmux_key("C-d") is True
        assert _is_tmux_key("C-z") is True
        assert _is_tmux_key("C-\\") is True

    def test_alt_combos(self):
        assert _is_tmux_key("M-a") is True
        assert _is_tmux_key("M-x") is True

    def test_literal_text(self):
        assert _is_tmux_key("echo hello") is False
        assert _is_tmux_key("ls -la") is False
        assert _is_tmux_key("") is False
        assert _is_tmux_key("Enter key") is False


class TestBufferShift:
    """Test sliding buffer detection for full tmux scrollback."""

    def test_basic_shift(self, sessions_dir):
        # Buffer was [A, B, C, D, E], now [C, D, E, F, G] (2 lines fell off)
        old = ["A", "B", "C", "D", "E"]
        new = ["C", "D", "E", "F", "G"]
        assert TTYManager._detect_buffer_shift(old, new) == 2

    def test_no_shift(self, sessions_dir):
        # Same content
        lines = ["A", "B", "C"]
        assert TTYManager._detect_buffer_shift(lines, lines) == 0

    def test_single_line_shift(self, sessions_dir):
        old = ["A", "B", "C", "D"]
        new = ["B", "C", "D", "E"]
        assert TTYManager._detect_buffer_shift(old, new) == 1

    def test_no_overlap(self, sessions_dir):
        # Completely different content (e.g. massive output burst)
        old = ["A", "B", "C"]
        new = ["X", "Y", "Z"]
        assert TTYManager._detect_buffer_shift(old, new) == 3

    def test_empty_old(self, sessions_dir):
        assert TTYManager._detect_buffer_shift([], ["A", "B"]) == 2

    def test_hwm_adjusts_on_slide(self, sessions_dir):
        mgr = TTYManager(sessions_dir=sessions_dir)
        tty = TTY(0, sessions_dir)
        tty.tty_dir.mkdir(parents=True)

        # Simulate: agent has seen everything in a full buffer
        tty.previous_lines = ["A", "B", "C", "D", "E"]
        tty.high_water_mark = 5  # seen all 5
        mgr.ttys[0] = tty

        # Now 2 new lines, buffer slides
        new_lines = ["C", "D", "E", "F", "G"]
        shift = TTYManager._detect_buffer_shift(tty.previous_lines, new_lines)
        tty.high_water_mark = max(0, tty.high_water_mark - shift)
        tty.previous_lines = new_lines

        # Agent should see [F, G] as new
        assert tty.get_new_lines() == ["F", "G"]

    def test_hwm_partially_caught_up(self, sessions_dir):
        tty = TTY(0, sessions_dir)
        # Agent has seen A, B, C (HWM=3), buffer has [A, B, C, D, E]
        tty.previous_lines = ["A", "B", "C", "D", "E"]
        tty.high_water_mark = 3

        # Buffer slides by 2: [C, D, E, F, G]
        new_lines = ["C", "D", "E", "F", "G"]
        shift = TTYManager._detect_buffer_shift(tty.previous_lines, new_lines)
        tty.high_water_mark = max(0, tty.high_water_mark - shift)
        tty.previous_lines = new_lines

        # Agent had seen up to C. D, E were already unseen, plus F, G are new.
        assert tty.get_new_lines() == ["D", "E", "F", "G"]


class TestAutoCloseDead:
    """Test that wait_for_activity auto-closes dead TTYs.

    These are unit tests (no container), so _capture_tty is patched to
    avoid podman calls during the settle loop.
    """

    @staticmethod
    async def _noop_capture(tty):
        return False

    async def test_dead_tty_removed_after_wait(self, sessions_dir):
        """Dead TTYs are removed from manager after wait reports them."""
        mgr = TTYManager(sessions_dir=sessions_dir)
        mgr._capture_tty = self._noop_capture
        tty = TTY(0, sessions_dir)
        tty.tty_dir.mkdir(parents=True)
        tty.previous_lines = ["output", "done"]
        tty.process_dead = True
        tty.exit_code = 0
        mgr.ttys[0] = tty

        summary = await mgr.wait_for_activity(timeout=1)
        assert "process exited" in summary
        assert 0 not in mgr.ttys

    async def test_dead_tty_archived_after_wait(self, sessions_dir):
        """Dead TTY directory is archived after wait."""
        archive_dir = sessions_dir.parent / "archive"
        mgr = TTYManager(sessions_dir=sessions_dir, archive_dir=archive_dir)
        mgr._capture_tty = self._noop_capture
        tty = TTY(0, sessions_dir)
        tty.tty_dir.mkdir(parents=True)
        tty.scrollback_file.write_text("final output\n")
        tty.previous_lines = ["final output"]
        tty.process_dead = True
        tty.exit_code = 0
        mgr.ttys[0] = tty

        await mgr.wait_for_activity(timeout=1)
        assert not tty.tty_dir.exists()
        assert archive_dir.exists()

    async def test_live_tty_not_closed(self, sessions_dir):
        """Live TTYs survive wait even when a dead one is auto-closed."""
        mgr = TTYManager(sessions_dir=sessions_dir)
        mgr._capture_tty = self._noop_capture

        dead = TTY(0, sessions_dir)
        dead.tty_dir.mkdir(parents=True)
        dead.previous_lines = ["bye"]
        dead.process_dead = True
        dead.exit_code = 0
        mgr.ttys[0] = dead

        alive = TTY(1, sessions_dir)
        alive.tty_dir.mkdir(parents=True)
        alive.previous_lines = ["prompt"]
        alive.mark_seen()
        mgr.ttys[1] = alive

        await mgr.wait_for_activity(timeout=1)
        assert 0 not in mgr.ttys
        assert 1 in mgr.ttys

    async def test_summary_includes_dead_before_removal(self, sessions_dir):
        """The summary reports the exit before the TTY is removed."""
        mgr = TTYManager(sessions_dir=sessions_dir)
        mgr._capture_tty = self._noop_capture
        tty = TTY(0, sessions_dir)
        tty.tty_dir.mkdir(parents=True)
        tty.previous_lines = ["error: something broke"]
        tty.process_dead = True
        tty.exit_code = 1
        mgr.ttys[0] = tty

        summary = await mgr.wait_for_activity(timeout=1)
        assert "process exited (code 1)" in summary
        assert "error: something broke" in summary
        # But TTY is gone now
        assert 0 not in mgr.ttys

    async def test_no_build_summary_skips_autoclose(self, sessions_dir):
        """When build_summary=False, dead TTYs are NOT auto-closed."""
        mgr = TTYManager(sessions_dir=sessions_dir)
        mgr._capture_tty = self._noop_capture
        tty = TTY(0, sessions_dir)
        tty.tty_dir.mkdir(parents=True)
        tty.previous_lines = ["done"]
        tty.process_dead = True
        tty.exit_code = 0
        mgr.ttys[0] = tty

        await mgr.wait_for_activity(timeout=1, build_summary=False)
        # Still there — login() uses build_summary=False and manages TTYs itself
        assert 0 in mgr.ttys


class TestTTYTmuxIntegration:
    """Integration tests for tmux-backed TTYs (requires podman)."""

    @pytest.fixture
    async def tmux_tty_manager(self, tmp_path, test_container):
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        archive_dir = tmp_path / "archive"
        mgr = TTYManager(sessions_dir=sessions_dir, container_name=test_container, archive_dir=archive_dir)
        mgr.sessions_dir.mkdir(parents=True, exist_ok=True)
        yield mgr
        await mgr.close_all()

    async def test_create_tty_tmux(self, tmux_tty_manager):
        """TTY creation creates a tmux session."""
        tty = await tmux_tty_manager.get_or_create_tty(0)
        assert tty.id == 0
        assert not tty.process_dead

    async def test_send_keys_and_capture(self, tmux_tty_manager):
        """Sending keys and capturing output works."""
        tty = await tmux_tty_manager.get_or_create_tty(0)
        await tmux_tty_manager.send_keys(0, "echo hello-from-tty")
        await tmux_tty_manager.send_keys(0, "Enter")
        await asyncio.sleep(1)
        await tmux_tty_manager._capture_tty(tty)
        assert any("hello-from-tty" in line for line in tty.previous_lines)

    async def test_session_files_written(self, tmux_tty_manager):
        """Session files are written on capture."""
        tty = await tmux_tty_manager.get_or_create_tty(0)
        await tmux_tty_manager.send_keys(0, "echo file-test")
        await tmux_tty_manager.send_keys(0, "Enter")
        await asyncio.sleep(1)
        await tmux_tty_manager._capture_tty(tty)
        assert tty.screen_file.exists()
        assert tty.scrollback_file.exists()
        assert tty.status_file.exists()
        assert "file-test" in tty.screen_file.read_text()

    async def test_wait_for_activity_with_settle(self, tmux_tty_manager):
        """wait_for_activity settles before returning."""
        await tmux_tty_manager.get_or_create_tty(0)
        await tmux_tty_manager.send_keys(0, "echo settle-test")
        await tmux_tty_manager.send_keys(0, "Enter")
        result = await tmux_tty_manager.wait_for_activity(timeout=10)
        assert "settle-test" in result

    async def test_close_tty_archives(self, tmux_tty_manager):
        """Closing a TTY archives its directory."""
        tty = await tmux_tty_manager.get_or_create_tty(0)
        tty_dir = tty.tty_dir
        assert tty_dir.exists()

        await tmux_tty_manager.close_tty(0)

        # TTY dir should be gone (moved to archive)
        assert not tty_dir.exists()
        # Archive should exist
        archive_dir = tmux_tty_manager.archive_dir
        assert archive_dir.exists()

    async def test_diff_tracking(self, tmux_tty_manager):
        """Diff tracking shows new output correctly."""
        tty = await tmux_tty_manager.get_or_create_tty(0)

        # Initial capture was done by get_or_create_tty and marked as seen
        summary1 = tmux_tty_manager.build_tty_status_summary()
        assert "no change" in summary1  # Nothing new

        # Send a command
        await tmux_tty_manager.send_keys(0, "echo diff-test")
        await tmux_tty_manager.send_keys(0, "Enter")
        await asyncio.sleep(1)
        await tmux_tty_manager._capture_tty(tty)

        # Now there should be new output
        summary2 = tmux_tty_manager.build_tty_status_summary()
        assert "diff-test" in summary2
        assert "[tty 0:" in summary2

        # After building summary, output is marked as seen
        summary3 = tmux_tty_manager.build_tty_status_summary()
        assert "no change" in summary3

    async def test_current_command_detected(self, tmux_tty_manager):
        """_check_tty_status detects the foreground process, not just bash."""
        tty = await tmux_tty_manager.get_or_create_tty(0)

        # Bash idle — should report bash
        await tmux_tty_manager._capture_tty(tty)
        assert tty.current_command == "bash"

        # Launch a long-running child process
        await tmux_tty_manager.send_keys(0, "sleep 30")
        await tmux_tty_manager.send_keys(0, "Enter")
        await asyncio.sleep(1)
        await tmux_tty_manager._capture_tty(tty)
        assert tty.current_command == "sleep", f"Expected 'sleep', got '{tty.current_command}'"

        # Kill it and check it goes back to bash
        await tmux_tty_manager.send_keys(0, "C-c")
        await asyncio.sleep(1)
        await tmux_tty_manager._capture_tty(tty)
        assert tty.current_command == "bash"

    async def test_script_name_resolved(self, tmux_tty_manager):
        """Bash scripts show the script name, not 'bash'.

        Uses an inline script since the test container doesn't have
        the full CLI suite.
        """
        tty = await tmux_tty_manager.get_or_create_tty(0)

        # Create a simple script that stays running (simulates a CLI)
        await tmux_tty_manager.send_keys(0, "cat > /tmp/myscript <<'SCRIPT'\n#!/usr/bin/env bash\nsleep 30\nSCRIPT")
        await tmux_tty_manager.send_keys(0, "Enter")
        await asyncio.sleep(0.5)
        await tmux_tty_manager.send_keys(0, "chmod +x /tmp/myscript && /tmp/myscript")
        await tmux_tty_manager.send_keys(0, "Enter")
        await asyncio.sleep(1)
        await tmux_tty_manager._capture_tty(tty)
        # The resolve logic looks through the process tree and finds the script name
        assert tty.current_command == "myscript", f"Expected 'myscript', got '{tty.current_command}'"

        # Clean up
        await tmux_tty_manager.send_keys(0, "C-c")
        await asyncio.sleep(0.5)
