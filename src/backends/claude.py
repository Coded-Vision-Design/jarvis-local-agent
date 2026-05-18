"""Claude Code headless backend. Mounts the user's ~/.claude read-only so
MEMORY.md and skill library come along for the ride. Uses --bare to avoid
loading the host's MCP settings.json (those reference Windows paths)."""
from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path

from .base import Backend, BackendResult

log = logging.getLogger("jarvis-agent.backend.claude")


async def _inject_watcher(
    proc: asyncio.subprocess.Process,
    queue: asyncio.Queue,
) -> None:
    """Drain inject_queue into the subprocess stdin.

    Claude's headless `-p` mode may read stdin for mid-task interrupts, but
    this behaviour is undocumented. Injection is best-effort — messages are
    written but may be silently ignored. We keep this watcher alive until the
    process exits so future Claude Code versions that do read stdin will work."""
    while proc.returncode is None:
        try:
            msg: str = await asyncio.wait_for(queue.get(), timeout=1.0)
            if proc.stdin and not proc.stdin.is_closing():
                proc.stdin.write((msg + "\n").encode())
                await proc.stdin.drain()
                log.debug("injected %d chars into claude stdin", len(msg))
        except asyncio.TimeoutError:
            pass
        except Exception as exc:
            log.debug("inject watcher error (ignored): %s", exc)
            break


# Markers that indicate Claude Code couldn't proceed because the auth path
# is rate-limited. Subscription rate limits surface differently from API
# rate limits; the CLI typically prints a clear English line either way.
# Phase 18 quota heuristic — module-level counter so /health can show
# "how often has Claude subscription been rate-limited recently?" The
# real subscription quota isn't exposed by Anthropic, so a count of
# recent rate-limits is the closest proxy. Reset implicitly because
# entries past 1 hour are filtered out on read.
_recent_rate_limits: list[float] = []


def record_rate_limit() -> None:
    """Called when a Claude subprocess returns rate-limited output."""
    import time as _t
    _recent_rate_limits.append(_t.time())


def quota_pressure() -> dict[str, object]:
    """Return a snapshot of recent rate-limit events for /health.

    Shape: { count_last_hour, last_at }. The cloud TopBar reads this and
    flips the agent dot's tooltip to "claude pressure: 3 hits/hr" so
    the user knows when delegated work is about to start hitting paid
    API fallback (or, with CLAUDE_FORCE_SUBSCRIPTION=1 on the VPS,
    re-queueing instead of running).
    """
    import time as _t
    cutoff = _t.time() - 3600
    recent = [t for t in _recent_rate_limits if t >= cutoff]
    # Trim in place so memory stays bounded.
    _recent_rate_limits[:] = recent
    return {
        "count_last_hour": len(recent),
        "last_at": recent[-1] if recent else None,
    }


_RATE_LIMIT_MARKERS = (
    "rate limit",          # generic — covers most cases
    "rate_limit_error",    # SDK-shaped JSON
    "rate_limit_exceeded", # alternate spelling
    "429",                 # raw HTTP code in stderr
    "usage limit",         # subscription-tier wording
    "you have reached",    # plan-quota wording
)


def _looks_rate_limited(stdout_lines: list[str], stderr_text: str) -> bool:
    """Heuristic: did the last attempt fail because of a rate / usage limit?"""
    haystack = (stderr_text + "\n" + "\n".join(stdout_lines[-20:])).lower()
    return any(m in haystack for m in _RATE_LIMIT_MARKERS)


