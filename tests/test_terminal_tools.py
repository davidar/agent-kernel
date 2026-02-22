"""Tests for the terminal multiplexer tools (src/tools/terminal.py).

Integration tests require podman + agent-test image with tmux installed.
"""

from unittest.mock import AsyncMock, patch

import pytest


class TestPointAndCall:
    """Test the point-and-call safety check on the type tool."""

    @pytest.fixture
    def mock_manager(self, tmp_path):
        """Create a TTYManager with fake TTYs (no podman)."""
        from src.tty import TTY, TTYManager

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
        from src.tools.terminal import type_tool

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
        from src.tools.terminal import type_tool

        with (
            patch("src.tools.terminal.is_logged_in", return_value=True),
            patch("src.tools.terminal.get_tty_manager", return_value=mock_manager),
        ):
            mock_manager.send_keys = AsyncMock()
            result = await type_tool.handler({"tty": 1, "text": "bsky timeline", "expect": "bash"})
            assert result.get("is_error") is True
            assert "Point-and-call mismatch" in result["content"][0]["text"]
            assert "'bash'" in result["content"][0]["text"]
            assert "'chat'" in result["content"][0]["text"]
            assert not mock_manager.send_keys.called

    async def test_expect_case_insensitive(self, mock_manager):
        """Expect matching is case-insensitive."""
        from src.tools.terminal import type_tool

        with (
            patch("src.tools.terminal.is_logged_in", return_value=True),
            patch("src.tools.terminal.get_tty_manager", return_value=mock_manager),
        ):
            mock_manager.send_keys = AsyncMock()
            result = await type_tool.handler({"tty": 0, "text": "ls", "expect": "Bash"})
            assert result.get("is_error") is not True

    async def test_expect_required(self, mock_manager):
        """type() fails when expect is not provided."""
        from src.tools.terminal import type_tool

        with (
            patch("src.tools.terminal.is_logged_in", return_value=True),
            patch("src.tools.terminal.get_tty_manager", return_value=mock_manager),
        ):
            result = await type_tool.handler({"tty": 0, "text": "echo hello"})
            assert result.get("is_error") is True
            assert "expect is required" in result["content"][0]["text"]

    async def test_expect_empty_string_rejected(self, mock_manager):
        """type() fails when expect is an empty string."""
        from src.tools.terminal import type_tool

        with (
            patch("src.tools.terminal.is_logged_in", return_value=True),
            patch("src.tools.terminal.get_tty_manager", return_value=mock_manager),
        ):
            result = await type_tool.handler({"tty": 0, "text": "echo hello", "expect": ""})
            assert result.get("is_error") is True
            assert "expect is required" in result["content"][0]["text"]

    async def test_new_tty_skips_check(self, mock_manager):
        """For a TTY that doesn't exist yet, expect check is skipped (TTY will be auto-created)."""
        from src.tools.terminal import type_tool

        with (
            patch("src.tools.terminal.is_logged_in", return_value=True),
            patch("src.tools.terminal.get_tty_manager", return_value=mock_manager),
        ):
            mock_manager.send_keys = AsyncMock()
            # TTY 5 doesn't exist — should succeed regardless of expect value
            result = await type_tool.handler({"tty": 5, "text": "echo hello", "expect": "bash"})
            assert result.get("is_error") is not True

    async def test_expect_uses_current_command_over_command(self, tmp_path):
        """Expect checks against current_command, not the original creation command."""
        from src.tty import TTY, TTYManager
        from src.tools.terminal import type_tool

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
        from src.tty import TTY, TTYManager
        from src.tools.terminal import type_tool

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


class TestTerminalToolLogic:
    """Test the operations that the terminal tools perform."""

    @pytest.fixture
    async def tty_manager(self, tmp_path, test_container):
        from src.tty import TTYManager

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
        import asyncio

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
        import asyncio

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
        import asyncio

        tty = await tty_manager.get_or_create_tty(0)
        await tty_manager.send_keys(0, "echo logfile-test")
        await tty_manager.send_keys(0, "Enter")
        await asyncio.sleep(1)
        await tty_manager._capture_tty(tty)
        assert tty.screen_file.exists()
        assert tty.scrollback_file.exists()
        assert tty.status_file.exists()
