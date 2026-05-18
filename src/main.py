from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi_mcp import FastApiMCP
from pydantic import BaseModel

from dataclasses import asdict

from .approvals import (
    add_pending,
    approve,
    approve_all,
    approve_batch,
    clear_all,
    create_session,
    end_session,
    list_pending,
    list_sessions,
    reject,
)
from .agent_identity import identity_payload, log_identity_banner
from .config import settings
from .health import health_loop
from .jarvis_client import get_client
from .poller import poll_loop
from .repos import is_whitelisted, load_repos
from .state import bus, inject_queues

logging.basicConfig(
    level=settings.log_level.upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("jarvis-agent")


# ── Lifespan: spawn the poll loop on startup, signal it to stop on shutdown ───
@asynccontextmanager
async def lifespan(app: FastAPI):
    log_identity_banner()
    stop = asyncio.Event()
    poll_task = asyncio.create_task(poll_loop(stop))
    health_task = asyncio.create_task(health_loop(stop))
    log.info("startup complete; polling jarvis at %s", settings.jarvis_base)
    try:
        yield
    finally:
        log.info("shutdown: signalling poll and health loops")
        stop.set()
        try:
            await asyncio.wait_for(
                asyncio.gather(poll_task, health_task, return_exceptions=True),
                timeout=5.0,
            )
        except asyncio.TimeoutError:
            poll_task.cancel()
            health_task.cancel()
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


# Phase 18d cross-origin. The cloud Jarvis at jarvis.codedvisiondesign.co.uk
# probes /health on every worker and iframes the agent /ui in the
# Workspace mode. Both surfaces hit CORS + CSP on the way back: without
# this middleware Cloud → Worker fails with "no Access-Control-Allow-
# Origin", and the iframe is refused by frame-ancestors.
_ALLOWED_ORIGINS = [
    "https://jarvis.codedvisiondesign.co.uk",
    # Loopback for the local-only dev box. The dev box hits its own
    # agent at localhost; allowing both 127.0.0.1 and localhost variants
    # so the browser's resolved hostname matches.
    "http://localhost:3000",
    "http://127.0.0.1:3000",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_ALLOWED_ORIGINS,
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


@app.middleware("http")
async def _allow_jarvis_iframe(request: Request, call_next):
    """Replace the default X-Frame-Options + CSP frame-ancestors with
    values that let the cloud Jarvis page iframe the agent UI in
    /workspace. Loopback localhost variants are also permitted so the
    dev box's local Jarvis dev server can iframe its sibling container.
    """
    response = await call_next(request)
    # X-Frame-Options is the legacy header; if anything upstream set it
    # to DENY the iframe gets blocked before CSP is even considered.
    if "x-frame-options" in response.headers:
        del response.headers["x-frame-options"]
    response.headers["Content-Security-Policy"] = (
        "frame-ancestors 'self' "
        "https://jarvis.codedvisiondesign.co.uk "
        "http://localhost:3000 "
        "http://127.0.0.1:3000"
    )
    return response


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


@app.get("/identity", operation_id="agent_identity")
async def agent_identity() -> dict:
    """Return this agent's identity, detected capabilities, and platform info.

    Used by the operator dashboard and by other agents in a hive-mind setup
    to discover what each peer can do."""
    return identity_payload()


# ── Vision endpoint (MiniCPM-V on :18003) ─────────────────────────────────────

class DescribeImageRequest(BaseModel):
    image_path: str | None = None      # path inside the container (e.g. /workspace/...)
    image_base64: str | None = None    # alternative: pass the bytes directly
    prompt: str = "Describe what you see in this image in detail."
    max_tokens: int = 512


# ── Android build / test facility ────────────────────────────────────────────

class AndroidBuildRequest(BaseModel):
    workspace: str           # path to Android project root
    variant: str = "Debug"
    clean: bool = False


class AndroidInstallRequest(BaseModel):
    apk_path: str
    device_id: str | None = None


@app.get("/android/devices", operation_id="android_devices")
async def android_devices() -> dict:
    """List connected Android devices (emulators or USB). Via host adb-server."""
    from .backends.android import list_devices
    return await list_devices()


@app.post("/android/build", operation_id="android_build")
async def android_build(body: AndroidBuildRequest) -> dict:
    """Run gradle assemble<Variant> in the given Android project workspace."""
    from .backends.android import build_apk
    return await build_apk(Path(body.workspace), variant=body.variant, clean=body.clean)


@app.post("/android/test", operation_id="android_test")
async def android_test(body: AndroidBuildRequest) -> dict:
    """Run unit tests (JVM only - no device needed)."""
    from .backends.android import run_unit_tests
    return await run_unit_tests(Path(body.workspace), variant=body.variant)


@app.post("/android/install", operation_id="android_install")
async def android_install(body: AndroidInstallRequest) -> dict:
    """adb install the named APK on the given (or sole) connected device."""
    from .backends.android import install_apk
    return await install_apk(body.apk_path, device_id=body.device_id)


class AndroidStudioRequest(BaseModel):
    project_path: str | None = None


class AndroidEmulatorRequest(BaseModel):
    avd: str | None = None


class AndroidScreenshotRequest(BaseModel):
    device_id: str | None = None
    out_path: str | None = None


class AndroidVerifyVisualRequest(BaseModel):
    expectation_prompt: str
    device_id: str | None = None
    capture_path: str | None = None


@app.post("/android/launch_studio", operation_id="android_launch_studio")
async def android_launch_studio(body: AndroidStudioRequest) -> dict:
    """Open Android Studio on the Windows host, optionally at a project path."""
    from .backends.android import launch_studio
    return launch_studio(body.project_path)


@app.post("/android/start_emulator", operation_id="android_start_emulator")
async def android_start_emulator(body: AndroidEmulatorRequest) -> dict:
    """Start an Android emulator on the Windows host."""
    from .backends.android import start_emulator
    return start_emulator(body.avd)


@app.post("/android/stop_emulator", operation_id="android_stop_emulator")
async def android_stop_emulator(body: AndroidEmulatorRequest) -> dict:
    """Stop an emulator (or all tracked emulators if avd omitted)."""
    from .backends.android import stop_emulator
    return stop_emulator(body.avd)


@app.get("/android/host_status", operation_id="android_host_status")
async def android_host_status() -> dict:
    """Read the Windows host watcher status (which Studio/emulator is up)."""
    from .backends.android import host_status
    return host_status()


@app.get("/android/avds", operation_id="android_list_avds")
async def android_list_avds() -> dict:
    """List configured Android Virtual Devices."""
    from .backends.android import list_avds
    return await list_avds()


@app.post("/android/wait_for_device", operation_id="android_wait_for_device")
async def android_wait_for_device(timeout_s: int = 120) -> dict:
    """Block until at least one device is fully booted and ready."""
    from .backends.android import wait_for_device
    return await wait_for_device(timeout_s)


@app.post("/android/screenshot", operation_id="android_screenshot")
async def android_screenshot(body: AndroidScreenshotRequest) -> dict:
    """Capture the current emulator/device screen via adb screencap + pull."""
    from .backends.android import screenshot
    return await screenshot(device_id=body.device_id, out_path=body.out_path)


@app.post("/android/verify_visual", operation_id="android_verify_visual")
async def android_verify_visual(body: AndroidVerifyVisualRequest) -> dict:
    """End-to-end visual check: screenshot the emulator, ask the vision model
    a specific question about it. Returns plain-English answer."""
    from .backends.android import verify_visual
    return await verify_visual(
        expectation_prompt=body.expectation_prompt,
        device_id=body.device_id,
        capture_path=body.capture_path,
    )


# ── ComfyUI (image generation facility) ──────────────────────────────────────

class GenerateImageRequest(BaseModel):
    positive_prompt: str
    negative_prompt: str = "blurry, low quality, distorted, ugly, deformed"
    seed: int | None = None
    steps: int = 25
    cfg: float = 7.0
    width: int = 1024
    height: int = 1024
    checkpoint: str | None = None
    timeout_s: float | None = None


@app.post("/comfyui/generate", operation_id="generate_image")
async def comfyui_generate(body: GenerateImageRequest) -> dict:
    """Generate one image via the local ComfyUI container.

    Blocks until completion (or timeout). Returns the on-disk path of the
    image. If ComfyUI errors, returns a diagnosis with the suggested fix.
    """
    from .backends.comfyui import generate_image
    return await generate_image(
        positive_prompt=body.positive_prompt,
        negative_prompt=body.negative_prompt,
        seed=body.seed,
        steps=body.steps,
        cfg=body.cfg,
        width=body.width,
        height=body.height,
        checkpoint=body.checkpoint,
        timeout_s=body.timeout_s,
    )


@app.get("/comfyui/logs", operation_id="comfyui_logs")
async def comfyui_logs(lines: int = 100) -> dict:
    """Fetch the last N lines of the ComfyUI container log via the Docker socket."""
    from .backends.comfyui import fetch_recent_logs
    return {"logs": await fetch_recent_logs(lines)}


# ── NitroGen (game-playing facility) ──────────────────────────────────────────

class NitrogenPlayRequest(BaseModel):
    game: str                       # "notepad.exe" or full path
    duration_s: int = 0             # 0 = until stopped
    kb_mouse: bool = False


@app.post("/nitrogen/play", operation_id="nitrogen_play")
async def nitrogen_play(body: NitrogenPlayRequest) -> dict:
    """Start a NitroGen game-playing session on the Windows host.

    The agent writes a command file; a host-side PowerShell watcher
    (scripts/nitrogen-host-watcher.ps1) picks it up and launches play.py
    against the named process. Poll /nitrogen/status to see when it
    actually started.
    """
    from .backends import nitrogen
    return nitrogen.play(body.game, duration_s=body.duration_s, kb_mouse=body.kb_mouse)


@app.post("/nitrogen/stop", operation_id="nitrogen_stop")
async def nitrogen_stop_endpoint() -> dict:
    """Stop the current NitroGen session."""
    from .backends import nitrogen
    return nitrogen.stop()


@app.get("/nitrogen/status", operation_id="nitrogen_status")
async def nitrogen_status() -> dict:
    """Current NitroGen session status (game, pid, started_at, etc.)."""
    from .backends import nitrogen
    return nitrogen.status()


@app.post("/vision/describe", operation_id="describe_image")
async def describe_image_endpoint(body: DescribeImageRequest) -> dict:
    """Send an image + prompt to the local vision model.

    Useful for Playwright screenshot inspection ('does this look right?'),
    ComfyUI output review, UI bug triage. Falls back gracefully if the
    vision backend isn't warm."""
    from .backends.vision import describe_image, vision_chat, _image_to_data_url

    if not body.image_path and not body.image_base64:
        raise HTTPException(400, "provide either image_path or image_base64")

    if body.image_path:
        from pathlib import Path
        p = Path(body.image_path)
        if not p.exists():
            raise HTTPException(404, f"image not found: {body.image_path}")
        result = await describe_image(p, body.prompt, max_tokens=body.max_tokens)
    else:
        data_url = f"data:image/png;base64,{body.image_base64}"
        result = await vision_chat([{
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": data_url}},
                {"type": "text", "text": body.prompt},
            ],
        }], max_tokens=body.max_tokens)

    if result is None:
        raise HTTPException(
            503,
            "Vision backend unavailable. Start it with: "
            "sudo bash /workspace/agent/scripts/start-vision-fg.sh",
        )
    return {"description": result}


# ── Live stream + UI ──────────────────────────────────────────────────────────

_UI_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Jarvis Local Agent — Live</title>
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: #0d0d0d; color: #e0e0e0; font-family: 'Cascadia Code', 'Fira Code', monospace; font-size: 13px; display: flex; height: 100vh; overflow: hidden; }
  #sidebar { width: 280px; min-width: 200px; border-right: 1px solid #2a2a2a; display: flex; flex-direction: column; padding: 12px; gap: 12px; overflow-y: auto; }
  #main { flex: 1; display: flex; flex-direction: column; overflow: hidden; }
  h1 { font-size: 14px; color: #7ecfff; letter-spacing: 0.05em; padding-bottom: 8px; border-bottom: 1px solid #2a2a2a; }
  #status { font-size: 11px; color: #666; }
  #status.connected { color: #4caf50; }
  #status.disconnected { color: #f44336; }
  .task-card { background: #161616; border: 1px solid #2a2a2a; border-radius: 6px; padding: 10px; display: flex; flex-direction: column; gap: 6px; }
  .task-card .task-id { font-size: 11px; color: #888; }
  .task-card .task-title { font-size: 12px; color: #ccc; word-break: break-word; }
  .task-card .task-meta { font-size: 11px; color: #5a9fd4; }
  .task-card .inject-row { display: flex; gap: 4px; }
  .task-card .inject-row input { flex: 1; background: #0d0d0d; border: 1px solid #333; color: #e0e0e0; padding: 4px 6px; border-radius: 4px; font-size: 12px; font-family: inherit; }
  .task-card .inject-row input:focus { outline: none; border-color: #5a9fd4; }
  .task-card .inject-row button { background: #1a3a5c; border: 1px solid #2a5a8c; color: #7ecfff; padding: 4px 10px; border-radius: 4px; cursor: pointer; font-size: 12px; }
  .task-card .inject-row button:hover { background: #2a5a8c; }
  #no-tasks { color: #444; font-size: 12px; text-align: center; padding: 20px 0; }
  #vscode-link { font-size: 12px; color: #7ecfff; text-decoration: none; padding: 6px 10px; border: 1px solid #2a5a8c; border-radius: 4px; text-align: center; }
  #vscode-link:hover { background: #1a3a5c; }
  #log { flex: 1; overflow-y: auto; padding: 10px; display: flex; flex-direction: column; gap: 3px; }
  #log-header { padding: 8px 12px; border-bottom: 1px solid #2a2a2a; display: flex; align-items: center; justify-content: space-between; }
  #log-header span { font-size: 12px; color: #888; }
  #clear-btn { background: none; border: 1px solid #333; color: #666; padding: 3px 8px; border-radius: 4px; cursor: pointer; font-size: 11px; }
  #clear-btn:hover { border-color: #555; color: #aaa; }
  .ev { padding: 3px 6px; border-radius: 3px; line-height: 1.5; border-left: 3px solid transparent; font-size: 12px; cursor: pointer; }
  .ev .ev-meta { color: #555; font-size: 10px; margin-right: 6px; }
  .ev .ev-task { color: #666; font-size: 10px; margin-right: 6px; }
  .ev-tool_call { border-left-color: #5a9fd4; background: #0e1e2e; }
  .ev-tool_call .ev-kind { color: #5a9fd4; }
  .ev-assistant_text { border-left-color: #333; }
  .ev-assistant_text .ev-kind { color: #888; }
  .ev-error { border-left-color: #f44336; background: #1e0e0e; }
  .ev-error .ev-kind { color: #f44336; }
  .ev-jarvis_note { border-left-color: #555; color: #666; font-style: italic; }
  .ev-jarvis_note .ev-kind { color: #555; }
  .ev .ev-body { color: #ccc; word-break: break-all; }
  .ev .ev-body.truncated::after { content: ' [click to expand]'; color: #555; }
  .ev.expanded .ev-body { white-space: pre-wrap; }
</style>
</head>
<body>
<div id="sidebar">
  <h1>Jarvis Agent</h1>
  <div id="status" class="disconnected">⬤ disconnected</div>
  <a id="vscode-link" href="http://127.0.0.1:17921" target="_blank">→ Open VS Code</a>
  <div id="tasks-container">
    <div id="no-tasks">No active tasks</div>
  </div>
</div>
<div id="main">
  <div id="log-header">
    <span id="ev-count">0 events</span>
    <button id="clear-btn" onclick="clearLog()">Clear</button>
  </div>
  <div id="log"></div>
</div>
<script>
const MAX_ROWS = 2000;
let evCount = 0;
let es = null;
const taskCards = {};

function ts(t) {
  const d = new Date(t * 1000);
  return d.toTimeString().slice(0,8);
}

function shortPayload(kind, payload) {
  if (!payload) return '';
  if (kind === 'tool_call') {
    const args = payload.args ? JSON.stringify(payload.args).slice(0, 120) : '';
    return `<span class="ev-kind">tool</span> <b>${payload.tool || ''}</b> ${escHtml(args)}`;
  }
  if (kind === 'assistant_text') {
    return `<span class="ev-kind">text</span> ${escHtml((payload.text || '').slice(0, 300))}`;
  }
  if (kind === 'error') {
    return `<span class="ev-kind">error</span> ${escHtml(payload.message || payload.summary || JSON.stringify(payload).slice(0, 200))}`;
  }
  if (kind === 'jarvis_note') {
    return `<span class="ev-kind">note</span> ${escHtml(payload.message || '')}`;
  }
  return `<span class="ev-kind">${escHtml(kind)}</span> ${escHtml(JSON.stringify(payload).slice(0, 200))}`;
}

function escHtml(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

function appendEvent(ev) {
  if (ev.kind === 'heartbeat') return;
  evCount++;
  document.getElementById('ev-count').textContent = evCount + ' events';
  const log = document.getElementById('log');
  const div = document.createElement('div');
  div.className = `ev ev-${ev.kind}`;
  const full = JSON.stringify(ev.payload, null, 2);
  const truncated = shortPayload(ev.kind, ev.payload);
  div.innerHTML = `<span class="ev-meta">${ts(ev.ts)}</span><span class="ev-task">#${ev.task_id}</span>${truncated}`;
  div.addEventListener('click', () => {
    div.classList.toggle('expanded');
    if (div.classList.contains('expanded')) {
      div.querySelector('.ev-body') && (div.querySelector('.ev-body').textContent = full);
    } else {
      div.innerHTML = `<span class="ev-meta">${ts(ev.ts)}</span><span class="ev-task">#${ev.task_id}</span>${truncated}`;
    }
  });
  log.appendChild(div);
  // prune old rows
  while (log.children.length > MAX_ROWS) log.removeChild(log.firstChild);
  log.scrollTop = log.scrollHeight;
}

function renderActiveTasks(active) {
  const container = document.getElementById('tasks-container');
  const ids = Object.keys(active).map(Number);
  // remove stale cards
  Object.keys(taskCards).forEach(id => {
    if (!active[id]) { taskCards[id].remove(); delete taskCards[id]; }
  });
  if (ids.length === 0) {
    if (!document.getElementById('no-tasks')) {
      const p = document.createElement('div'); p.id = 'no-tasks'; p.textContent = 'No active tasks';
      container.appendChild(p);
    }
    return;
  }
  const noTasks = document.getElementById('no-tasks');
  if (noTasks) noTasks.remove();
  ids.forEach(id => {
    if (taskCards[id]) return;
    const m = active[id];
    const card = document.createElement('div'); card.className = 'task-card';
    card.innerHTML = `
      <div class="task-id">#${id}</div>
      <div class="task-title">${escHtml((m.title || '').slice(0, 80))}</div>
      <div class="task-meta">${escHtml(m.backend || '')} · ${escHtml(m.repo || '')}</div>
      <div class="inject-row">
        <input type="text" placeholder="Inject message to stdin…" id="inj-${id}">
        <button onclick="inject(${id})">→</button>
      </div>`;
    container.appendChild(card);
    taskCards[id] = card;
    const inp = card.querySelector('input');
    inp.addEventListener('keydown', e => { if (e.key === 'Enter') inject(id); });
  });
}

function inject(taskId) {
  const inp = document.getElementById('inj-' + taskId);
  const msg = inp ? inp.value.trim() : '';
  if (!msg) return;
  fetch('/tasks/' + taskId + '/inject', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({message: msg}),
  }).then(r => {
    if (r.ok) { inp.value = ''; inp.placeholder = 'sent ✓'; setTimeout(() => inp.placeholder = 'Inject message to stdin…', 2000); }
    else inp.placeholder = 'failed ✗';
  }).catch(() => { inp.placeholder = 'error ✗'; });
}

function clearLog() {
  document.getElementById('log').innerHTML = '';
  evCount = 0;
  document.getElementById('ev-count').textContent = '0 events';
}

function connect() {
  if (es) es.close();
  es = new EventSource('/stream/live');
  es.onopen = () => {
    const s = document.getElementById('status');
    s.textContent = '⬤ connected'; s.className = 'connected';
  };
  es.onmessage = e => {
    let data;
    try { data = JSON.parse(e.data); } catch { return; }
    if (data.type === 'hello') {
      renderActiveTasks(data.active || {});
      (data.replay || []).forEach(appendEvent);
      return;
    }
    if (data.kind) appendEvent(data);
    // refresh task list on each event to pick up new/ended tasks
    fetch('/stream/live').catch(() => {});
  };
  es.onerror = () => {
    const s = document.getElementById('status');
    s.textContent = '⬤ disconnected'; s.className = 'disconnected';
    setTimeout(connect, 3000);
  };
}

// Poll active tasks every 5s to sync the sidebar
setInterval(() => {
  fetch('/stream/active').then(r => r.json()).then(data => renderActiveTasks(data)).catch(() => {});
}, 5000);

connect();
</script>
</body>
</html>"""


@app.get("/stream/live", include_in_schema=False)
async def stream_live() -> StreamingResponse:
    """SSE: broadcast all active task events + 200-event replay on connect."""
    q = bus.subscribe()

    async def gen():
        hello = json.dumps({
            "type": "hello",
            "active": bus.active_tasks(),
            "replay": bus.replay(),
        })
        yield f"data: {hello}\n\n"
        try:
            while True:
                try:
                    ev = await asyncio.wait_for(q.get(), 15.0)
                except asyncio.TimeoutError:
                    yield ": heartbeat\n\n"
                    continue
                yield f"data: {json.dumps(ev)}\n\n"
        finally:
            bus.unsubscribe(q)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/stream/task/{task_id}", include_in_schema=False)
async def stream_task(task_id: int) -> StreamingResponse:
    """SSE: events for a single task."""
    q = bus.subscribe(task_id)

    async def gen():
        try:
            while True:
                try:
                    ev = await asyncio.wait_for(q.get(), 15.0)
                except asyncio.TimeoutError:
                    yield ": heartbeat\n\n"
                    continue
                yield f"data: {json.dumps(ev)}\n\n"
        finally:
            bus.unsubscribe(q, task_id)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/stream/active", include_in_schema=False)
async def stream_active() -> dict:
    """Returns currently active tasks (used by UI to sync sidebar)."""
    return bus.active_tasks()


class InjectRequest(BaseModel):
    message: str


@app.post("/tasks/{task_id}/inject", operation_id="inject_message")
async def inject_message(task_id: int, body: InjectRequest) -> dict:
    """Inject a message into the stdin of the running sub-agent process."""
    q = inject_queues.get(task_id)
    if not q:
        raise HTTPException(status_code=404, detail="No active task with that id")
    try:
        q.put_nowait(body.message)
    except asyncio.QueueFull:
        raise HTTPException(status_code=429, detail="Inject queue full — try again shortly")
    return {"ok": True}


@app.get("/ui", response_class=HTMLResponse, include_in_schema=False)
async def serve_ui() -> HTMLResponse:
    """Self-contained dark terminal UI for watching task progress in real-time."""
    return HTMLResponse(_UI_HTML)


# ── Task management endpoints (used by MCP server + Cline) ────────────────────

class CreateTaskRequest(BaseModel):
    title: str
    body: str
    repo: str
    backend: str = "smart"
    create_repo: bool = False


@app.get("/tasks/list", operation_id="list_tasks_combined")
async def list_tasks(limit: int = 10) -> dict:
    """Return active tasks from the bus + recent tasks from the VPS."""
    active = bus.active_tasks()
    client = get_client()
    recent = await client.get_recent_tasks(limit=limit)
    return {"active": active, "recent": recent}


@app.post("/tasks/create", operation_id="create_task")
async def create_task(body: CreateTaskRequest) -> dict:
    """Create a new Jarvis task. Validates the repo is whitelisted (or will be created)."""
    if not body.create_repo and not is_whitelisted(body.repo):
        raise HTTPException(
            status_code=400,
            detail=(
                f"Repo '{body.repo}' is not whitelisted. "
                f"Add it to /workspace/agent/repos.yml or set create_repo=true."
            ),
        )

    client = get_client()
    task = await client.create_task(
        title=body.title,
        body=body.body,
        repo=body.repo,
        backend=body.backend,
    )
    if task is None:
        raise HTTPException(status_code=502, detail="Jarvis server rejected the task creation request.")

    # Embed create_repo flag in metadata so runner.py sees it
    if body.create_repo:
        task_id = task.get("id")
        if task_id:
            await client.merge_metadata(task_id, {"local_agent_create_repo": True})

    return {"ok": True, "task_id": task.get("id"), "task": task}


@app.get("/tasks/{task_id}/status", operation_id="get_task_status")
async def task_status(task_id: int) -> dict:
    """Return current status and recent events for a task."""
    active = bus.active_tasks()
    task_info = active.get(task_id)
    # Replay recent events for this task from the bus ring buffer
    events = [ev for ev in bus.replay() if ev.get("task_id") == task_id]
    return {
        "task_id": task_id,
        "active": task_info is not None,
        "task": task_info or {},
        "events": events[-20:],  # last 20 events
    }


# ── Approval queue endpoints ──────────────────────────────────────────────────

class AddPendingRequest(BaseModel):
    title: str
    body: str
    repo: str
    backend: str = "smart"
    source: str = "manual"
    severity: str = "medium"


class BatchApproveRequest(BaseModel):
    approval_ids: list[str]


class ApproveAllRequest(BaseModel):
    source_filter: str | None = None
    severity_filter: str | None = None


class CreateSessionRequest(BaseModel):
    name: str
    repo: str | None = None
    backends: list[str] = []
    max_tasks: int = 10
    duration_seconds: int = 3600
    sequential: bool = True


async def _push_pending_to_vps(p) -> dict | None:
    """Promote a pending approval to a real Jarvis task on the VPS."""
    client = get_client()
    task = await client.create_task(
        title=p.title,
        body=p.body,
        repo=p.repo,
        backend=p.backend,
    )
    if task:
        log.info("promoted approval %s to task %s", p.id[:8], task.get("id"))
    return task


@app.get("/approvals", operation_id="list_pending_approvals")
async def get_approvals() -> dict:
    """List all pending approvals (newest first)."""
    items = list_pending()
    return {"pending": [asdict(it) for it in items], "count": len(items)}


@app.post("/approvals", operation_id="add_pending_approval")
async def post_approval(body: AddPendingRequest) -> dict:
    """Queue a new task for approval (instead of running immediately)."""
    if not is_whitelisted(body.repo):
        raise HTTPException(400, f"repo {body.repo!r} not whitelisted")
    p = add_pending(
        title=body.title,
        body=body.body,
        repo=body.repo,
        backend=body.backend,
        source=body.source,
        severity=body.severity,
    )
    return {"ok": True, "approval_id": p.id, "approval": asdict(p)}


@app.post("/approvals/{approval_id}/approve", operation_id="approve_task")
async def post_approve(approval_id: str) -> dict:
    p = approve(approval_id)
    if not p:
        raise HTTPException(404, "approval not found")
    task = await _push_pending_to_vps(p)
    return {
        "ok": True,
        "approval": asdict(p),
        "task": task,
    }


@app.post("/approvals/{approval_id}/reject", operation_id="reject_task")
async def post_reject(approval_id: str) -> dict:
    ok = reject(approval_id)
    if not ok:
        raise HTTPException(404, "approval not found")
    return {"ok": True}


@app.post("/approvals/approve-batch", operation_id="approve_batch")
async def post_approve_batch(body: BatchApproveRequest) -> dict:
    approved = approve_batch(body.approval_ids)
    tasks = []
    for p in approved:
        t = await _push_pending_to_vps(p)
        tasks.append(t)
    return {"ok": True, "approved_count": len(approved), "tasks": tasks}


@app.post("/approvals/approve-all", operation_id="approve_all")
async def post_approve_all(body: ApproveAllRequest) -> dict:
    """Approve everything matching the optional source/severity filters.

    Examples:
      {} - approve everything pending
      {"source_filter": "health_loop"} - approve all health findings
      {"severity_filter": "high"} - approve only high/critical
    """
    approved = approve_all(body.source_filter, body.severity_filter)
    tasks = []
    for p in approved:
        t = await _push_pending_to_vps(p)
        tasks.append(t)
    return {"ok": True, "approved_count": len(approved), "tasks": tasks}


@app.delete("/approvals", operation_id="clear_all_approvals")
async def delete_all_approvals() -> dict:
    count = clear_all()
    return {"ok": True, "rejected_count": count}


@app.get("/approvals/sessions", operation_id="list_e2e_sessions")
async def get_sessions() -> dict:
    return {"sessions": [asdict(s) for s in list_sessions()]}


@app.post("/approvals/sessions", operation_id="create_e2e_session")
async def post_session(body: CreateSessionRequest) -> dict:
    """Create an E2E approval session. While active, tasks tagged with this
    session_id auto-approve and run sequentially without per-task prompts."""
    session = create_session(
        name=body.name,
        repo=body.repo,
        backends=body.backends,
        max_tasks=body.max_tasks,
        duration_seconds=body.duration_seconds,
        sequential=body.sequential,
    )
    return {"ok": True, "session_id": session.id, "session": asdict(session)}


@app.delete("/approvals/sessions/{session_id}", operation_id="end_e2e_session")
async def delete_session(session_id: str) -> dict:
    ok = end_session(session_id)
    if not ok:
        raise HTTPException(404, "session not found")
    return {"ok": True}


# ── MCP ───────────────────────────────────────────────────────────────────────
# Expose endpoints as MCP tools at /mcp (SSE).
# Sub-agent CLIs can mount this via --mcp-config.
mcp = FastApiMCP(
    app,
    name="jarvis-local-agent",
    description="Local sub-agent: repo whitelist, future file/shell/screenshot tools.",
)
mcp.mount()
