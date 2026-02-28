"""Tests for the terminal multiplexer tools (src/tools/terminal.py).

Integration tests require podman + agent-test image with tmux installed.
"""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from src.tools.awareness import check_tick_end_conditions
from src.tools.terminal import open_tool, type_tool
from src.tty import TTY, TTYManager


class TestPointAndCall:
    """Test the point-and-call safety check on the type tool."""

    @pytest.fixture
    def mock_manager(self, tmp_path):
        """Create a TTYManager with fake TTYs (no podman)."""
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        mgr = TTYManager(sessions_dir=sessions_dir)

        # TTY 0: running bash
        tty0 = TTY(0, sessions_dir)
        tty0.command = "bash"
        tty0.current_command = "bash"
        tty0.previous_lines = ["$ "]
        tty0.mark_seen()
        mgr.ttys[0] = tty0

        # TTY 1: running chat
        tty1 = TTY(1, sessions_dir)
        tty1.command = "bash"
        tty1.current_command = "chat"
        tty1.previous_lines = ["[chat] "]
        tty1.mark_seen()
        mgr.ttys[1] = tty1

        return mgr

    async def test_expect_matches_allows_send(self, mock_manager):
        """type() succeeds when expect matches the running command."""
        with (
            patch("src.tools.terminal.is_logged_in", return_value=True),
            patch("src.tools.terminal.get_tty_manager", return_value=mock_manager),
        ):
            mock_manager.send_keys = AsyncMock()
            result = await type_tool.handler({"tty": 0, "text": "echo hello", "expect": "bash"})
            assert result.get("is_error") is not True
            assert mock_manager.send_keys.called

    async def test_expect_mismatch_blocks_send(self, mock_manager):
        """type() fails when expect doesn't match the running command."""
        with (
            patch("src.tools.terminal.is_logged_in", return_value=True),
            patch("src.tools.terminal.get_tty_manager", return_value=mock_manager),
        ):
            mock_manager.send_keys = AsyncMock()
            result = await type_tool.handler({"tty": 1, "text": "bsky timeline", "expect": "bash"})
            assert result.get("is_error") is True
            assert "Point-and-call mismatch" in result["content"][0]["text"]
            assert "terminal 1" in result["content"][0]["text"]
            assert "'bash'" in result["content"][0]["text"]
            assert "'chat'" in result["content"][0]["text"]
            assert not mock_manager.send_keys.called

    async def test_expect_case_insensitive(self, mock_manager):
        """Expect matching is case-insensitive."""
        with (
            patch("src.tools.terminal.is_logged_in", return_value=True),
            patch("src.tools.terminal.get_tty_manager", return_value=mock_manager),
        ):
            mock_manager.send_keys = AsyncMock()
            result = await type_tool.handler({"tty": 0, "text": "ls", "expect": "Bash"})
            assert result.get("is_error") is not True

    async def test_expect_required(self, mock_manager):
        """type() fails when expect is not provided."""
        with (
            patch("src.tools.terminal.is_logged_in", return_value=True),
            patch("src.tools.terminal.get_tty_manager", return_value=mock_manager),
        ):
            result = await type_tool.handler({"tty": 0, "text": "echo hello"})
            assert result.get("is_error") is True
            assert "expect is required" in result["content"][0]["text"]

    async def test_expect_empty_string_rejected(self, mock_manager):
        """type() fails when expect is an empty string."""
        with (
            patch("src.tools.terminal.is_logged_in", return_value=True),
            patch("src.tools.terminal.get_tty_manager", return_value=mock_manager),
        ):
            result = await type_tool.handler({"tty": 0, "text": "echo hello", "expect": ""})
            assert result.get("is_error") is True
            assert "expect is required" in result["content"][0]["text"]

    async def test_nonexistent_terminal_rejected(self, mock_manager):
        """type() fails when the terminal doesn't exist (must use open() first)."""
        with (
            patch("src.tools.terminal.is_logged_in", return_value=True),
            patch("src.tools.terminal.get_tty_manager", return_value=mock_manager),
        ):
            mock_manager.send_keys = AsyncMock()
            result = await type_tool.handler({"tty": 5, "text": "echo hello", "expect": "bash"})
            assert result.get("is_error") is True
            assert "does not exist" in result["content"][0]["text"]
            assert "open()" in result["content"][0]["text"]
            assert not mock_manager.send_keys.called

    async def test_expect_uses_current_command_over_command(self, tmp_path):
        """Expect checks against current_command, not the original creation command."""
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        mgr = TTYManager(sessions_dir=sessions_dir)
        tty = TTY(0, sessions_dir)
        tty.command = "bash"  # Created as bash
        tty.current_command = "python3"  # But now running python3
        tty.previous_lines = [">>> "]
        tty.mark_seen()
        mgr.ttys[0] = tty

        with (
            patch("src.tools.terminal.is_logged_in", return_value=True),
            patch("src.tools.terminal.get_tty_manager", return_value=mgr),
        ):
            mgr.send_keys = AsyncMock()
            # Should fail: expect bash but running python3
            result = await type_tool.handler({"tty": 0, "text": "import os", "expect": "bash"})
            assert result.get("is_error") is True
            assert "python3" in result["content"][0]["text"]

            # Should succeed: expect python3
            result = await type_tool.handler({"tty": 0, "text": "import os", "expect": "python3"})
            assert result.get("is_error") is not True

    async def test_expect_falls_back_to_command_when_no_current(self, tmp_path):
        """When current_command is empty, expect checks against command."""
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        mgr = TTYManager(sessions_dir=sessions_dir)
        tty = TTY(0, sessions_dir)
        tty.command = "bash"
        tty.current_command = ""  # Not yet detected
        tty.previous_lines = ["$ "]
        tty.mark_seen()
        mgr.ttys[0] = tty

        with (
            patch("src.tools.terminal.is_logged_in", return_value=True),
            patch("src.tools.terminal.get_tty_manager", return_value=mgr),
        ):
            mgr.send_keys = AsyncMock()
            result = await type_tool.handler({"tty": 0, "text": "ls", "expect": "bash"})
            assert result.get("is_error") is not True


