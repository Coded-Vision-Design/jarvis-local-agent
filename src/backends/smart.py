"""Smart backend - role-based router across Claude, Codex, Qwen, Hermes.

Tier vision (per the user's preferred role split):

  Architect tier   - Claude Opus 4.7      planning, architecture, debugging
  Standard tier    - Claude Sonnet 4.5    basic implementation tasks
  Assist tier      - Codex (OpenAI)       planning / architecture / debug assist
  Worker tier      - Qwen local           autonomous worker:
                                            high-speed coder, scaffolder,
                                            repetitive task executor,
                                            bulk component creator
  Creative tier    - Hermes local         planning / design fallback
                                          (optional, must be running on :18002)

The router inspects the task body for role-revealing keywords and routes
explicitly. Falls back one tier on quota / rate-limit / unavailability.
A complexity score is still computed for tie-breaking but the keyword
classifier dominates.

Set metadata.local_agent_backend = "smart" (the default) to use this.
The task log shows which tier was chosen and which backend actually ran."""
from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path

from ..config import settings
from .base import Backend, BackendResult
from .claude import ClaudeBackend
from .qwen import QwenBackend

log = logging.getLogger("jarvis-agent.backend.smart")


def _can_use_claude() -> bool:
    """Claude Code is routable when the worker has the CLI and either
    subscription OAuth (always tried with API key stripped) or an API key
    for overflow. Subscription-only hosts must not be gated on API key."""
    from ..agent_identity import detect_capabilities

    caps = set(detect_capabilities())
    if "claude" not in caps:
        return False
    if settings.anthropic_api_key:
        return True
    # Capability implies claude binary + login on this host; subscription path
    # does not require ANTHROPIC_API_KEY in env.
    return True


# ── Role detection keywords (lowercase, single-pass match) ────────────────────

# Worker tier: tasks Qwen should own. Cheap, fast, repetitive, no-think.
_WORKER_KEYWORDS = [
    # Scaffolding
    "scaffold", "boilerplate", "stub", "starter",
    "generate component", "generate components", "create component",
    "create components", "make a component", "build a component",
    # Bulk creation
    "bulk", "in bulk", "for each", "for every", "loop over",
    "generate the", "create the boilerplate", "create files for",
    "list of", "table of", "matrix of",
    # Repetitive / mechanical
    "rename", "rename all", "find and replace", "search and replace",
    "extract to", "move to", "format the", "reformat",
    "update imports", "fix imports", "sort imports",
    "add comments", "add comment", "add a comment", "add docstrings",
    "add jsdoc", "add types", "update copyright", "bump version",
    # Quick edits
    "fix typo", "fix typos", "fix the typo", "change the colour",
    "change the text", "update copy", "update wording", "translate to",
]

# Architect tier: deep reasoning, multi-file refactors, system design, debug deep dives.
_ARCHITECT_KEYWORDS = [
    "architecture", "system design", "design the system", "design system",
    "data model", "schema design", "api design",
    "refactor", "redesign", "rewrite", "migrate", "migration",
    "overhaul", "restructure",
    "debug", "investigate", "root cause", "diagnose", "trace the bug",
    "why is", "why does", "why isn't", "why doesn't",
    "plan the", "produce a plan", "design a", "propose an approach",
    "evaluate trade-off", "evaluate trade-offs", "tradeoffs",
    "performance issue", "memory leak", "race condition",
    "security review", "audit",
]

# Standard tier: regular feature work, well-scoped implementations.
_STANDARD_KEYWORDS = [
    "implement", "add feature", "add the", "build the", "create the",
    "fix bug", "fix the bug", "fix issue", "fix the issue",
    "wire up", "hook up", "connect",
    "write the", "add a route", "add an endpoint", "add a test",
]

# Creative tier: open-ended brainstorming, no concrete output yet.
_CREATIVE_KEYWORDS = [
    "brainstorm", "ideas for", "explore",
    "what would", "what if we", "how should we",
    "draft a", "outline a",
]


