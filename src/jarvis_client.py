"""HTTP client for the Jarvis Next.js API. Handles the narrow set of endpoints
the local agent talks to (poll, per-task writes). Bearer auth on every call."""
from __future__ import annotations

import logging
from typing import Any

import httpx

from .agent_identity import agent_id, identity_payload
from .config import settings

log = logging.getLogger("jarvis-agent.client")


class JarvisClient:
    def __init__(self) -> None:
        self.base = settings.jarvis_base.rstrip("/")
        # Phase 21m — advertise the roles this worker will accept.
        # Comma-separated env var AGENT_ROLES. When unset / empty,
        # the server treats it as "accepts all roles" (back-compat).
        # decompose_and_delegate-generated children carry
        # metadata.role; the poll claim filters by this header.
        import os as _os
        roles_env = (_os.environ.get("AGENT_ROLES") or "").strip()
        headers = {
            "Authorization": f"Bearer {settings.jarvis_local_agent_token}",
            "Content-Type": "application/json",
            # User-Agent doubles as a cheap agent identifier in VPS access logs.
            "User-Agent": f"jarvis-local-agent/0.2 (agent={agent_id()})",
            "X-Jarvis-Agent-Id": agent_id(),
        }
        if roles_env:
            headers["X-Jarvis-Agent-Roles"] = roles_env
        self._headers = headers
        # One client, kept open. httpx pools connections.
        self._http = httpx.AsyncClient(
            base_url=self.base,
            headers=self._headers,
            timeout=httpx.Timeout(connect=5.0, read=15.0, write=10.0, pool=5.0),
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def claim_next(self) -> dict[str, Any] | None:
        """Atomically claim the next queued delegated task. None if empty queue."""
        r = await self._http.post("/api/tasks/local-agent/poll")
        if r.status_code == 204:
            return None
        if r.status_code == 401:
            log.error("local agent unauthorized — check JARVIS_LOCAL_AGENT_TOKEN matches Jarvis .env")
            return None
        r.raise_for_status()
        return r.json().get("task")

    async def append_run(
        self,
        task_id: int,
        run_kind: str,
        payload: dict[str, Any],
        cost_pence: int | None = None,
    ) -> None:
        body: dict[str, Any] = {"kind": "run", "run_kind": run_kind, "payload": payload}
        if cost_pence is not None:
            body["cost_pence"] = cost_pence
        r = await self._http.post(f"/api/tasks/{task_id}/local-agent", json=body)
        if r.status_code >= 400:
            log.warning("append_run %s failed: %s %s", run_kind, r.status_code, r.text[:200])

    async def heartbeat(self, task_id: int) -> None:
        # Attach agent identity so the VPS knows which worker is alive.
        # Falls back gracefully if the VPS endpoint ignores the extra field.
        body = {"kind": "heartbeat", "agent": identity_payload()}
        r = await self._http.post(
            f"/api/tasks/{task_id}/local-agent", json=body,
        )
        if r.status_code >= 400:
            log.warning("heartbeat failed: %s %s", r.status_code, r.text[:200])

    async def merge_metadata(self, task_id: int, patch: dict[str, Any]) -> None:
        r = await self._http.post(
            f"/api/tasks/{task_id}/local-agent",
            json={"kind": "metadata", "patch": patch},
        )
        if r.status_code >= 400:
            log.warning("merge_metadata failed: %s %s", r.status_code, r.text[:200])

    # ── Phase 19 handover notes ────────────────────────────────────────
    async def post_note(self, task_id: int, body: dict[str, Any]) -> dict[str, Any] | None:
        """Append a handover note. Server stamps author_kind, agent_id,
        backend from bearer + body context. Returns the created note or
        None on failure (we never raise; note writes are best-effort).

        Fire-and-forget kicks the embed pipeline so the note becomes
        RAG-searchable within a few seconds — we don't await it because
        the runner shouldn't block on RAG ops."""
        import asyncio as _asyncio
        body.setdefault("agent_id", agent_id())
        r = await self._http.post(f"/api/tasks/{task_id}/notes", json=body)
        if r.status_code >= 400:
            log.warning("post_note failed: %s %s", r.status_code, r.text[:200])
            return None
        try:
            note = r.json().get("note")
        except Exception:
            return None
        if note and isinstance(note.get("id"), int):
            _asyncio.create_task(self._embed_note_async(note["id"]))
        return note

    async def _embed_note_async(self, note_id: int) -> None:
        """Background embed kick. Idempotent on the server side so a
        retry is safe. Failures logged, never raised."""
        try:
            r = await self._http.post("/api/rag/embed-note", json={"note_id": note_id})
            if r.status_code >= 400:
                log.warning("embed-note %s failed: %s %s", note_id, r.status_code, r.text[:200])
        except Exception as exc:
            log.warning("embed-note %s exception: %s", note_id, exc)

    async def recent_notes_for_entity(self, entity: str, limit: int = 10) -> list[dict[str, Any]]:
        """First tier of pre-work retrieval — last N non-superseded
        notes on the given entity, newest first."""
        if not entity:
            return []
        r = await self._http.get(
            "/api/tasks/notes/recent",
            params={"entity": entity, "limit": limit},
        )
        if r.status_code >= 400:
            log.warning("recent_notes failed: %s %s", r.status_code, r.text[:200])
            return []
        try:
            return r.json().get("notes", [])
        except Exception:
            return []

    async def fetch_knowledge_context(
        self,
        *,
        title: str,
        body: str,
        entity: str | None = None,
        limit: int = 4,
    ) -> str:
        """Tier 3 pre-work: Jarvis knowledge bank (RAG chunks) for this task."""
        query = f"{title}\n{body[:1500]}".strip()
        if not query:
            return ""
        params: dict[str, Any] = {"q": query, "limit": limit}
        if entity:
            params["entity"] = entity
        r = await self._http.get("/api/knowledge/context/agent", params=params)
        if r.status_code >= 400:
            log.warning(
                "fetch_knowledge_context failed: %s %s",
                r.status_code,
                r.text[:200],
            )
            return ""
        try:
            return (r.json().get("block") or "").strip()
        except Exception:
            return ""

    async def search_notes(
        self,
        q: str = "",
        tags: list[str] | None = None,
        triggers: list[str] | None = None,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        """Second tier of pre-work retrieval — FTS + tag-AND across all
        notes regardless of entity. Surfaces cross-tenant failure
        patterns ("I've seen this kind of bleed in 3 other repos")."""
        params: dict[str, Any] = {"limit": limit}
        if q:
            params["q"] = q
        if tags:
            params["tags"] = ",".join(tags)
        if triggers:
            params["triggers"] = ",".join(triggers)
        r = await self._http.get("/api/tasks/notes/search", params=params)
        if r.status_code >= 400:
            log.warning("search_notes failed: %s %s", r.status_code, r.text[:200])
            return []
        try:
            return r.json().get("notes", [])
        except Exception:
            return []

    async def create_task(
        self,
        title: str,
        body: str,
        repo: str,
        backend: str = "smart",
        priority: int = 50,
    ) -> dict[str, Any] | None:
        """Create a new delegated task on the Jarvis server. Returns the created task or None."""
        payload: dict[str, Any] = {
            "title": title,
            "body": body,
            "status": "queued",
            "metadata": {
                "local_agent_backend": backend,
                "local_agent_repo": repo,
                "created_by": "jarvis-local-agent-mcp",
            },
            "priority": priority,
        }
        try:
            r = await self._http.post("/api/tasks", json=payload)
            if r.status_code in (200, 201):
                return r.json().get("task") or r.json()
            log.warning("create_task failed: %s %s", r.status_code, r.text[:200])
            return None
        except Exception as exc:
            log.warning("create_task exception: %s", exc)
            return None

    async def get_recent_tasks(self, limit: int = 20) -> list[dict[str, Any]]:
        """Fetch recently completed/blocked tasks from the Jarvis server."""
        try:
            r = await self._http.get(f"/api/tasks?limit={limit}&status=done,blocked")
            if r.status_code == 200:
                data = r.json()
                return data.get("tasks") or data if isinstance(data, list) else []
            return []
        except Exception:
            return []

    async def set_status(
        self,
        task_id: int,
        status: str,
        spent_pence: int | None = None,
        spent_tokens: int | None = None,
    ) -> None:
        body: dict[str, Any] = {"kind": "status", "status": status}
        if spent_pence is not None:
            body["spent_pence"] = spent_pence
        if spent_tokens is not None:
            body["spent_tokens"] = spent_tokens
        r = await self._http.post(f"/api/tasks/{task_id}/local-agent", json=body)
        if r.status_code >= 400:
            log.warning("set_status %s failed: %s %s", status, r.status_code, r.text[:200])


# Module-level singleton; lazily constructed.
_client: JarvisClient | None = None


def get_client() -> JarvisClient:
    global _client
    if _client is None:
        _client = JarvisClient()
    return _client
