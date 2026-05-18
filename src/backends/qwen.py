"""qwen-code backend. Runs `qwen` CLI against the local vLLM-served
Qwen3-Coder-14B-Instruct. Free, offline, sandboxed inside the workspace
directory."""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from ..config import settings
from .base import Backend, BackendResult

log = logging.getLogger("jarvis-agent.backend.qwen")


class QwenBackend(Backend):
    name = "qwen"

    async def run(self, task: str, workspace: Path, log_cb) -> BackendResult:
        # qwen-code reads OpenAI-compatible env vars: OPENAI_API_KEY (any value),
        # OPENAI_BASE_URL, OPENAI_MODEL. Set them per-call.
        env = {
            "OPENAI_BASE_URL": f"{settings.vllm_base}/v1",
            "OPENAI_API_KEY": "unused-local-vllm",
            "OPENAI_MODEL": settings.vllm_model,
        }

        # `qwen --yolo -p "<task>"` runs non-interactively, auto-approves
        # filesystem mutations within the cwd. We intentionally restrict cwd
        # to the per-task workspace so it can't reach outside.
        cmd = ["qwen", "--yolo", "-p", task]

        await log_cb("assistant_text", {"text": f"qwen-code starting: {' '.join(cmd[:2])} <task>"})

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(workspace),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env={**env, "PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/root"},
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