class TestOpenTool:
    """Test the open tool."""

    @pytest.fixture
    def mock_manager(self, tmp_path):
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        return TTYManager(sessions_dir=sessions_dir)

    async def test_open_returns_terminal_number(self, mock_manager):
        """open() returns the terminal number and capacity."""
        with (
            patch("src.tools.terminal.is_logged_in", return_value=True),
            patch("src.tools.terminal.get_tty_manager", return_value=mock_manager),
        ):
            mock_manager.get_or_create_tty = AsyncMock()
            result = await open_tool.handler({})
            assert result.get("is_error") is not True
            text = result["content"][0]["text"]
            assert "Opened terminal 0" in text
            assert "more available" in text

    async def test_open_with_command(self, mock_manager):
        """open(command='python3') passes the command through."""
        with (
            patch("src.tools.terminal.is_logged_in", return_value=True),
            patch("src.tools.terminal.get_tty_manager", return_value=mock_manager),
        ):
            mock_manager.get_or_create_tty = AsyncMock()
            result = await open_tool.handler({"command": "python3"})
            assert result.get("is_error") is not True
            assert "python3" in result["content"][0]["text"]
            mock_manager.get_or_create_tty.assert_called_once_with(0, command="python3")

    async def test_open_skips_occupied_ids(self, mock_manager):
        """open() finds the next available ID."""
        # Occupy terminal 0
        tty0 = TTY(0, mock_manager.sessions_dir)
        mock_manager.ttys[0] = tty0

        with (
            patch("src.tools.terminal.is_logged_in", return_value=True),
            patch("src.tools.terminal.get_tty_manager", return_value=mock_manager),
        ):
            mock_manager.get_or_create_tty = AsyncMock()
            result = await open_tool.handler({})
            assert result.get("is_error") is not True
            assert "Opened terminal 1" in result["content"][0]["text"]

    async def test_open_requires_login(self, mock_manager):
        """open() fails if login() hasn't been called."""
        with (
            patch("src.tools.terminal.is_logged_in", return_value=False),
            patch("src.tools.terminal.get_tty_manager", return_value=mock_manager),
        ):
            result = await open_tool.handler({})
            assert result.get("is_error") is True
            assert "login()" in result["content"][0]["text"]


