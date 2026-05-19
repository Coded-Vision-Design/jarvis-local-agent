"""Phase 19 handover-note generation.

When a task hits one of four trigger points (done / blocked /
pre_approval / tool_error) the runner asks its backend to write a
short structured note in JSON. The note is then POSTed to
`/api/tasks/:id/notes` where it becomes part of the engineer-grade
knowledge layer + (via NOTIFY) feeds RAG retrieval for future tasks.

Voice: terse engineer log, bullet-led. Each field is optional; the
backend is told to leave fields blank when they don't apply. Hard cap
on tokens so notes stay scanable and cheap.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import subprocess
import time
from typing import Any

log = logging.getLogger(__name__)


# Cap each backend invocation so noting itself never bills a fortune.
# At ~600 output tokens × Haiku rates = ~£0.001/note. With four
# triggers per task and ~50 tasks/day this caps at ~£0.20/day.
_NOTE_MAX_TOKENS = 600

# Tags the agent can self-emit. Restricting the vocabulary keeps the
# pattern surface coherent. The backend is told to pick from this list
# OR coin a new tag IFF it fits a real recurring pattern (not one-off).
_TAG_VOCAB = (
    "rag-bleed", "deploy-fail", "auth-refactor", "type-error",
    "test-flake", "rate-limited", "secret-missing", "schema-mismatch",
    "git-conflict", "tool-error", "approval-needed", "scope-creep",
    "low-confidence", "third-party-outage", "needs-human-review",
)

# JSON schema the backend is asked to fill. Strings only; agent is told
# to leave a field blank ("") rather than fabricate.
_NOTE_SHAPE = {
    "tags": [],
    "what_done": "",
    "what_remaining": "",
    "what_failed": "",
    "risks": "",
    "next_steps": "",
}


def build_note_prompt(
    trigger: str,
    task: dict[str, Any],
    context: dict[str, Any],
) -> str:
    """Render the structured-note prompt the agent backend completes.

    The prompt is intentionally short — every extra token shows up in
    the budget. Bullet-led examples teach the voice without lengthy
    instructions. Returns JSON-only on the wire (no prose preamble).
    """
    title = task.get("title", "")
    body = (task.get("body", "") or "")[:1500]
    entity = task.get("related_entity") or "—"

    trigger_focus = _TRIGGER_FOCUS.get(trigger, "")
    recent_errors = (context.get("recent_errors") or [])[:3]
    recent_tools = (context.get("recent_tools") or [])[:5]
    user_steer = context.get("user_steer") or ""

    parts = [
        "You are leaving a HANDOVER NOTE on a task you just worked on.",
        "Voice: terse engineer log. Bullet-led. File paths + line numbers when relevant.",
        "Do NOT pad. Each field can be one line or a few bullets. Leave a field blank ('')",
        "if it doesn't apply. NEVER fabricate file paths, PR URLs or facts not in context.",
        "",
        f"Trigger: {trigger}",
        f"Task: #{task.get('id')} · {title}",
        f"Entity: {entity}",
        "",
        "Body excerpt:",
        body,
        "",
    ]
    if recent_tools:
        parts += ["Recent tool calls:"] + [f"- {t}" for t in recent_tools] + [""]
    if recent_errors:
        parts += ["Recent errors:"] + [f"- {e}" for e in recent_errors] + [""]
    if user_steer:
        parts += [f"Latest user steer: {user_steer[:400]}", ""]
    if trigger_focus:
        parts += [f"Focus for this note: {trigger_focus}", ""]

    parts += [
        f"Pick tags from this list (1–3): {', '.join(_TAG_VOCAB)}",
        "OR coin a new tag IFF it names a recurring pattern (not a one-off).",
        "",
        "Respond with ONLY valid JSON. No prose. No markdown fences. Shape:",
        json.dumps(_NOTE_SHAPE, indent=2),
    ]
    return "\n".join(parts)


_TRIGGER_FOCUS = {
    "done": (
        "What was accomplished. What still needs doing if the user wants to extend. "
        "Risks of regression. Concrete next step a future operator would take."
    ),
    "blocked": (
        "Where the wall is. What was tried. Why it stalled. "
        "Smallest unblocking step the next runner could try."
    ),
    "pre_approval": (
        "Why this action is the right one. What alternatives were considered + rejected. "
        "What the rollback looks like if it goes wrong. Keep it tight — surfaces in the approval card."
    ),
    "tool_error": (
        "Which tool, which args, what came back. Pattern this matches if any. "
        "Concrete fix the next attempt should try."
    ),
}


def parse_note_json(raw: str) -> dict[str, Any] | None:
    """Pull the JSON object out of the backend's response. Tolerates
    markdown code fences and prose preamble (the backend sometimes adds
    them despite the prompt's ask). Returns None if no valid JSON is
    extractable — caller treats that as 'skip this note'."""
    if not raw:
        return None
    candidate = raw.strip()
    # Strip code fences
    if candidate.startswith("```"):
        candidate = re.sub(r"^```[a-zA-Z]*\n?", "", candidate)
        candidate = re.sub(r"\n?```\s*$", "", candidate)
    # Find the first balanced JSON object
    start = candidate.find("{")
    if start == -1:
        return None
    # Try parsing progressively shorter slices until valid (handles
    # cases where the model trails extra prose after the JSON).
    for end in range(len(candidate), start, -1):
        slice_ = candidate[start:end]
        try:
            parsed = json.loads(slice_)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            continue
    return None


def normalise_note(raw: dict[str, Any] | None) -> dict[str, Any]:
    """Coerce the parsed JSON into the shape the API expects. Drops
    unknown keys, enforces type contracts, caps string lengths."""
    if not raw:
        return {}
    out: dict[str, Any] = {}
    for key in ("what_done", "what_remaining", "what_failed", "risks", "next_steps"):
        v = raw.get(key)
        if isinstance(v, str) and v.strip():
            out[key] = v.strip()[:8000]
    tags = raw.get("tags")
    if isinstance(tags, list):
        clean = [
            t.strip().lower()
            for t in tags
            if isinstance(t, str) and 1 <= len(t.strip()) <= 60
        ]
        if clean:
            out["tags"] = clean[:5]
    return out


async def ask_backend_for_note(
    backend_name: str,
    prompt: str,
    *,
    timeout_seconds: int = 60,
) -> tuple[str, int]:
    """Cheapest path to a one-shot JSON from the active backend.

    For claude / codex / qwen subprocess-style backends we shell out to
    `<binary> -p` with the prompt on stdin. The agent's `smart` backend
    routes through claude by default for these short calls.

    Returns (raw_response, cost_pence_estimate). The cost estimate is a
    flat 1p (≈ Haiku rates for ≤600 tokens) — we accept the imprecision
    so we don't have to parse per-backend cost output.
    """
    # Pick the cheap variant — explicit `claude` binary with no tools.
    # Falls back to other binaries by name if claude isn't available.
    cmd = None
    if backend_name in ("claude", "smart"):
        cmd = ["claude", "-p", "--max-tokens", str(_NOTE_MAX_TOKENS)]
    elif backend_name == "codex":
        cmd = ["codex", "exec", "--quiet"]
    elif backend_name == "qwen":
        cmd = ["qwen", "-p"]
    if cmd is None:
        return ("", 0)

    started = time.time()
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, _ = await asyncio.wait_for(
                proc.communicate(input=prompt.encode("utf-8", errors="replace")),
                timeout=timeout_seconds,
            )
        except asyncio.TimeoutError:
            proc.kill()
            return ("", 0)
        elapsed = time.time() - started
        log.debug("note backend %s took %.1fs", backend_name, elapsed)
        return (stdout.decode("utf-8", errors="replace"), 1)
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("note backend %s spawn failed: %s", backend_name, exc)
        return ("", 0)


async def emit_handover_note(
    client,
    task: dict[str, Any],
    trigger: str,
    backend_name: str,
    context: dict[str, Any],
    artefacts: dict[str, Any] | None = None,
) -> None:
    """Top-level entry point — generate + POST a handover note for the
    given trigger. Best-effort: any failure is swallowed (logged) so
    note generation never breaks the actual task pipeline."""
    try:
        prompt = build_note_prompt(trigger, task, context)
        raw, cost = await ask_backend_for_note(backend_name, prompt)
        parsed = parse_note_json(raw)
        clean = normalise_note(parsed)
        # Empty notes are useless — skip rather than insert a blank row
        # the user has to wade past.
        if not any(clean.get(k) for k in ("what_done", "what_remaining", "what_failed", "risks", "next_steps")):
            log.info("note for task %s trigger=%s produced no usable content; skipping",
                     task.get("id"), trigger)
            return
        note_body = {
            "trigger_kind": trigger,
            "related_entity": task.get("related_entity"),
            "backend": backend_name,
            "artefacts": artefacts or {},
            "cost_pence": cost,
            **clean,
        }
        await client.post_note(int(task["id"]), note_body)
    except Exception as exc:
        log.warning("emit_handover_note(trigger=%s) failed: %s", trigger, exc)
