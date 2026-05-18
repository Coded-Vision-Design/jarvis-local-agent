"""Posts completion summaries to Discord as 'Jarvis' via the webhook URL.
Fails silent — Discord being down should never block job completion."""
from __future__ import annotations

import logging

import httpx

from .config import settings

log = logging.getLogger("jarvis-agent.discord")


async def post_as_jarvis(content: str, username: str = "Jarvis") -> None:
    url = settings.discord_jarvis_tasks_webhook_url
    if not url:
        log.debug("no webhook URL configured; skipping post")
        return
    if len(content) > 1900:
        content = content[:1900] + "…"
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            r = await client.post(
                url,
                json={
                    "content": content,
                    "username": username,
                    "allowed_mentions": {"parse": []},
                },
            )
            if r.status_code >= 400:
                log.warning("webhook returned %s: %s", r.status_code, r.text[:200])
    except Exception:
        log.exception("webhook post failed")
