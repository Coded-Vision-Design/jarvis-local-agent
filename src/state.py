"""In-flight concurrency tracker. Plan caps it at 1 claude + 1 qwen running
at once so neither sub-agent steals VRAM/CPU from the other and we don't
blow through Anthropic credits in a runaway loop."""
from __future__ import annotations

import asyncio


class Slots:
    def __init__(self) -> None:
        self._claude = asyncio.Semaphore(1)
        self._qwen = asyncio.Semaphore(1)

    def acquire(self, backend: str) -> asyncio.Semaphore:
        if backend == "claude":
            return self._claude
        if backend == "qwen":
            return self._qwen
        raise ValueError(f"unknown backend: {backend}")

    def available(self, backend: str) -> bool:
        sem = self.acquire(backend)
        # ._value is the standard CPython attribute; safe enough for a status probe.
        return getattr(sem, "_value", 0) > 0


slots = Slots()
