"""Approval queue - tasks wait for user approval before running.

When the health loop or an MCP-created task is marked `requires_approval`,
it lands here first. The user approves via the web UI or via an MCP tool,
and only then does the task get pushed to the VPS as `queued`.

This prevents the agent from auto-running speculative work (e.g. health loop
findings) without explicit consent. Supports single, batch, and session-level
("E2E mode") approvals.

Storage: JSON file at /workspace/agent/.approval_queue.json - simple,
human-readable, survives container restarts."""
from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .config import settings

log = logging.getLogger("jarvis-agent.approvals")

QUEUE_FILE = Path("/workspace/agent/.approval_queue.json")
SESSION_FILE = Path("/workspace/agent/.approval_sessions.json")


@dataclass
class PendingApproval:
    id: str                     # uuid4 - distinct from task_id (which doesn't exist yet)
    title: str
    body: str
    repo: str
    backend: str = "smart"
    source: str = "manual"      # "manual", "health_loop", "mcp", "cline"
    severity: str = "medium"    # "low", "medium", "high", "critical"
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    auto_approve_after: float | None = None  # if set, approves automatically at this timestamp


@dataclass
class ApprovalSession:
    """A pre-approved batch context - 'E2E mode'.

    While the session is active, tasks tagged with this session_id auto-approve
    and run sequentially. After each task completes, the agent reports back and
    waits for the next user instruction.
    """
    id: str
    name: str
    repo: str | None = None        # if set, only auto-approve tasks for this repo
    backends: list[str] = field(default_factory=list)  # allowed backends; empty = any
    max_tasks: int = 10            # safety: cap on auto-approvals
    tasks_run: int = 0
    expires_at: float | None = None
    sequential: bool = True        # if True, fix-then-continue mode
    created_at: float = field(default_factory=time.time)


# ── Queue persistence ─────────────────────────────────────────────────────────

def _load_queue() -> list[PendingApproval]:
    if not QUEUE_FILE.exists():
        return []
    try:
        raw = json.loads(QUEUE_FILE.read_text(encoding="utf-8"))
        return [PendingApproval(**item) for item in raw.get("pending", [])]
    except Exception:
        log.exception("failed to load approval queue")
        return []


def _save_queue(items: list[PendingApproval]) -> None:
    QUEUE_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = {"pending": [asdict(it) for it in items]}
    QUEUE_FILE.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _load_sessions() -> dict[str, ApprovalSession]:
    if not SESSION_FILE.exists():
        return {}
    try:
        raw = json.loads(SESSION_FILE.read_text(encoding="utf-8"))
        return {k: ApprovalSession(**v) for k, v in raw.items()}
    except Exception:
        log.exception("failed to load sessions")
        return {}


def _save_sessions(sessions: dict[str, ApprovalSession]) -> None:
    SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = {k: asdict(v) for k, v in sessions.items()}
    SESSION_FILE.write_text(json.dumps(payload, indent=2), encoding="utf-8")


# ── Public API ────────────────────────────────────────────────────────────────

def list_pending() -> list[PendingApproval]:
    """Return all pending approvals, newest first."""
    items = _load_queue()
    items.sort(key=lambda x: x.created_at, reverse=True)
    return items


def add_pending(
    title: str,
    body: str,
    repo: str,
    backend: str = "smart",
    source: str = "manual",
    severity: str = "medium",
    metadata: dict[str, Any] | None = None,
    session_id: str | None = None,
) -> PendingApproval:
    """Queue a task for approval. Returns the PendingApproval record.

    If session_id matches an active session, auto-approves and the caller
    should immediately push the task to the VPS (returns with metadata['auto_approved']=True)."""
    pending = PendingApproval(
        id=str(uuid.uuid4()),
        title=title,
        body=body,
        repo=repo,
        backend=backend,
        source=source,
        severity=severity,
        metadata=metadata or {},
    )

    # Auto-approve via session?
    if session_id:
        session = _load_sessions().get(session_id)
        if session and _session_active(session):
            if _session_allows(session, pending):
                pending.metadata["auto_approved"] = True
                pending.metadata["session_id"] = session_id
                # Increment counter
                sessions = _load_sessions()
                sessions[session_id].tasks_run += 1
                _save_sessions(sessions)
                log.info("session %s auto-approved task: %s", session_id, title[:60])
                return pending

    items = _load_queue()
    items.append(pending)
    _save_queue(items)
    log.info("added pending approval: %s (%s)", title[:60], pending.id[:8])
    return pending


