"""qwen-code backend. Runs `qwen` CLI against the local vLLM-served
Qwen2.5-Coder-14B-Instruct-AWQ (or whatever VLLM_MODEL says). Free, offline,
sandboxed inside the workspace directory.

Skill packs (get-shit-done, antigravity-awesome-skills, superpowers) are
baked into the image at /root/.qwen/skills/ and discovered via QWEN_HOME."""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from ..config import settings
from .base import Backend, BackendResult

log = logging.getLogger("jarvis-agent.backend.qwen")


async def _inject_watcher(
    proc: asyncio.subprocess.Process,
    queue: asyncio.Queue,
) -> None:
    """Drain inject_queue into the subprocess stdin.

    Note: qwen --yolo runs fully autonomously and is unlikely to read stdin
    mid-task. Injection is best-effort — messages are written but may be
    silently ignored by the CLI. We keep this watcher alive until the process
    exits so that any future qwen-code version that does read stdin will work."""
    while proc.returncode is None:
        try:
            msg: str = await asyncio.wait_for(queue.get(), timeout=1.0)
            if proc.stdin and not proc.stdin.is_closing():
                proc.stdin.write((msg + "\n").encode())
                await proc.stdin.drain()
                log.debug("injected %d chars into qwen stdin", len(msg))
        except asyncio.TimeoutError:
            pass
        except Exception as exc:
            log.debug("inject watcher error (ignored): %s", exc)
            break


class QwenBackend(Backend):
    name = "qwen"

    async def run(
        self,
        task: str,
        workspace: Path,
        log_cb,
        inject_queue: asyncio.Queue | None = None,
    ) -> BackendResult:
        # qwen-code reads OpenAI-compatible env vars: OPENAI_API_KEY (any value),
        # OPENAI_BASE_URL, OPENAI_MODEL. Set them per-call.
        env = {
            "OPENAI_BASE_URL": f"{settings.vllm_base}/v1",
            "OPENAI_API_KEY": "unused-local-vllm",
            "OPENAI_MODEL": settings.vllm_model,
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "HOME": "/root",
            # Let qwen-code discover skill packs baked into the image.
            "QWEN_HOME": str(settings.qwen_home),
        }

        # `qwen --yolo -p "<task>"` runs non-interactively, auto-approves
        # filesystem mutations within the cwd. We intentionally restrict cwd
        # to the per-task workspace so it can't reach outside.
        cmd = ["qwen", "--yolo", "-p", task]

        await log_cb("assistant_text", {"text": f"qwen-code starting: {' '.join(cmd[:2])} <task>"})

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(workspace),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=env,
        )

        # Start inject watcher if a queue was provided
        watcher_task: asyncio.Task | None = None
        if inject_queue is not None:
            watcher_task = asyncio.create_task(
                _inject_watcher(proc, inject_queue),
                name=f"qwen-inject-{id(proc)}",
            )

        # Stream stdout line by line so progress lands in task_runs in near-real-time.
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

        full_log = "\n".join(captured[-40:])  # tail for the summary
        if rc != 0:
            return BackendResult(
                ok=False,
                summary=f"qwen-code exited {rc}",
                error=full_log[-1800:],
            )
        return BackendResult(
            ok=True,
            summary=f"qwen-code completed.\n\nLast output:\n```\n{full_log[-1500:]}\n```",
        )
