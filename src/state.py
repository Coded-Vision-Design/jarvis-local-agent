"""In-flight concurrency tracker + live event bus + inject queues.

Plan caps concurrency at 1 claude + 1 qwen running at once so neither
sub-agent steals VRAM/CPU from the other and we don't blow through
Anthropic credits in a runaway loop.

StreamBus is a lightweight asyncio pub/sub that lets SSE endpoints
subscribe to log_cb events in real-time without any external broker."""
from __future__ import annotations

import asyncio
import time
from collections import deque
from typing import Any


class Slots:
    def __init__(self) -> None:
        self._claude = asyncio.Semaphore(1)
        self._qwen = asyncio.Semaphore(1)
        self._hermes = asyncio.Semaphore(1)

    def acquire(self, backend: str) -> asyncio.Semaphore:
        if backend == "claude":
            return self._claude
        if backend in ("qwen", "smart", "auto"):
            return self._qwen
        if backend == "hermes":
            return self._hermes
        raise ValueError(f"unknown backend: {backend}")

    def available(self, backend: str) -> bool:
        sem = self.acquire(backend)
        return getattr(sem, "_value", 0) > 0


class StreamBus:
    """Asyncio pub/sub for live task events.

    Tasks publish via put_event(); SSE endpoints subscribe via subscribe()
    and read from the returned Queue. Uses put_nowait so slow subscribers
    drop events rather than blocking the log_cb hot path.
    """

    def __init__(self) -> None:
        # per-task subscribers: task_id -> list[Queue]
        self._subs: dict[int, list[asyncio.Queue]] = {}
        # global subscribers (receive every event from every task)
        self._global_subs: list[asyncio.Queue] = []
        # currently running tasks: task_id -> metadata dict
        self._active: dict[int, dict[str, Any]] = {}
        # ring buffer of last 200 events for page-reload replay
        self._replay: deque[dict[str, Any]] = deque(maxlen=200)

    def task_started(self, task_id: int, meta: dict[str, Any]) -> None:
        self._active[task_id] = {**meta, "started_at": time.time()}
        self._subs.setdefault(task_id, [])

    def task_ended(self, task_id: int) -> None:
        self._active.pop(task_id, None)
        # keep queues alive for 60 s so a page reload can drain final events
        loop = asyncio.get_event_loop()
        loop.call_later(60, lambda: self._subs.pop(task_id, None))

    def active_tasks(self) -> dict[int, dict[str, Any]]:
        return dict(self._active)

    def replay(self) -> list[dict[str, Any]]:
        return list(self._replay)

    async def put_event(self, task_id: int, kind: str, payload: dict[str, Any]) -> None:
        """Publish an event. Never blocks — drops events for lagging subscribers."""
        event: dict[str, Any] = {
            "task_id": task_id,
            "kind": kind,
            "payload": payload,
            "ts": time.time(),
        }
        self._replay.append(event)
        for q in list(self._subs.get(task_id, [])):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass  # slow subscriber — drop rather than block log_cb
        for q in list(self._global_subs):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass

    def subscribe(self, task_id: int | None = None) -> asyncio.Queue:
        """Return a new Queue that will receive future events.
        Pass task_id=None for the global (all tasks) feed."""
        q: asyncio.Queue = asyncio.Queue(maxsize=500)
        if task_id is None:
            self._global_subs.append(q)
        else:
            self._subs.setdefault(task_id, []).append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue, task_id: int | None = None) -> None:
        lst = self._subs.get(task_id, []) if task_id is not None else self._global_subs
        try:
            lst.remove(q)
        except ValueError:
            pass


# ── Singletons ────────────────────────────────────────────────────────────────
slots = Slots()
bus = StreamBus()

# Per-task stdin injection queues.  runner.py creates one before calling
# backend.run() and removes it in finally.  backends drain it in a watcher task.
inject_queues: dict[int, asyncio.Queue] = {}
