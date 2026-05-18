"""Hermes backend. Routes to a second vLLM instance running a Hermes model.

Hermes-3 (NousResearch) is strong at instruction-following, creative planning,
and structured reasoning. Used by SmartBackend for design and planning tasks.

Requires a second vLLM process running on HERMES_BASE (default port 18001).
If unavailable, SmartBackend falls back to Claude or Qwen automatically."""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from ..config import settings
from .base import Backend, BackendResult

log = logging.getLogger("jarvis-agent.backend.hermes")


async def _inject_watcher(
    proc: asyncio.subprocess.Process,
    queue: asyncio.Queue,
) -> None:
    while proc.returncode is None:
        try:
            msg: str = await asyncio.wait_for(queue.get(), timeout=1.0)
            if proc.stdin and not proc.stdin.is_closing():
                proc.stdin.write((msg + "\n").encode())
                await proc.stdin.drain()
        except asyncio.TimeoutError:
            pass
        except Exception as exc:
            log.debug("hermes inject watcher error: %s", exc)
            break


class HermesBackend(Backend):
    name = "hermes"

    async def run(
        self,
        task: str,
        workspace: Path,
        log_cb,
        inject_queue: asyncio.Queue | None = None,
    ) -> BackendResult:
        # Hermes uses qwen-code CLI but pointed at the Hermes vLLM instance.
        env = {
            "OPENAI_BASE_URL": f"{settings.hermes_base}/v1",
            "OPENAI_API_KEY": "unused-local-hermes",
            "OPENAI_MODEL": settings.hermes_model,
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "HOME": "/root",
            "QWEN_HOME": str(settings.qwen_home),
        }

        cmd = ["qwen", "--yolo", "-p", task]

        await log_cb("assistant_text", {
            "text": f"hermes-backend starting (model={settings.hermes_model})"
        })

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(workspace),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=env,
            )
        except FileNotFoundError:
            return BackendResult(
                ok=False,
                summary="hermes backend unavailable",
                error="qwen CLI not found - cannot run hermes backend",
            )

        watcher_task: asyncio.Task | None = None
        if inject_queue is not None:
            watcher_task = asyncio.create_task(
                _inject_watcher(proc, inject_queue),
                name=f"hermes-inject-{id(proc)}",
            )

        captured: list[str] = []
        assert proc.stdout is not None
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            text = line.decode("utf-8", errors="replace").rstrip()
            captured.append(text)
            if text.strip():
                await log_cb("assistant_text", {"text": text[:500]})

        rc = await proc.wait()

        if watcher_task is not None:
            watcher_task.cancel()
            try:
                await watcher_task
            except asyncio.CancelledError:
                pass

        full_log = "\n".join(captured[-40:])
        if rc != 0:
            return BackendResult(
                ok=False,
                summary=f"hermes exited {rc}",
                error=full_log[-1800:],
            )
        return BackendResult(
            ok=True,
            summary=f"hermes completed.\n\nLast output:\n```\n{full_log[-1500:]}\n```",
        )