class TestTickEndConditions:
    """Test that tick-end checks ignore dead TTYs."""

    def _make_manager(self, tmp_path):
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        return TTYManager(sessions_dir=sessions_dir)

    def test_dead_tty_does_not_block_tick_end(self, tmp_path):
        """Dead TTYs don't appear in tick-end blocking issues."""
        mgr = self._make_manager(tmp_path)
        tty = TTY(0, mgr.sessions_dir)
        tty.process_dead = True
        tty.exit_code = 0
        mgr.ttys[0] = tty

        with (
            patch("src.tools.awareness.is_logged_in", return_value=True),
            patch("src.tools.awareness._tick") as mock_tick,
            patch("src.tools.awareness.get_tty_manager", return_value=mgr),
        ):
            mock_tick.logged_in = True
            issues = check_tick_end_conditions()
            assert issues == []

    def test_live_tty_blocks_tick_end(self, tmp_path):
        """Live TTYs still block tick end."""
        mgr = self._make_manager(tmp_path)
        tty = TTY(0, mgr.sessions_dir)
        tty.process_dead = False
        mgr.ttys[0] = tty

        with (
            patch("src.tools.awareness.is_logged_in", return_value=True),
            patch("src.tools.awareness._tick") as mock_tick,
            patch("src.tools.awareness.get_tty_manager", return_value=mgr),
        ):
            mock_tick.logged_in = True
            issues = check_tick_end_conditions()
            assert len(issues) == 1
            assert "Open terminals" in issues[0]

    def test_mixed_live_and_dead(self, tmp_path):
        """Only live TTYs are listed in tick-end issues."""
        mgr = self._make_manager(tmp_path)

        dead = TTY(0, mgr.sessions_dir)
        dead.process_dead = True
        dead.exit_code = 0
        mgr.ttys[0] = dead

        alive = TTY(1, mgr.sessions_dir)
        alive.process_dead = False
        mgr.ttys[1] = alive

        with (
            patch("src.tools.awareness.is_logged_in", return_value=True),
            patch("src.tools.awareness._tick") as mock_tick,
            patch("src.tools.awareness.get_tty_manager", return_value=mgr),
        ):
            mock_tick.logged_in = True
            issues = check_tick_end_conditions()
            assert len(issues) == 1
            assert "1" in issues[0]
            assert "0" not in issues[0]


class TestTerminalToolLogic:
    """Test the operations that the terminal tools perform."""

    @pytest.fixture
    async def tty_manager(self, tmp_path, test_container):
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        mgr = TTYManager(sessions_dir=sessions_dir, container_name=test_container)
        mgr.sessions_dir.mkdir(parents=True, exist_ok=True)
        yield mgr
        await mgr.close_all()

    async def test_send_keys_creates_tty(self, tty_manager):
        """send_keys auto-creates a TTY."""
        await tty_manager.send_keys(0, "echo hello")
        assert 0 in tty_manager.ttys

    async def test_send_keys_literal(self, tty_manager):
        """Literal text is sent via send-keys -l."""
        await tty_manager.get_or_create_tty(0)
        await tty_manager.send_keys(0, "echo keytest")
        await tty_manager.send_keys(0, "Enter")
        await asyncio.sleep(1)
        tty = tty_manager.ttys[0]
        await tty_manager._capture_tty(tty)
        assert any("keytest" in line for line in tty.previous_lines)

    async def test_send_keys_special(self, tty_manager):
        """Special keys (Enter, C-c) are recognized."""
        await tty_manager.get_or_create_tty(0)
        # Should not raise
        await tty_manager.send_keys(0, "Enter")
        await tty_manager.send_keys(0, "C-c")

    async def test_has_unseen_changes(self, tty_manager):
        """has_unseen_changes detects new TTY output."""
        tty = await tty_manager.get_or_create_tty(0)
        # After creation, everything is marked as seen
        assert tty_manager.has_unseen_changes() is False

        await tty_manager.send_keys(0, "echo unseen-test")
        await tty_manager.send_keys(0, "Enter")
        await asyncio.sleep(1)
        await tty_manager._capture_tty(tty)
        assert tty_manager.has_unseen_changes() is True

        # Mark as seen
        tty.mark_seen()
        assert tty_manager.has_unseen_changes() is False

    async def test_wait_for_activity_timeout(self, tty_manager):
        """wait_for_activity returns after timeout with no activity."""
        await tty_manager.get_or_create_tty(0)
        # Mark everything as seen
        tty_manager.ttys[0].mark_seen()
        # Wait should timeout quickly and report no changes
        result = await tty_manager.wait_for_activity(timeout=1)
        assert "no change" in result or "timeout" in result.lower()

    async def test_close_tty(self, tty_manager):
        """Closing a TTY works correctly."""
        await tty_manager.get_or_create_tty(0)
        result = await tty_manager.close_tty(0)
        assert result is True
        assert 0 not in tty_manager.ttys

    async def test_tty_session_files_created(self, tty_manager):
        """Session files are created for each TTY."""
        tty = await tty_manager.get_or_create_tty(0)
        await tty_manager.send_keys(0, "echo logfile-test")
        await tty_manager.send_keys(0, "Enter")
        await asyncio.sleep(1)
        await tty_manager._capture_tty(tty)
        assert tty.screen_file.exists()
        assert tty.scrollback_file.exists()
        assert tty.status_file.exists()
