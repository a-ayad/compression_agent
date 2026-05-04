"""Per-job progress events with broadcast subscriptions for SSE streaming."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import dataclass, field, asdict
from typing import Any, Optional


@dataclass
class JobEvent:
    type: str               # "stage" | "progress" | "log" | "result" | "error" | "done"
    stage: str = ""         # "queued" | "analyzing" | "searching" | "encoding" | "measuring" | "done"
    percent: Optional[float] = None
    message: str = ""
    data: Any = None
    ts: float = field(default_factory=lambda: time.time())

    def to_sse(self) -> str:
        d = {"type": self.type, "ts": self.ts}
        if self.stage:
            d["stage"] = self.stage
        if self.percent is not None:
            d["percent"] = self.percent
        if self.message:
            d["message"] = self.message
        if self.data is not None:
            d["data"] = self.data
        return f"data: {json.dumps(d)}\n\n"


class JobState:
    def __init__(self, job_id: str):
        self.job_id = job_id
        self.history: list[JobEvent] = []
        self.finished = False
        self.error: Optional[str] = None
        self.result: Optional[dict] = None
        self._listeners: set[asyncio.Queue[JobEvent]] = set()

    async def emit(self, event: JobEvent) -> None:
        self.history.append(event)
        if event.type == "done":
            self.finished = True
            if isinstance(event.data, dict):
                self.result = event.data
        elif event.type == "error":
            self.finished = True
            self.error = event.message
        for q in list(self._listeners):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass

    def subscribe(self) -> tuple[asyncio.Queue[JobEvent], list[JobEvent]]:
        """Atomically: register listener AND snapshot history.
        Caller must replay history first, then drain queue."""
        q: asyncio.Queue[JobEvent] = asyncio.Queue(maxsize=2048)
        # No await between these two lines → no race window
        self._listeners.add(q)
        snapshot = list(self.history)
        return q, snapshot

    def unsubscribe(self, q: asyncio.Queue[JobEvent]) -> None:
        self._listeners.discard(q)


class JobRegistry:
    def __init__(self):
        self._jobs: dict[str, JobState] = {}

    def new_job(self) -> JobState:
        job_id = uuid.uuid4().hex[:12]
        st = JobState(job_id)
        self._jobs[job_id] = st
        return st

    def get(self, job_id: str) -> Optional[JobState]:
        return self._jobs.get(job_id)


registry = JobRegistry()
