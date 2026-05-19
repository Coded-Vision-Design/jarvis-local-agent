"""Background loop: claim delegated tasks from Jarvis and dispatch them
to run_job. Runs forever as an asyncio task spawned from main.py."""
from __future__ import annotations

import asyncio
import logging

from .config import settings
from .jarvis_client import get_client
from .runner import run_job
from .ws_subscriber import WS_CONNECTED

log = logging.getLogger("jarvis-agent.poller")


async def poll_loop(stop: asyncio.Event) -> None:
    client = get_client()
    hot_interval = settings.poll_interval_seconds
    cold_interval = settings.ws_fallback_poll_seconds
    log.info(
        "poll loop started; hot=%ss cold=%ss; jarvis=%s",
        hot_interval,
        cold_interval,
        settings.jarvis_base,
    )
    while not stop.is_set():
        try:
            task = await client.claim_next()
            if task:
                log.info("claimed task %s", task.get("id"))
                # Fire-and-forget; the runner manages its own heartbeats and status.
                asyncio.create_task(run_job(task))
                # Brief pause before next poll so we don't immediately grab a second
                # task in the same backend slot (run_job will reject + re-queue, but
                # quieter to just slow down a touch).
                await asyncio.sleep(1.0)
                continue
        except Exception:
            log.exception("poll error; backing off")
            await asyncio.sleep(min(60, hot_interval * 4))
            continue
        # Phase 22 — when the WS subscriber is connected and pushing,
        # the poll loop becomes a reconnect / cold-start fallback only:
        # cadence drops from 5 s to 30 s. The poll still hits the
        # cloud's heartbeat endpoint so reachable status stays accurate.
        interval = cold_interval if WS_CONNECTED.is_set() else hot_interval
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass
    log.info("poll loop stopped")
