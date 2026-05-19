"""Phase 19 pre-work retrieval.

Before the agent's backend ever sees the task, fetch context Jarvis
has built up from prior work and prepend it to the prompt. Two tiers:

  1. Per-entity recency — last 10 non-superseded notes on the same
     `related_entity`. Cheap, always runs if entity is set. Surfaces
     "what's been done on Carlson Gracie before".

  2. Cross-entity pattern search — FTS over notes tagged by the agent
     itself (rag-bleed, deploy-fail, etc) matching the task title +
     body. Surfaces "I've seen this kind of failure before on three
     other repos".

Both feeds get rendered as a tight markdown section the backend reads
as its first move. The render is deterministic — no LLM in the loop
here — so cost is just the HTTP calls.
"""

from __future__ import annotations

import logging
import re
from typing import Any

log = logging.getLogger(__name__)


# Cap individual note content shown in the prepended block so the
# system prompt doesn't blow out the context window when there are
# many past notes on the same entity.
_PER_NOTE_CHAR_CAP = 400
_TOTAL_BLOCK_CHAR_CAP = 4000


async def fetch_prior_context(
    client,
    task: dict[str, Any],
    *,
    entity_limit: int = 10,
    pattern_limit: int = 5,
) -> str:
    """Return a markdown-formatted prepend block of prior context. Empty
    string when there's nothing useful — the runner just skips the
    prepend in that case so the system prompt stays clean for first-
    time-on-this-entity tasks."""
    entity = (task.get("related_entity") or "").strip()
    title = (task.get("title") or "").strip()
    body = (task.get("body") or "")[:1500]

    # Tier 1 — recency on this entity
    recent: list[dict[str, Any]] = []
    if entity:
        try:
            recent = await client.recent_notes_for_entity(entity, entity_limit)
        except Exception as exc:
            log.warning("recent_notes_for_entity failed: %s", exc)

    # Tier 2 — pattern search across the corpus. Use the task title +
    # body as the FTS query; this picks up notes tagged with similar
    # failure shapes.
    query = _build_search_query(title, body)
    patterns: list[dict[str, Any]] = []
    if query:
        try:
            patterns = await client.search_notes(q=query, limit=pattern_limit)
        except Exception as exc:
            log.warning("search_notes failed: %s", exc)

    # De-dupe: a note returned by both tiers should appear once.
    seen_ids: set[int] = set()
    deduped_recent: list[dict[str, Any]] = []
    for n in recent:
        nid = n.get("id")
        if isinstance(nid, int) and nid not in seen_ids:
            seen_ids.add(nid)
            deduped_recent.append(n)
    deduped_patterns: list[dict[str, Any]] = []
    for n in patterns:
        nid = n.get("id")
        if isinstance(nid, int) and nid not in seen_ids:
            seen_ids.add(nid)
            deduped_patterns.append(n)

    if not deduped_recent and not deduped_patterns:
        return ""

    return _render_block(entity, deduped_recent, deduped_patterns)


def _build_search_query(title: str, body: str) -> str:
    """Trim the title+body down to a few discriminating words for FTS.
    Postgres plainto_tsquery handles stopword removal so we just need
    to keep token-rich content. Length cap is for sanity, not cost."""
    text = f"{title} {body[:400]}"
    # Strip code-block markdown that often shows up in task bodies — the
    # FTS lexer doesn't understand it and it dilutes the signal.
    text = re.sub(r"```[^`]*```", " ", text)
    text = re.sub(r"[^\w\s\-]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    # Keep the first ~30 words — plenty for pattern matching, well under
    # plainto_tsquery's effective limit.
    words = text.split()
    return " ".join(words[:30])


def _render_block(
    entity: str,
    recent: list[dict[str, Any]],
    patterns: list[dict[str, Any]],
) -> str:
    """Render the two tiers as a single markdown section. Truncates
    aggressively to keep the prepend cheap."""
    lines = ["## Prior context (auto-retrieved — Phase 19 handover notes)"]
    if entity and recent:
        lines.append(f"\n### Recent work on `{entity}` (last {len(recent)})")
        for n in recent:
            lines.append(_format_note_line(n))
    if patterns:
        lines.append(f"\n### Similar patterns from prior tasks ({len(patterns)})")
        for n in patterns:
            lines.append(_format_note_line(n, include_entity=True))

    block = "\n".join(lines)
    if len(block) > _TOTAL_BLOCK_CHAR_CAP:
        block = block[: _TOTAL_BLOCK_CHAR_CAP] + "\n…(truncated)"
    lines = block.split("\n")
    lines.append("")
    lines.append("(Take these into account before picking your strategy. ")
    lines.append("If a pattern matches, lead with the fix that worked. ")
    lines.append("If a recent note says something is stale, verify before assuming.)")
    return "\n".join(lines)


def _format_note_line(note: dict[str, Any], *, include_entity: bool = False) -> str:
    """One bullet per note. Surfaces trigger + ts + the most useful
    field (next_steps > what_failed > what_done)."""
    trigger = note.get("trigger_kind") or "manual"
    ts = (note.get("ts") or "")[:10]  # YYYY-MM-DD
    tags = note.get("tags") or []
    tag_str = f" [{', '.join(tags[:3])}]" if tags else ""
    ent = f" · {note.get('related_entity', '—')}" if include_entity else ""

    # Prefer next_steps (the most actionable field). Fall back through
    # what_failed → what_done so we always say something.
    summary = (
        note.get("next_steps")
        or note.get("what_failed")
        or note.get("what_done")
        or ""
    )
    summary = summary.strip()
    if len(summary) > _PER_NOTE_CHAR_CAP:
        summary = summary[: _PER_NOTE_CHAR_CAP - 1] + "…"

    return f"- [{ts}] [{trigger}]{ent}{tag_str} — {summary}"
