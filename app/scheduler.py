"""Library polling, debounce, and auto-refresh scheduling."""
import asyncio
import logging
from typing import Callable

logger = logging.getLogger(__name__)


class LibraryScheduler:
    """Polls the Plex library for changes and triggers refresh jobs with debounce."""

    def __init__(
        self,
        poll_interval: int,        # seconds between polls
        debounce_window: int,       # seconds to wait after last detected change
        on_change: Callable,        # async callback: called with new_updated_at
        get_updated_at: Callable,   # sync callable: returns current library updated_at int
    ):
        self.poll_interval = poll_interval
        self.debounce_window = debounce_window
        self._on_change = on_change
        self._get_updated_at = get_updated_at
        self._last_updated_at: int | None = None
        self._debounce_task: asyncio.Task | None = None
        self._pending_updated_at: int | None = None
        self._running = False

    async def run(self) -> None:
        """Main polling loop. Run as an asyncio task."""
        self._running = True
        logger.info(
            "Library scheduler started — polling every %ds, debounce %ds",
            self.poll_interval,
            self.debounce_window,
        )
        while self._running:
            await asyncio.sleep(self.poll_interval)
            try:
                updated_at = self._get_updated_at()
                if updated_at is None:
                    continue
                if self._last_updated_at is None:
                    self._last_updated_at = updated_at
                    continue
                if updated_at != self._last_updated_at:
                    logger.info("Library change detected (updated_at: %d → %d)", self._last_updated_at, updated_at)
                    self._last_updated_at = updated_at
                    self._pending_updated_at = updated_at
                    self._reset_debounce()
            except Exception:
                logger.exception("Error in library scheduler poll")

    def _reset_debounce(self) -> None:
        if self._debounce_task and not self._debounce_task.done():
            self._debounce_task.cancel()
        self._debounce_task = asyncio.create_task(self._debounce_fire())

    async def _debounce_fire(self) -> None:
        try:
            await asyncio.sleep(self.debounce_window)
            updated_at = self._pending_updated_at
            self._pending_updated_at = None
            logger.info("Debounce elapsed — triggering refresh (updated_at: %d)", updated_at)
            await self._on_change(updated_at)
        except asyncio.CancelledError:
            pass

    def stop(self) -> None:
        self._running = False
        if self._debounce_task and not self._debounce_task.done():
            self._debounce_task.cancel()