def _hit_count(text: str, keywords: list[str]) -> int:
    return sum(1 for kw in keywords if kw in text)


def detect_tier(task: str) -> str:
    """Return one of: 'worker', 'architect', 'standard', 'creative', 'auto'.

    Pure-function, deterministic, returns the dominant tier by keyword count.
    Ties break in favour of safer/cheaper tier (worker > standard > architect > creative).
    """
    lower = task.lower()
    worker = _hit_count(lower, _WORKER_KEYWORDS)
    architect = _hit_count(lower, _ARCHITECT_KEYWORDS)
    standard = _hit_count(lower, _STANDARD_KEYWORDS)
    creative = _hit_count(lower, _CREATIVE_KEYWORDS)

    scores = {"worker": worker, "architect": architect, "standard": standard, "creative": creative}
    top = max(scores.values())
    if top == 0:
        return "auto"
    # Tie-break: worker > standard > architect > creative
    for tier in ("worker", "standard", "architect", "creative"):
        if scores[tier] == top:
            return tier
    return "auto"


def complexity_score(task: str) -> float:
    """Soft signal used only when keyword detection returned 'auto'."""
    lower = task.lower()
    score = 0.0
    word_count = len(task.split())
    score += min(word_count / 400, 0.4)
    score += min(_hit_count(lower, _ARCHITECT_KEYWORDS) * 0.15, 0.4)
    score -= _hit_count(lower, _WORKER_KEYWORDS) * 0.1
    score += len(re.findall(r"\d+\s+files?", lower)) * 0.08
    return max(0.0, min(1.0, score))


# ── Failure-mode detection ────────────────────────────────────────────────────

_QUOTA_SIGNALS = [
    "rate limit", "rate_limit", "quota exceeded", "usage limit",
    "overloaded_error", "529", "too many requests", "credit balance",
    "out of credits", "your account", "billing", "plan limit",
]
_CONTEXT_OVERFLOW_SIGNALS = [
    "maximum context length", "context length", "context window",
    "context_length_exceeded", "max tokens",
]


def _is_quota_error(result: BackendResult) -> bool:
    text = ((result.error or "") + " " + (result.summary or "")).lower()
    return any(sig in text for sig in _QUOTA_SIGNALS)


def _is_context_overflow(result: BackendResult) -> bool:
    text = ((result.error or "") + " " + (result.summary or "")).lower()
    return any(sig in text for sig in _CONTEXT_OVERFLOW_SIGNALS)


# ── Smart router ──────────────────────────────────────────────────────────────

