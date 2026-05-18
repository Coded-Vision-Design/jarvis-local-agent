"""Cline Free backend - Tier 4 last-resort safety net.

When all local + Anthropic tiers are unavailable, we fall through to Cline's
free-tier proxy so work keeps moving. Cline routes free-tier requests to
whichever model is healthiest at the time (typically Claude Sonnet via their
gateway, or Llama / DeepSeek depending on the day).

This backend is rate-limited by Cline, NOT free of latency. Use sparingly.

Required env vars (set in .env, defaults below are safe placeholders):
    CLINE_API_KEY   - your Cline account API key from cline.bot/account
    CLINE_API_BASE  - the proxy endpoint (default: https://api.cline.bot/v1)
    CLINE_MODEL     - leave empty to let Cline pick the best free model,
                      or pin to a specific one (e.g. "free/sonnet-latest")

Architecture note: like the Claude backend, this is a one-shot synthesis
backend, not a sandboxed code-executing CLI. It generates code/text in
response to a task body; the runner then writes the result into the
workspace as appropriate. It does NOT have direct filesystem tool access
the way `claude` (via headless mode) does. For now, treat this purely as
a fallback content generator - it answers questions but doesn't execute.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path

import httpx

from .base import Backend, BackendResult

log = logging.getLogger("jarvis-agent.backend.cline_free")

CLINE_API_BASE = os.environ.get("CLINE_API_BASE", "https://api.cline.bot/v1").rstrip("/")
CLINE_API_KEY = os.environ.get("CLINE_API_KEY", "")
CLINE_MODEL = os.environ.get("CLINE_MODEL", "")  # blank = let Cline pick
CLINE_TIMEOUT_S = float(os.environ.get("CLINE_TIMEOUT_S", "180"))


def is_configured() -> bool:
    """True if we have an API key to call Cline Free with."""
    return bool(CLINE_API_KEY)


async def cline_free_available() -> bool:
    """Cheap reachability check for capability advertising / health probes."""
    if not is_configured():
        return False
    try:
        async with httpx.AsyncClient(timeout=3.0) as c:
            r = await c.get(
                f"{CLINE_API_BASE}/models",
                headers={"Authorization": f"Bearer {CLINE_API_KEY}"},
            )
            return r.status_code in (200, 401)  # 401 means endpoint exists, key bad
    except Exception:
        return False


class ClineFreeBackend(Backend):
    """Tier 4 - free-tier proxy. Last-resort, rate-limited.

    Does not execute code or run shell commands. Returns a generated
    text/code response that the runner can apply manually.
    """

    name = "cline_free"

    async def run(
        self,
        task: str,
        workspace: Path,
        log_cb,
        inject_queue: asyncio.Queue | None = None,
    ) -> BackendResult:
        if not is_configured():
            return BackendResult(
                ok=False,
                summary="Cline Free not configured",
                error="CLINE_API_KEY missing. Sign in at cline.bot, copy key to .env, restart agent.",
            )

        await log_cb("jarvis_note", {
            "message": f"cline-free: calling {CLINE_API_BASE} (model={CLINE_MODEL or 'auto-pick'})"
        })

        system = (
            "You are a fallback worker for Jarvis when its primary backends are "
            "unavailable. Read the task in <task>...</task>. Generate the exact "
            "file contents needed to fulfil it. If the task asks for changes to "
            "specific files, emit a single fenced code block per file using the "
            "format:\n\n"
            "```<lang> path/relative/to/repo/root\n<content>\n```\n\n"
            "Use British English. No em-dashes. Follow the project's CLAUDE.md "
            "if one exists in the workspace."
        )

        payload: dict[str, object] = {
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": f"<task>\n{task}\n</task>"},
            ],
            "max_tokens": 4096,
            "temperature": 0.2,
        }
        if CLINE_MODEL:
            payload["model"] = CLINE_MODEL

        try:
            async with httpx.AsyncClient(timeout=CLINE_TIMEOUT_S) as c:
                r = await c.post(
                    f"{CLINE_API_BASE}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {CLINE_API_KEY}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
                if r.status_code != 200:
                    return BackendResult(
                        ok=False,
                        summary=f"Cline Free returned HTTP {r.status_code}",
                        error=r.text[:1500],
                    )
                data = r.json()

        except httpx.TimeoutException:
            return BackendResult(
                ok=False,
                summary="Cline Free timed out",
                error=f"No response in {CLINE_TIMEOUT_S}s",
            )
        except Exception as exc:
            return BackendResult(
                ok=False,
                summary=f"Cline Free error: {type(exc).__name__}",
                error=str(exc)[:1500],
            )

        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            return BackendResult(
                ok=False,
                summary="Cline Free response malformed",
                error=json.dumps(data)[:1500],
            )

        # Stream the response into the task log so the operator can see what came back
        for chunk in content[:4000].splitlines():
            if chunk.strip():
                await log_cb("assistant_text", {"text": chunk[:500]})

        usage = data.get("usage", {}) or {}
        spent_tokens = (usage.get("prompt_tokens", 0) or 0) + (usage.get("completion_tokens", 0) or 0)

        return BackendResult(
            ok=True,
            summary=(
                f"cline-free completed (model={data.get('model','?')}).\n\n"
                f"Last 1500 chars of output:\n```\n{content[-1500:]}\n```"
            ),
            spent_tokens=spent_tokens or None,
        )
