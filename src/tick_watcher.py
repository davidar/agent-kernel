"""Mid-tick notification system.

Watches for notification files and delivers them to the agent via
client.query() during an active tick.

Session/TTY output watching is handled by the TTY manager's capture loop
and SDK hooks (PostToolUse, UserPromptSubmit, Stop) — not here.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path

from watchfiles import awatch, Change

from .config import data_dir
from .logging_config import get_logger

logger = get_logger(__name__)

POLL_INTERVAL = 5.0  # Notification directory polling interval (seconds)


class TickWatcher:
    """Manages mid-tick notification delivery.

    Watches notification files and dispatches via client.query().
    TTY output is handled by the capture loop + hooks.
    """

    def __init__(
        self,
        notify_callback: Callable[[str], Awaitable[object]],
    ) -> None:
        self._notify_callback = notify_callback
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._notifications_dir = data_dir() / "system" / "notifications"
        self._poll_interval = POLL_INTERVAL
        self._tasks: list[asyncio.Task[None]] = []
        self._running = False

    @property
    def running(self) -> bool:
        """Whether the watcher is currently running."""
        return self._running

    async def start(self) -> None:
        """Launch all background watcher tasks."""
        self._running = True
        self._tasks = [
            asyncio.create_task(self._dispatch_loop()),
            asyncio.create_task(self._watch_notifications()),
        ]

    async def stop(self) -> None:
        """Cancel all tasks and drain the queue."""
        self._running = False
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._tasks.clear()

    async def _dispatch_loop(self) -> None:
        """Drain the notification queue and deliver via callback."""
        while self._running:
            try:
                msg = await asyncio.wait_for(self._queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            try:
                await self._notify_callback(msg)
            except Exception:
                logger.exception("Failed to deliver notification")

    async def _watch_notifications(self) -> None:
        """Watch notification directory for files.

        Uses inotify (via watchfiles) with polling fallback.
        """
        self._notifications_dir.mkdir(parents=True, exist_ok=True)

        # Consume pre-existing notifications (written between trigger and tick start)
        for f in sorted(self._notifications_dir.glob("*.txt")):
            self._consume_notification(f)

        # Try inotify first
        try:
            inotify_task = asyncio.create_task(self._inotify_notifications(awatch, Change))
            poll_task = asyncio.create_task(self._poll_notifications())
            # Run both — inotify catches fast, polling catches stragglers
            await asyncio.gather(inotify_task, poll_task)
        except ImportError:
            logger.warning("watchfiles not available, using polling only")
            await self._poll_notifications()

    async def _inotify_notifications(self, awatch, Change) -> None:
        """Watch notification dir via inotify."""
        try:
            async for changes in awatch(self._notifications_dir, stop_event=self._make_stop_event()):
                if not self._running:
                    break
                for change_type, path_str in changes:
                    if change_type != Change.added:
                        continue
                    path = Path(path_str)
                    if path.suffix != ".txt":
                        continue
                    self._consume_notification(path)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("inotify notification loop error")

    async def _poll_notifications(self) -> None:
        """Poll notification directory as fallback."""
        seen: set[str] = set()

        while self._running:
            await asyncio.sleep(self._poll_interval)
            if not self._running:
                break
            try:
                for f in self._notifications_dir.glob("*.txt"):
                    if f.name not in seen:
                        seen.add(f.name)
                        self._consume_notification(f)
            except Exception:
                logger.exception("Notification poll error")

    def _consume_notification(self, path: Path) -> None:
        """Read a notification file, enqueue it verbatim, and delete."""
        try:
            content = path.read_text().strip()
            path.unlink(missing_ok=True)
        except OSError:
            return

        if not content:
            return

        logger.info("Notification: %s", content[:80])
        self._queue.put_nowait(content)

    def _make_stop_event(self) -> asyncio.Event:
        """Create an event for watchfiles to check."""
        event = asyncio.Event()
        return event
