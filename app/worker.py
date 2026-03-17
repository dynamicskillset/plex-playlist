"""Background job worker — single asyncio.Queue, sequential processing."""
import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


class JobType(str, Enum):
    CREATE_PLAYLIST = "create_playlist"
    REFRESH_PLAYLIST = "refresh_playlist"
    FULL_REGENERATE = "full_regenerate_playlist"
    PROMPT_EDIT = "prompt_edit_regenerate"
    REFRESH_CYCLE = "refresh_cycle"
    INTEGRITY_AUDIT = "integrity_audit"


@dataclass
class Job:
    type: JobType
    payload: dict = field(default_factory=dict)
    # SSE channel key for streaming progress back to the browser
    sse_key: str | None = None


class JobQueue:
    """Single asyncio.Queue. One job runs at a time."""

    def __init__(self):
        self._queue: asyncio.Queue[Job] = asyncio.Queue()
        self._current: Job | None = None
        self._refresh_cycle_queued = False
        # progress callbacks: sse_key → list of message strings
        self._progress: dict[str, list[str]] = {}
        self._progress_events: dict[str, asyncio.Event] = {}

    def status(self) -> dict:
        return {
            "current": self._current.type if self._current else None,
            "queued": self._queue.qsize(),
        }

    def is_busy(self) -> bool:
        return self._current is not None or not self._queue.empty()

    def enqueue(self, job: Job) -> bool:
        """Add job to queue. Returns False if dropped (duplicate refresh_cycle)."""
        if job.type == JobType.REFRESH_CYCLE:
            if self._refresh_cycle_queued:
                logger.debug("Dropped duplicate refresh_cycle job")
                return False
            self._refresh_cycle_queued = True
        if job.sse_key:
            self._progress[job.sse_key] = []
            self._progress_events[job.sse_key] = asyncio.Event()
        self._queue.put_nowait(job)
        return True

    def emit(self, sse_key: str, message: str) -> None:
        """Append a progress message for SSE consumers."""
        if sse_key not in self._progress:
            self._progress[sse_key] = []
        self._progress[sse_key].append(message)
        if sse_key in self._progress_events:
            self._progress_events[sse_key].set()

    async def stream(self, sse_key: str):
        """Async generator yielding SSE-formatted progress messages."""
        sent = 0
        while True:
            messages = self._progress.get(sse_key, [])
            while sent < len(messages):
                msg = messages[sent]
                sent += 1
                yield f"data: {msg}\n\n"
                if msg.startswith("Done") or msg.startswith("Failed") or msg.startswith("Completed"):
                    return
            event = self._progress_events.get(sse_key)
            if event:
                event.clear()
                try:
                    await asyncio.wait_for(event.wait(), timeout=30)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"

    async def run(self, handlers: dict[JobType, Any]) -> None:
        """Main worker loop. Call this as an asyncio task."""
        logger.info("Job queue worker started")
        while True:
            job = await self._queue.get()
            if job.type == JobType.REFRESH_CYCLE:
                self._refresh_cycle_queued = False
            self._current = job
            logger.info("Processing job: %s", job.type)
            handler = handlers.get(job.type)
            if handler:
                try:
                    await handler(job)
                except Exception:
                    logger.exception("Unhandled error in job %s", job.type)
            else:
                logger.warning("No handler for job type: %s", job.type)
            self._current = None
            self._queue.task_done()


# Global singleton
queue = JobQueue()