def approve(approval_id: str) -> PendingApproval | None:
    """Remove and return the approval. Caller pushes it to the VPS as a real task."""
    items = _load_queue()
    target: PendingApproval | None = None
    remaining: list[PendingApproval] = []
    for it in items:
        if it.id == approval_id and target is None:
            target = it
        else:
            remaining.append(it)
    if target is None:
        return None
    _save_queue(remaining)
    log.info("approved: %s", target.title[:60])
    return target


def reject(approval_id: str) -> bool:
    items = _load_queue()
    remaining = [it for it in items if it.id != approval_id]
    if len(remaining) == len(items):
        return False
    _save_queue(remaining)
    log.info("rejected approval: %s", approval_id[:8])
    return True


def approve_batch(approval_ids: list[str]) -> list[PendingApproval]:
    items = _load_queue()
    approved: list[PendingApproval] = []
    remaining: list[PendingApproval] = []
    id_set = set(approval_ids)
    for it in items:
        if it.id in id_set:
            approved.append(it)
        else:
            remaining.append(it)
    _save_queue(remaining)
    log.info("batch approved %d items", len(approved))
    return approved


def approve_all(source_filter: str | None = None, severity_filter: str | None = None) -> list[PendingApproval]:
    """Approve everything matching the optional filters.

    Use source_filter='health_loop' to approve all health findings at once.
    Use severity_filter='high' to approve only high/critical."""
    items = _load_queue()
    approved: list[PendingApproval] = []
    remaining: list[PendingApproval] = []
    for it in items:
        match = True
        if source_filter and it.source != source_filter:
            match = False
        if severity_filter and it.severity not in (severity_filter, "critical"):
            match = False
        if match:
            approved.append(it)
        else:
            remaining.append(it)
    _save_queue(remaining)
    log.info("approved %d items matching filters", len(approved))
    return approved


def clear_all() -> int:
    """Reject everything pending. Returns count cleared."""
    items = _load_queue()
    _save_queue([])
    return len(items)


# ── Session management ────────────────────────────────────────────────────────

def _session_active(session: ApprovalSession) -> bool:
    if session.tasks_run >= session.max_tasks:
        return False
    if session.expires_at and time.time() > session.expires_at:
        return False
    return True


def _session_allows(session: ApprovalSession, pending: PendingApproval) -> bool:
    if session.repo and pending.repo != session.repo:
        return False
    if session.backends and pending.backend not in session.backends:
        return False
    return True


def create_session(
    name: str,
    repo: str | None = None,
    backends: list[str] | None = None,
    max_tasks: int = 10,
    duration_seconds: int = 3600,
    sequential: bool = True,
) -> ApprovalSession:
    """Create an E2E approval session - tasks auto-approve within these bounds.

    Use this when you want Jarvis to work through a sequence of tasks without
    asking permission between each one. The session ends after `max_tasks` or
    `duration_seconds`."""
    session = ApprovalSession(
        id=str(uuid.uuid4()),
        name=name,
        repo=repo,
        backends=backends or [],
        max_tasks=max_tasks,
        expires_at=time.time() + duration_seconds if duration_seconds else None,
        sequential=sequential,
    )
    sessions = _load_sessions()
    sessions[session.id] = session
    _save_sessions(sessions)
    log.info("created approval session %s (%s)", session.id[:8], name)
    return session


def list_sessions() -> list[ApprovalSession]:
    return list(_load_sessions().values())


def end_session(session_id: str) -> bool:
    sessions = _load_sessions()
    if session_id not in sessions:
        return False
    del sessions[session_id]
    _save_sessions(sessions)
    return True
