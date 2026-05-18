"""Common Backend interface. Each backend wraps a CLI sub-agent
(claude-code or qwen-code) and exposes a single async `run` method that
executes the task inside a checked-out workspace and returns a summary."""
from __future__ import annotations

import abc
import asyncio
from dataclasses import dataclass
from pathlib import Path


@dataclass
class BackendResult:
    ok: bool
    summary: str  # human-readable, will appear in the Discord post
    spent_pence: int | None = None
    spent_tokens: int | None = None
    error: str | None = None


class Backend(abc.ABC):
    name: str = "abstract"

    @abc.abstractmethod
    async def run(
        self,
        task: str,
        workspace: Path,
        log_cb,  # async (kind: str, payload: dict) -> None
        inject_queue: asyncio.Queue | None = None,
    ) -> BackendResult:
        """Execute `task` inside `workspace`. Stream progress via `log_cb`.

        `workspace` is the absolute path inside the container (under
        /workspace/workspaces/<repo>) on the cloned + checked-out branch.

        `inject_queue` is an optional asyncio.Queue whose items (strings) will
        be written to the subprocess stdin as they arrive. Best-effort — CLIs
        in fully autonomous mode (--yolo / --dangerously-skip-permissions) may
        not read stdin at all."""
