"""Tests for the tick watcher (src/tick_watcher.py)."""

import asyncio

import pytest


@pytest.fixture
def watcher_env(data_dir):
    """Set up environment for tick watcher tests."""
    import src.config

    src.config.init(data_dir)
    return data_dir


def _write_notification(notifications_dir, filename, content="[New message from testuser]"):
    """Helper to write a notification file."""
    (notifications_dir / filename).write_text(content)


class TestTickWatcherDispatchLoop:
    async def test_queued_message_delivered(self, watcher_env):
        """Messages on the queue are delivered via callback."""
        from src.tick_watcher import TickWatcher

        delivered = []

        async def mock_callback(msg):
            delivered.append(msg)

        watcher = TickWatcher(notify_callback=mock_callback)
        await watcher.start()

        watcher._queue.put_nowait("[session:sh] command completed")
        # Give dispatch loop time to process
        await asyncio.sleep(0.2)

        await watcher.stop()
        assert len(delivered) == 1
        assert "[session:sh] command completed" in delivered[0]

    async def test_multiple_messages_delivered_in_order(self, watcher_env):
        """Multiple queued messages are delivered in order."""
        from src.tick_watcher import TickWatcher

        delivered = []

        async def mock_callback(msg):
            delivered.append(msg)

        watcher = TickWatcher(notify_callback=mock_callback)
        await watcher.start()

        watcher._queue.put_nowait("first")
        watcher._queue.put_nowait("second")
        watcher._queue.put_nowait("third")

        await asyncio.sleep(0.5)
        await watcher.stop()

        assert delivered == ["first", "second", "third"]


class TestTickWatcherNotificationFiles:
    async def test_poll_detects_new_notification(self, watcher_env):
        """Polling detects new notification files."""
        from src.tick_watcher import TickWatcher

        delivered = []

        async def mock_callback(msg):
            delivered.append(msg)

        notifications_dir = watcher_env / "system" / "notifications"
        watcher = TickWatcher(notify_callback=mock_callback)
        watcher._notifications_dir = notifications_dir
        watcher._poll_interval = 0.2
        await watcher.start()

        # Wait a beat for poll to seed its seen set
        await asyncio.sleep(0.3)

        # Drop a notification after watcher starts
        _write_notification(notifications_dir, "msg-001.txt", content="[New message from alice]")

        # Wait for inotify or poll to detect it
        await asyncio.sleep(1.0)
        await watcher.stop()

        assert len(delivered) == 1
        assert delivered[0] == "[New message from alice]"

    async def test_preexisting_notifications_consumed(self, watcher_env):
        """Notification files present at startup are consumed and delivered."""
        from src.tick_watcher import TickWatcher

        delivered = []

        async def mock_callback(msg):
            delivered.append(msg)

        notifications_dir = watcher_env / "system" / "notifications"

        # Pre-existing notification (should be consumed at startup)
        _write_notification(notifications_dir, "old-msg.txt", content="[Pre-existing message]")

        watcher = TickWatcher(notify_callback=mock_callback)
        watcher._notifications_dir = notifications_dir
        watcher._poll_interval = 0.2
        await watcher.start()

        # Wait for dispatch loop to deliver
        await asyncio.sleep(0.5)
        await watcher.stop()

        assert len(delivered) == 1
        assert delivered[0] == "[Pre-existing message]"
        # File should have been deleted
        assert not (notifications_dir / "old-msg.txt").exists()

    async def test_notification_file_consumed(self, watcher_env):
        """Notification files are deleted after being consumed."""
        from src.tick_watcher import TickWatcher

        async def noop(msg):
            pass

        notifications_dir = watcher_env / "system" / "notifications"
        watcher = TickWatcher(notify_callback=noop)
        watcher._notifications_dir = notifications_dir
        watcher._poll_interval = 0.2
        await watcher.start()

        # Wait a beat for poll to seed its seen set
        await asyncio.sleep(0.3)

        notif_file = notifications_dir / "new-msg.txt"
        _write_notification(notifications_dir, "new-msg.txt", content="[New message from alice]")

        await asyncio.sleep(1.0)
        await watcher.stop()

        # File should have been deleted
        assert not notif_file.exists()


class TestTickWatcherLifecycle:
    async def test_start_stop(self, watcher_env):
        """Watcher starts and stops cleanly."""
        from src.tick_watcher import TickWatcher

        async def noop(msg):
            pass

        watcher = TickWatcher(notify_callback=noop)
        await watcher.start()
        assert watcher._running
        assert len(watcher._tasks) == 2

        await watcher.stop()
        assert not watcher._running
        assert len(watcher._tasks) == 0

    async def test_stop_is_idempotent(self, watcher_env):
        """Calling stop() twice doesn't error."""
        from src.tick_watcher import TickWatcher

        async def noop(msg):
            pass

        watcher = TickWatcher(notify_callback=noop)
        await watcher.start()
        await watcher.stop()
        await watcher.stop()  # Should not raise

    async def test_callback_error_doesnt_crash_dispatch(self, watcher_env):
        """If callback raises, dispatch loop continues."""
        from src.tick_watcher import TickWatcher

        call_count = 0

        async def failing_callback(msg):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("simulated failure")

        watcher = TickWatcher(notify_callback=failing_callback)
        await watcher.start()

        watcher._queue.put_nowait("msg1")  # Will fail
        watcher._queue.put_nowait("msg2")  # Should still be delivered

        await asyncio.sleep(0.5)
        await watcher.stop()

        assert call_count == 2
