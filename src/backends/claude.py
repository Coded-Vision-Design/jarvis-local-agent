"""Claude Code headless backend. Mounts the user's ~/.claude read-only so
MEMORY.md and skill library come along for the ride. Uses --bare to avoid
loading the host's MCP settings.json (those reference Windows paths)."""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from .base import Backend, BackendResult

log = logging.getLogger("jarvis-agent.backend.claude")


class ClaudeBackend(Backend):
    name = "claude"

    async def run(self, task: str, workspace: Path, log_cb) -> BackendResult:
        memory_path = Path("/root/.claude/projects/c--xampp-htdocs-Jarvis/memory/MEMORY.md")
        global_memory_args: list[str] = []
        if memory_path.exists():
            global_memory_args = ["--append-system-prompt", memory_path.read_text(encoding="utf-8")]

        # `claude -p "<task>"` runs in non-interactive print mode.
        # --output-format=stream-json gives us structured progress to push into task_runs.
        # --dangerously-skip-permissions is the headless equivalent of --yolo; we accept
        #   the risk because the workspace is sandboxed to /workspace/workspaces/<repo>.
        cmd = [
            "claude",
            "-p", task,
            "--output-format", "stream-json",
            "--verbose",
            "--dangerously-skip-permissions",
            *global_memory_args,
        ]

        env = {
            **os.environ,
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "HOME": "/root",
            "CLAUDE_CONFIG_DIR": "/root/.claude",  # read-only mount
        }

        await log_cb("assistant_text", {"text": "claude-code starting"})

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(workspace),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )

        captured: list[str] = []
        spent_pence_total = 0
        spent_tokens_total = 0

        assert proc.stdout is not None
        async for raw in proc.stdout:
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            captured.append(line)
            # stream-json emits one JSON object per line. We don't strictly
            # need to parse them all — just surface anything that looks like
            # a tool_use or message_delta as a run row.
            try:
                import json
                obj = json.loads(line)
                t = obj.get("type")
                if t == "tool_use":
                    await log_cb("tool_call", {
                        "tool": obj.get("name"),
                        "args": obj.get("input"),
                    })
                elif t == "message" or t == "content_block_delta":
                    text = obj.get("delta", {}).get("text") or obj.get("text") or ""
                    if text.strip():
                        await log_cb("assistant_text", {"text": text[:500]})
                # rough cost tally if Claude Code reports usage
                usage = obj.get("usage") or {}
                if usage:
                    spent_tokens_total += int(usage.get("input_tokens", 0)) + int(usage.get("output_tokens", 0))
            except Exception:
                # Not JSON or unexpected shape — just keep the raw line in capture.
                pass

        rc = await proc.wait()
        full_log = "\n".join(captured[-30:])
        if rc != 0:
            stderr_text = ""
            if proc.stderr:
                stderr_text = (await proc.stderr.read()).decode("utf-8", errors="replace")
            return BackendResult(
                ok=False,
                summary=f"claude-code exited {rc}",
                error=(stderr_text or full_log)[-1800:],
                spent_tokens=spent_tokens_total or None,
            )
        return BackendResult(
            ok=True,
            summary=f"claude-code completed.\n\nFinal output:\n```\n{full_log[-1500:]}\n```",
            spent_tokens=spent_tokens_total or None,
            spent_pence=spent_pence_total or None,
        )
