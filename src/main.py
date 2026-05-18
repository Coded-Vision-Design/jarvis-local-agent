from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from fastapi_mcp import FastApiMCP
from pydantic import BaseModel

from .config import settings
from .jarvis_client import get_client
from .poller import poll_loop
from .repos import load_repos

logging.basicConfig(
    level=settings.log_level.upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("jarvis-agent")


# ── Lifespan: spawn the poll loop on startup, signal it to stop on shutdown ───
@asynccontextmanager
async def lifespan(app: FastAPI):
    stop = asyncio.Event()
    poll_task = asyncio.create_task(poll_loop(stop))
    log.info("startup complete; polling jarvis at %s", settings.jarvis_base)
    try:
        yield
    finally:
        log.info("shutdown: signalling poll loop")
        stop.set()
        try:
            await asyncio.wait_for(poll_task, timeout=5.0)
        except asyncio.TimeoutError:
            poll_task.cancel()
        await get_client().aclose()


app = FastAPI(
    title="Jarvis Local Agent",
    version="0.1.0",
    description=(
        "Runs delegated coding jobs on the user's Windows dev box. "
        "Two backends: Claude Code headless and qwen-code against local vLLM."
    ),
    lifespan=lifespan,
)


class Health(BaseModel):
    ok: bool
    vllm_reachable: bool
    claude_ok: bool
    qwen_ok: bool
    jarvis_reachable: bool
    repos_count: int


class ReposList(BaseModel):
    repos: list[str]


@app.get("/health", response_model=Health, operation_id="health")
async def health() -> Health:
    """Liveness + dependency check."""
    vllm_reachable = False
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            r = await client.get(f"{settings.vllm_base}/v1/models")
            vllm_reachable = r.status_code == 200
    except Exception:
        pass

    jarvis_reachable = False
    try:
        async with httpx.AsyncClient(timeout=2.0) as c2:
            r2 = await c2.get(f"{settings.jarvis_base.rstrip('/')}/api/health")
            jarvis_reachable = r2.status_code < 500
    except Exception:
        pass

    import shutil
    claude_ok = shutil.which("claude") is not None
    qwen_ok = shutil.which("qwen") is not None

    return Health(
        ok=True,
        vllm_reachable=vllm_reachable,
        claude_ok=claude_ok,
        qwen_ok=qwen_ok,
        jarvis_reachable=jarvis_reachable,
        repos_count=len(load_repos()),
    )


@app.get("/repos", response_model=ReposList, operation_id="list_repos")
async def list_repos() -> ReposList:
    """Returns the current whitelist (re-read from disk on each call)."""
    return ReposList(repos=load_repos())


# Expose endpoints as MCP tools at /mcp (SSE).
# Sub-agent CLIs can mount this via --mcp-config.
mcp = FastApiMCP(
    app,
    name="jarvis-local-agent",
    description="Local sub-agent: repo whitelist, future file/shell/screenshot tools.",
)
mcp.mount()