class ClaudeBackend(Backend):
    name = "claude"

    async def _run_one(
        self,
        cmd: list[str],
        env: dict,
        workspace: Path,
        log_cb,
        inject_queue: asyncio.Queue | None,
    ) -> tuple[int, list[str], str, int, bool]:
        """Spawn one `claude -p` attempt. Returns
        (return_code, captured_lines, stderr_text, total_tokens, rate_limited)."""
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(workspace),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )

        watcher_task: asyncio.Task | None = None
        if inject_queue is not None:
            watcher_task = asyncio.create_task(
                _inject_watcher(proc, inject_queue),
                name=f"claude-inject-{id(proc)}",
            )

        captured: list[str] = []
        spent_tokens_total = 0

        assert proc.stdout is not None
        async for raw in proc.stdout:
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            captured.append(line)
            try:
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
                usage = obj.get("usage") or {}
                if usage:
                    spent_tokens_total += int(usage.get("input_tokens", 0)) + int(usage.get("output_tokens", 0))
            except Exception:
                pass

        rc = await proc.wait()

        if watcher_task is not None:
            watcher_task.cancel()
            try:
                await watcher_task
            except asyncio.CancelledError:
                pass

        stderr_text = ""
        if proc.stderr:
            stderr_text = (await proc.stderr.read()).decode("utf-8", errors="replace")

        rate_limited = rc != 0 and _looks_rate_limited(captured, stderr_text)
        if rate_limited:
            record_rate_limit()
        return rc, captured, stderr_text, spent_tokens_total, rate_limited

    async def run(
        self,
        task: str,
        workspace: Path,
        log_cb,
        inject_queue: asyncio.Queue | None = None,
    ) -> BackendResult:
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

        # Try subscription first (no API key in env). On 429 / rate-limit signal,
        # re-spawn with the API key included so we don't lose the work. This
        # captures subscription savings as the default while keeping API as an
        # overflow valve when Max plan limits bite.
        # CLAUDE_FORCE_API_KEY=1 → only API key (opt-in, for tasks that
        # specifically want the API path).
        # CLAUDE_FORCE_SUBSCRIPTION=1 → only subscription, never fall back
        # to API (used on the VPS hive worker — keeps that worker's spend
        # at £0, and if the subscription is rate-limited we'd rather the
        # task re-queue for another worker than burn API quota on it).
        force_api = os.environ.get("CLAUDE_FORCE_API_KEY") == "1"
        force_sub = os.environ.get("CLAUDE_FORCE_SUBSCRIPTION") == "1"
        if force_api and force_sub:
            # User pinned both — favour subscription (cheaper) and log.
            await log_cb("jarvis_note", {
                "message": "Both CLAUDE_FORCE_API_KEY and CLAUDE_FORCE_SUBSCRIPTION set; using subscription only.",
                "kind": "config_conflict",
            })
            force_api = False
        attempts = (
            [True]   # API key only — explicit opt-in path
            if force_api else
            [False]  # subscription only — VPS worker mode
            if force_sub else
            [False, True]   # subscription, fall back to API on rate-limit
        )

        rc = 0
        captured: list[str] = []
        spent_tokens_total = 0
        spent_pence_total = 0
        stderr_text = ""

        for attempt_idx, use_api_key in enumerate(attempts):
            env = {
                **os.environ,
                "PATH": "/usr/local/bin:/usr/bin:/bin",
                "HOME": "/root",
                "CLAUDE_CONFIG_DIR": "/root/.claude",
            }
            if not use_api_key:
                env.pop("ANTHROPIC_API_KEY", None)

            await log_cb("assistant_text", {
                "text": "claude-code starting" + (
                    " (subscription)" if not use_api_key else " (api key — fallback)"
                ),
            })

            (
                rc,
                captured,
                stderr_text,
                spent_tokens_total,
                rate_limited,
            ) = await self._run_one(cmd, env, workspace, log_cb, inject_queue)

            if not rate_limited:
                break

            # Rate-limited on subscription. If we have another attempt queued
            # (the API key one), retry; otherwise surface the rate limit.
            if attempt_idx + 1 < len(attempts):
                await log_cb("jarvis_note", {
                    "message": (
                        "Claude subscription hit rate limit. Falling back to "
                        "ANTHROPIC_API_KEY for this run only — subscription will "
                        "be tried again on the next task."
                    ),
                    "kind": "fallback_to_api",
                })

        full_log = "\n".join(captured[-30:])
        if rc != 0:
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
