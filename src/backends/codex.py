"""Codex CLI backend (OpenAI Codex CLI).

Uses `codex` CLI in non-interactive mode. Two auth modes supported:

  1. ChatGPT subscription (recommended): the operator runs `codex login`
     once in a code-server terminal. This opens the OAuth flow on
     :1455 (already published in docker-compose). Codex CLI saves the
     tokens to ~/.codex/auth.json and from then on the agent reuses them.
     Cost: rolled into the ChatGPT Plus / Pro subscription.

  2. API key: set OPENAI_API_KEY in the agent's env. Codex reads it
     automatically. Cost: per-token via the OpenAI Platform billing.

The smart router places Codex between Claude and Qwen as the Assist tier -
useful as a second opinion on complex planning/debugging tasks, especially
when Anthropic is quota-throttled.

Like the Claude backend, this is one-shot synthesis: Codex returns text/
code suggestions; the runner writes them into the workspace. Codex CLI
also has agent modes that execute commands, but we keep this backend
in "advice" mode for safety - the workspace tree is owned by Jarvis's
own file edits, not Codex shell-outs."""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
from pathlib import Path

from .base import Backend, BackendResult

log = logging.getLogger("jarvis-agent.backend.codex")

# Codex CLI lives in coder's npm-global by default.
CODEX_BIN = os.environ.get("CODEX_BIN", "codex")
CODEX_TIMEOUT_S = float(os.environ.get("CODEX_TIMEOUT_S", "300"))

# Codex stores auth tokens at $CODEX_HOME/auth.json (default ~/.codex/auth.json).
# We check multiple candidate paths so it works whether you logged in from
# code-server (coder user) or from the agent container (root), or set up
# the shared workspace path.
def _auth_candidates() -> list[Path]:
    paths: list[str] = []
    if (env_home := os.environ.get("CODEX_HOME")):
        paths.append(f"{env_home}/auth.json")
    paths.extend([
        "/workspace/.codex/auth.json",       # shared workspace mount
        "/home/coder/.codex/auth.json",      # code-server's coder user
        "/root/.codex/auth.json",            # agent (root user)
        os.path.expanduser("~/.codex/auth.json"),
    ])
    seen: set[str] = set()
    return [Path(p) for p in paths if not (p in seen or seen.add(p))]


def is_logged_in() -> bool:
    """True if `codex login` has been completed (OAuth tokens cached anywhere
    Codex CLI might look). We don't introspect the token - just check the file
    exists and is non-empty. Codex CLI itself validates on every call."""
    for p in _auth_candidates():
        try:
            if p.exists() and p.stat().st_size > 0:
                return True
        except Exception:
            continue
    return False


def has_api_key() -> bool:
    return bool(os.environ.get("OPENAI_API_KEY") and os.environ.get("OPENAI_API_KEY") != "unused-local-vllm")


def is_available() -> bool:
    """True if codex CLI is installed and at least one auth method is configured."""
    if shutil.which(CODEX_BIN) is None:
        return False
    return is_logged_in() or has_api_key()


class CodexBackend(Backend):
    """Subscription-or-API codex CLI wrapper. Assist tier between Claude and Qwen."""

    name = "codex"

    async def run(
        self,
        task: str,
        workspace: Path,
        log_cb,
        inject_queue: asyncio.Queue | None = None,
    ) -> BackendResult:
        if shutil.which(CODEX_BIN) is None:
            return BackendResult(
                ok=False,
                summary="codex CLI not installed",
                error="Install with: npm install -g @openai/codex (run as coder, not root).",
            )

        if not (is_logged_in() or has_api_key()):
            return BackendResult(
                ok=False,
                summary="codex not authenticated",
                error=(
                    "Neither OAuth (subscription) nor API key found. "
                    "Run `codex login` in a code-server terminal to use your "
                    "ChatGPT subscription, or set OPENAI_API_KEY in .env."
                ),
            )

        auth_mode = "subscription" if is_logged_in() else "api-key"
        await log_cb("jarvis_note", {
            "message": f"codex-backend starting (auth={auth_mode})"
        })

        # `codex exec` runs the task non-interactively. --skip-confirmations
        # avoids the interactive "do you want me to run X?" prompt.
        cmd = [CODEX_BIN, "exec", "--skip-confirmations", task]

        env = {**os.environ, "HOME": "/home/coder"}

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
                summary="codex CLI launch failed",
                error=f"FileNotFoundError for {CODEX_BIN}",
            )
        except PermissionError as exc:
            return BackendResult(
                ok=False,
                summary="codex CLI permission denied",
                error=f"{exc} - is /home/coder writable for the agent uid?",
            )

        watcher_task: asyncio.Task | None = None
        if inject_queue is not None:
            async def _inject_watcher() -> None:
                while proc.returncode is None:
                    try:
                        msg = await asyncio.wait_for(inject_queue.get(), timeout=1.0)
                        if proc.stdin and not proc.stdin.is_closing():
                            proc.stdin.write((msg + "\n").encode())
                            await proc.stdin.drain()
                    except asyncio.TimeoutError:
                        pass
                    except Exception:
                        break
            watcher_task = asyncio.create_task(_inject_watcher())

        # Stream output line-by-line so progress appears in the task log
        captured: list[str] = []
        assert proc.stdout is not None
        try:
            while True:
                try:
                    line = await asyncio.wait_for(proc.stdout.readline(), timeout=CODEX_TIMEOUT_S)
                except asyncio.TimeoutError:
                    proc.kill()
                    if watcher_task:
                        watcher_task.cancel()
                    return BackendResult(
                        ok=False,
                        summary=f"codex timed out after {CODEX_TIMEOUT_S}s",
                        error="\n".join(captured[-30:]),
                    )
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").rstrip()
                captured.append(text)
                if text.strip():
                    await log_cb("assistant_text", {"text": text[:500]})

            rc = await proc.wait()
        finally:
            if watcher_task is not None:
                watcher_task.cancel()
                try:
                    await watcher_task
                except asyncio.CancelledError:
                    pass

        full_log = "\n".join(captured[-50:])

        if rc != 0:
            return BackendResult(
                ok=False,
                summary=f"codex exited {rc}",
                error=full_log[-2000:],
            )

        return BackendResult(
            ok=True,
            summary=(
                f"codex completed (auth={auth_mode}).\n\n"
                f"Tail of output:\n```\n{full_log[-1500:]}\n```"
            ),
        )
