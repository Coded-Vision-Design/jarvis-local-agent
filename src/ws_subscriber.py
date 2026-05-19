"""Phase 22 — WebSocket subscriber.

Opens a persistent connection to the cloud's WebSocket gateway and
nudges the poller to claim immediately whenever a `new_task` push
arrives. Replaces the 5-second poll floor with sub-50 ms pickup.

The poll loop stays alive as a fallback - reconnect-after-disconnect
and cold-start scenarios still need a way to catch missed pushes.
Once the WS is up, the poller's interval bumps to
ws_fallback_poll_seconds (30 s) to halve the cloud's read load.
"""
from __future__ import annotations

import asyncio
import json
import logging
from urllib.parse import urlencode

import websockets
from websockets.exceptions import ConnectionClosed

from .agent_identity import agent_id
from .config import settings
from .jarvis_client import get_client
from .runner import run_job

log = logging.getLogger("jarvis-agent.ws")

# Set when the WS is connected and pushing - the poller checks this
# to decide which cadence to use.
WS_CONNECTED = asyncio.Event()


def _ws_url() -> str:
    """Build the WS URL with agent_id + roles in the query string. The
    cloud gateway uses these to filter pushes per-agent."""
    import os as _os

    roles_env = (_os.environ.get("AGENT_ROLES") or "").strip()
    qs = {"agent_id": agent_id()}
    if roles_env:
        qs["roles"] = roles_env
    return f"{settings.jarvis_ws_url}?{urlencode(qs)}"


async def _drive_claim_once() -> None:
    """Try to claim immediately. Mirror of poller.py's hot-loop body
    so the WS push and the poll fallback both share the same code
    path for handling claims."""
    client = get_client()
    try:
        task = await client.claim_next()
        if task:
            log.info("ws-driven claim: task %s", task.get("id"))
            asyncio.create_task(run_job(task))
    except Exception:
        log.exception("ws-driven claim failed")


async def ws_subscribe_loop(stop: asyncio.Event) -> None:
    """Forever-loop that keeps a WS open to the cloud gateway. Auto-
    reconnects with exponential backoff (capped) on disconnect."""
    if not settings.jarvis_ws_enabled:
        log.info("ws subscriber disabled by config")
        return

    url = _ws_url()
    headers = [("Authorization", f"Bearer {settings.jarvis_local_agent_token}")]
    backoff = 1.0

    log.info("ws subscriber starting; url=%s", url)
    while not stop.is_set():
        try:
            async with websockets.connect(
                url,
                additional_headers=headers,
                ping_interval=30,
                ping_timeout=10,
                close_timeout=5,
                max_size=64_000,
            ) as ws:
                WS_CONNECTED.set()
                backoff = 1.0
                log.info("ws connected; awaiting new_task pushes")
                async for raw in ws:
                    try:
                        msg = json.loads(raw)
                    except (TypeError, ValueError):
                        continue
                    kind = msg.get("type")
                    if kind == "new_task":
                        # Drive an immediate claim rather than acting on
                        # the embedded task_id directly. The claim still
                        # races atomically (FOR UPDATE SKIP LOCKED) so
                        # parallel workers can't double-claim.
                        await _drive_claim_once()
                    elif kind == "hello":
                        log.debug("ws hello received: %s", msg)
                    elif kind == "pong":
                        pass
                    else:
                        log.debug("ws message ignored: type=%s", kind)
        except ConnectionClosed:
            log.info("ws closed; reconnecting in %.1fs", backoff)
        except Exception as exc:
            log.warning("ws error %s; reconnecting in %.1fs", exc, backoff)
        finally:
            WS_CONNECTED.clear()

        if stop.is_set():
            break
        try:
            await asyncio.wait_for(stop.wait(), timeout=backoff)
            break
        except asyncio.TimeoutError:
            pass
        backoff = min(30.0, backoff * 1.7)

    log.info("ws subscriber stopped")