class SmartBackend(Backend):
    """Role-based tier router.

    Architect / Standard tasks → Claude (Opus for architect, Sonnet for standard
    is decided by the claude CLI itself based on the configured model).
    Worker tasks → Qwen.
    Creative tasks → Hermes if available, else Qwen.
    Auto (no keywords) → fall back to complexity score; <0.4 = worker, else standard.

    Fallback chain on failure:
      architect/standard fails -> worker (Qwen)
      worker fails -> creative (Hermes) if available, else error
      All fail -> return last error
    """

    name = "smart"

    async def run(
        self,
        task: str,
        workspace: Path,
        log_cb,
        inject_queue: asyncio.Queue | None = None,
    ) -> BackendResult:
        tier = detect_tier(task)
        score = complexity_score(task)

        await log_cb("jarvis_note", {
            "message": f"smart-router: tier={tier} complexity={score:.2f}"
        })

        # Resolve "auto" via complexity score
        if tier == "auto":
            if score < 0.35:
                tier = "worker"
            elif score >= 0.6:
                tier = "architect"
            else:
                tier = "standard"
            await log_cb("jarvis_note", {
                "message": f"smart-router: auto resolved to tier={tier}"
            })

        # ── Architect tier: Claude (Opus-class), fallback Codex, then Qwen ──
        if tier == "architect":
            if _can_use_claude():
                await log_cb("jarvis_note", {"message": "smart-router: routing to claude (architect tier)"})
                try:
                    result = await ClaudeBackend().run(task, workspace, log_cb, inject_queue)
                    if result.ok:
                        return result
                    if _is_quota_error(result):
                        await log_cb("jarvis_note", {
                            "message": "smart-router: claude quota hit - trying codex (assist tier)"
                        })
                    else:
                        return result  # genuine failure, don't paper over with a weaker model
                except Exception as exc:
                    await log_cb("jarvis_note", {
                        "message": f"smart-router: claude raised {type(exc).__name__} - trying codex"
                    })
            # Codex assist tier (subscription via OAuth or API key)
            codex_result = await self._try_codex(task, workspace, log_cb, inject_queue, "architect fallback")
            if codex_result and codex_result.ok:
                return codex_result
            return await self._run_qwen_with_label(task, workspace, log_cb, inject_queue,
                                                    label="architect last-fallback")

        # ── Standard tier: Claude (Sonnet-class), fallback Codex, then Qwen ─
        if tier == "standard":
            if _can_use_claude():
                await log_cb("jarvis_note", {"message": "smart-router: routing to claude (standard tier)"})
                try:
                    result = await ClaudeBackend().run(task, workspace, log_cb, inject_queue)
                    if result.ok:
                        return result
                    if _is_quota_error(result):
                        await log_cb("jarvis_note", {
                            "message": "smart-router: claude quota hit - trying codex (assist tier)"
                        })
                    else:
                        return result
                except Exception as exc:
                    await log_cb("jarvis_note", {
                        "message": f"smart-router: claude raised {type(exc).__name__} - trying codex"
                    })
            codex_result = await self._try_codex(task, workspace, log_cb, inject_queue, "standard fallback")
            if codex_result and codex_result.ok:
                return codex_result
            return await self._run_qwen_with_label(task, workspace, log_cb, inject_queue,
                                                    label="standard last-fallback")

        # ── Worker tier: Qwen (the autonomous worker role) ────────────────
        if tier == "worker":
            await log_cb("jarvis_note", {
                "message": "smart-router: routing to qwen (worker tier - scaffolder/bulk/repetitive)"
            })
            return await self._run_qwen_with_label(task, workspace, log_cb, inject_queue,
                                                    label="worker")

        # ── Creative tier: Hermes (if available), else Qwen, else Claude ──
        if tier == "creative":
            # Capability gate (Phase 18c). Hermes needs a reachable second
            # vLLM endpoint; skip ahead if it's not available rather than
            # trying it and failing.
            from ..agent_identity import detect_capabilities
            caps = set(detect_capabilities())
            if "hermes" in caps:
                try:
                    from .hermes import HermesBackend
                    await log_cb("jarvis_note", {"message": "smart-router: routing to hermes (creative tier)"})
                    result = await HermesBackend().run(task, workspace, log_cb, inject_queue)
                    if result.ok:
                        return result
                    await log_cb("jarvis_note", {
                        "message": "smart-router: hermes failed - falling back"
                    })
                except Exception as exc:
                    await log_cb("jarvis_note", {
                        "message": f"smart-router: hermes raised {type(exc).__name__} - falling back"
                    })
            else:
                await log_cb("jarvis_note", {
                    "message": f"smart-router: hermes not in capabilities ({sorted(caps)}) - using next tier"
                })
            # No-GPU workers go to Claude (creative writing on Sonnet is
            # fine — better than a free-tier text model anyway).
            if "qwen" not in caps and _can_use_claude():
                await log_cb("jarvis_note", {"message": "smart-router: creative on claude (no qwen/hermes)"})
                return await ClaudeBackend().run(task, workspace, log_cb, inject_queue)
            return await self._run_qwen_with_label(task, workspace, log_cb, inject_queue,
                                                    label="creative fallback")

        # Should never reach here, but be defensive
        log.warning("smart-router: unexpected tier=%s, defaulting to qwen", tier)
        return await self._run_qwen_with_label(task, workspace, log_cb, inject_queue,
                                                label="unexpected-tier fallback")

    async def _try_codex(
        self, task, workspace, log_cb, inject_queue, label: str,
    ) -> BackendResult | None:
        """Assist tier: invoke Codex CLI when Claude is unavailable.

        Returns None (caller should continue fallback) if Codex isn't usable,
        otherwise the BackendResult. Subscription auth via OAuth or API key."""
        from .codex import CodexBackend, is_available
        if not is_available():
            await log_cb("jarvis_note", {
                "message": "smart-router: codex unavailable (no auth) - skipping"
            })
            return None
        await log_cb("jarvis_note", {"message": f"smart-router: invoking codex ({label})"})
        try:
            result = await CodexBackend().run(task, workspace, log_cb, inject_queue)
            if result.ok:
                await log_cb("jarvis_note", {"message": "smart-router: codex succeeded ✓"})
                return result
            if _is_quota_error(result):
                await log_cb("jarvis_note", {
                    "message": "smart-router: codex quota hit - falling back to qwen"
                })
                return None  # let caller fall through to qwen
            # Non-quota failure: don't bother fallback chain
            return result
        except Exception as exc:
            await log_cb("jarvis_note", {
                "message": f"smart-router: codex raised {type(exc).__name__} - falling back to qwen"
            })
            return None

    async def _run_qwen_with_label(
        self, task, workspace, log_cb, inject_queue, *, label: str,
    ) -> BackendResult:
        # Capability gate (Phase 18c). Qwen needs a reachable local vLLM
        # and therefore a GPU. On no-GPU workers (VPS hive sibling) this
        # would fail immediately every time — skip straight to cline-free
        # so the task isn't pointlessly retried against an empty endpoint.
        from ..agent_identity import detect_capabilities
        caps = set(detect_capabilities())
        if "qwen" not in caps:
            await log_cb("jarvis_note", {
                "message": f"smart-router: qwen not in capabilities ({sorted(caps)}) - jumping to cline-free (tier 4)"
            })
            return await self._run_cline_free_fallback(task, workspace, log_cb, inject_queue, label)

        try:
            result = await QwenBackend().run(task, workspace, log_cb, inject_queue)
        except Exception as exc:
            await log_cb("jarvis_note", {
                "message": f"smart-router: qwen raised {type(exc).__name__} - escalating to cline-free (tier 4)"
            })
            return await self._run_cline_free_fallback(task, workspace, log_cb, inject_queue, label)

        if result.ok:
            await log_cb("jarvis_note", {"message": f"smart-router: qwen ({label}) succeeded ✓"})
            return result

        # Failure modes: context overflow gets a single retry-with-tier-4 escalation
        if _is_context_overflow(result):
            await log_cb("jarvis_note", {
                "message": "smart-router: qwen context overflow - escalating to cline-free (tier 4)"
            })
            return await self._run_cline_free_fallback(task, workspace, log_cb, inject_queue, label)

        return result

    async def _run_cline_free_fallback(
        self, task, workspace, log_cb, inject_queue, label: str,
    ) -> BackendResult:
        """Tier 4 last-resort. Only invoked when tiers 1-3 are all unreachable."""
        from .cline_free import ClineFreeBackend, is_configured
        if not is_configured():
            await log_cb("error", {
                "message": "smart-router: tier 4 (cline-free) not configured - set CLINE_API_KEY in .env"
            })
            return BackendResult(
                ok=False,
                summary=f"All tiers failed and Cline Free not configured ({label})",
                error="Tier 4 unavailable: CLINE_API_KEY missing.",
            )
        await log_cb("jarvis_note", {"message": "smart-router: invoking cline-free (tier 4)"})
        return await ClineFreeBackend().run(task, workspace, log_cb, inject_queue)
