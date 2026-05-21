"""Evidence rules for delegated tasks — when 'done' requires shipped artefacts."""
from __future__ import annotations

import re
from typing import Any

# Tasks that imply filesystem / deploy output. No git diff → blocked, not done.
_BUILD_KEYWORDS = [
    "build ",
    "build a",
    "scaffold",
    "implement",
    "create the",
    "create a",
    "demo site",
    "demo website",
    "landing page",
    "next.js",
    "nextjs",
    "deploy",
    "npm run build",
    "pull request",
    "open a pr",
    "full site",
    "one-page",
    "one page",
    "website project",
    "web app",
    "webapp",
]

# Doc-only / advisory work may legitimately finish with no file changes.
_DOC_ONLY_KEYWORDS = [
    "explain only",
    "advice only",
    "no code changes",
    "do not edit",
    "don't edit",
    "read-only review",
    "review only",
    "audit only",
    "summarise",
    "summarize",
    "answer only",
]


def _text_blob(task: dict[str, Any]) -> str:
    title = (task.get("title") or "").strip()
    body = (task.get("body") or "")[:8000]
    meta = task.get("metadata") or {}
    steer = ""
    if isinstance(meta, dict):
        steer = str(meta.get("last_user_steer") or "")[:2000]
    return f"{title}\n{body}\n{steer}".lower()


def allows_no_changes(task: dict[str, Any]) -> bool:
    """Explicit opt-out from the shipped-artefact requirement."""
    meta = task.get("metadata") or {}
    if not isinstance(meta, dict):
        return False
    if meta.get("allow_no_changes") is True:
        return True
    if meta.get("local_agent_allow_no_changes") is True:
        return True
    blob = _text_blob(task)
    return any(kw in blob for kw in _DOC_ONLY_KEYWORDS)


def is_build_class_task(task: dict[str, Any]) -> bool:
    """True when the task description expects code or deploy output on disk."""
    if allows_no_changes(task):
        return False
    meta = task.get("metadata") or {}
    if isinstance(meta, dict):
        if meta.get("requires_shipped_artefact") is True:
            return True
        if meta.get("local_agent_requires_artefact") is True:
            return True
    blob = _text_blob(task)
    return any(kw in blob for kw in _BUILD_KEYWORDS)


def no_changes_should_block(
    task: dict[str, Any],
    *,
    backend_name: str,
) -> tuple[bool, str]:
    """Return (should_block, reason) when the runner sees zero file changes.

    Codex runs in advisory mode — never treat a silent Codex run as done for
    build-class work. Claude/Qwen may still legitimately no-op on doc tasks."""
    if allows_no_changes(task):
        return False, ""
    if backend_name == "codex":
        return True, "codex_advisory_no_files"
    if is_build_class_task(task):
        return True, "no_shipped_artefact"
    return False, ""
