"""ComfyUI control + self-healing facility.

Lets the agent generate images via the local ComfyUI container by:

  1. Submitting a baseline SDXL/Flux workflow with custom positive/negative
     prompts (Jarvis maintains the workflow JSON in templates/comfyui_workflow.json
     so prompts swap cleanly without users editing the graph).
  2. Polling for completion, downloading the result image.
  3. Reading ComfyUI logs and detecting common errors (model missing, OOM,
     custom-node import failure) and offering structured fixes.

Privilege model (important - read once):

  ComfyUI runs in its own container (jarvis-comfyui). The agent does NOT
  exec into it as root. The agent CAN:

    - read/write ComfyUI workflows in C:/Users/djohn/Documents/ComfyUI/
      (mounted into both containers)
    - read /var/log/comfyui or container logs via the Docker socket the
      comfyui-proxy already has access to
    - submit prompts via ComfyUI's HTTP API
    - delete its OWN generated outputs

  The agent CANNOT:

    - apt-get inside the ComfyUI container
    - install Python packages into ComfyUI's venv
    - modify ComfyUI's source code
    - install custom_nodes (you do this; agent suggests which to install)

  This is intentional. ComfyUI's environment is treated as a sealed
  service; the agent talks to it via API, the same way it talks to vLLM.
  If a fix needs sudo or apt-get inside the container, the agent reports
  the diagnosis and proposes the exact command for you to run.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import uuid
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger("jarvis-agent.backend.comfyui")

COMFY_PROXY = os.environ.get("COMFYUI_PROXY_BASE", "http://comfyui-proxy:17926")
COMFY_TIMEOUT_S = float(os.environ.get("COMFYUI_TIMEOUT_S", "180"))
WORKFLOW_TEMPLATE = Path(__file__).resolve().parent.parent.parent / "templates" / "comfyui_workflow.json"


# ── Workflow template helpers ─────────────────────────────────────────────────

def _load_template() -> dict[str, Any]:
    """Load the baseline SDXL workflow. Falls back to a minimal hard-coded one
    if the file isn't present."""
    if WORKFLOW_TEMPLATE.exists():
        return json.loads(WORKFLOW_TEMPLATE.read_text(encoding="utf-8"))
    # Minimal SDXL workflow - single image, default sampler
    return _DEFAULT_WORKFLOW


_DEFAULT_WORKFLOW: dict[str, Any] = {
    "3": {  # KSampler
        "class_type": "KSampler",
        "inputs": {
            "seed": 0, "steps": 25, "cfg": 7.0, "sampler_name": "dpmpp_2m",
            "scheduler": "karras", "denoise": 1.0,
            "model": ["4", 0], "positive": ["6", 0], "negative": ["7", 0], "latent_image": ["5", 0],
        },
    },
    "4": {  # Load checkpoint
        "class_type": "CheckpointLoaderSimple",
        "inputs": {"ckpt_name": "sd_xl_base_1.0.safetensors"},
    },
    "5": {  # Empty latent
        "class_type": "EmptyLatentImage",
        "inputs": {"width": 1024, "height": 1024, "batch_size": 1},
    },
    "6": {  # Positive prompt
        "class_type": "CLIPTextEncode",
        "inputs": {"text": "", "clip": ["4", 1]},
    },
    "7": {  # Negative prompt
        "class_type": "CLIPTextEncode",
        "inputs": {"text": "blurry, low quality, distorted, ugly, deformed", "clip": ["4", 1]},
    },
    "8": {  # VAE decode
        "class_type": "VAEDecode",
        "inputs": {"samples": ["3", 0], "vae": ["4", 2]},
    },
    "9": {  # Save image
        "class_type": "SaveImage",
        "inputs": {"images": ["8", 0], "filename_prefix": "jarvis"},
    },
}


def _inject_prompts(
    workflow: dict[str, Any],
    positive: str,
    negative: str,
    seed: int | None = None,
    steps: int = 25,
    cfg: float = 7.0,
    width: int = 1024,
    height: int = 1024,
    checkpoint: str | None = None,
) -> dict[str, Any]:
    """Walk the workflow and substitute prompts + sampler params.

    Looks for CLIPTextEncode nodes (positive/negative by convention - first
    is positive, second is negative). Replaces their `text` field. Also
    updates KSampler seed/steps/cfg and EmptyLatentImage size.
    """
    import copy
    wf = copy.deepcopy(workflow)

    clip_nodes = [(k, v) for k, v in wf.items() if v.get("class_type") == "CLIPTextEncode"]
    if len(clip_nodes) >= 1:
        clip_nodes[0][1]["inputs"]["text"] = positive
    if len(clip_nodes) >= 2:
        clip_nodes[1][1]["inputs"]["text"] = negative

    for node in wf.values():
        if node.get("class_type") == "KSampler":
            ins = node["inputs"]
            if seed is not None:
                ins["seed"] = seed
            ins["steps"] = steps
            ins["cfg"] = cfg
        if node.get("class_type") == "EmptyLatentImage":
            node["inputs"]["width"] = width
            node["inputs"]["height"] = height
        if checkpoint and node.get("class_type") == "CheckpointLoaderSimple":
            node["inputs"]["ckpt_name"] = checkpoint

    return wf


# ── Public API ────────────────────────────────────────────────────────────────

async def generate_image(
    positive_prompt: str,
    negative_prompt: str = "blurry, low quality, distorted, ugly, deformed",
    *,
    seed: int | None = None,
    steps: int = 25,
    cfg: float = 7.0,
    width: int = 1024,
    height: int = 1024,
    checkpoint: str | None = None,
    timeout_s: float | None = None,
) -> dict[str, Any]:
    """Generate one image. Blocks until ComfyUI finishes (or timeout).

    Returns:
        {
            "ok": True,
            "image_path": "/workspace/.../output/jarvis_00012_.png",
            "prompt_id": "abc-123-..."
        }
    or {"ok": False, "error": "...", "diagnosis": {...}}
    """
    workflow = _inject_prompts(
        _load_template(),
        positive=positive_prompt,
        negative=negative_prompt,
        seed=seed, steps=steps, cfg=cfg,
        width=width, height=height,
        checkpoint=checkpoint,
    )
    client_id = uuid.uuid4().hex

    timeout = timeout_s or COMFY_TIMEOUT_S

    try:
        async with httpx.AsyncClient(timeout=timeout) as c:
            # 1. Submit prompt
            r = await c.post(
                f"{COMFY_PROXY}/prompt",
                json={"prompt": workflow, "client_id": client_id},
            )
            if r.status_code != 200:
                return {
                    "ok": False,
                    "error": f"ComfyUI submit failed (HTTP {r.status_code})",
                    "detail": r.text[:500],
                    "diagnosis": diagnose_error(r.text),
                }
            prompt_id = r.json().get("prompt_id")

            # 2. Poll history
            deadline_at = asyncio.get_event_loop().time() + timeout
            while asyncio.get_event_loop().time() < deadline_at:
                await asyncio.sleep(2)
                h = await c.get(f"{COMFY_PROXY}/history/{prompt_id}")
                if h.status_code != 200:
                    continue
                entries = h.json() or {}
                if prompt_id in entries:
                    outputs = entries[prompt_id].get("outputs", {})
                    images = []
                    for node_id, node_out in outputs.items():
                        for img in node_out.get("images", []):
                            images.append(img)
                    if images:
                        # ComfyUI returns relative names; map to host path
                        img0 = images[0]
                        host_path = (
                            f"/workspace/comfyui-output/{img0.get('subfolder','')}/{img0['filename']}"
                            .replace("//", "/")
                        )
                        return {
                            "ok": True,
                            "image_path": host_path,
                            "prompt_id": prompt_id,
                            "image_count": len(images),
                        }
            return {"ok": False, "error": f"ComfyUI timeout after {timeout}s", "prompt_id": prompt_id}

    except httpx.ConnectError:
        return {
            "ok": False,
            "error": f"ComfyUI proxy not reachable at {COMFY_PROXY}",
            "diagnosis": {"kind": "proxy_down", "fix": "Start the ComfyUI overlay: docker compose -f docker-compose.yml -f docker-compose.comfyui.yml up -d"},
        }
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


# ── Self-healing: error pattern matching ──────────────────────────────────────

_ERROR_PATTERNS = [
    (
        re.compile(r"checkpoint.*not.found|model.*not.*exist|No checkpoint", re.I),
        "missing_checkpoint",
        "The named checkpoint isn't in C:/Users/djohn/Documents/ComfyUI/models/checkpoints/. "
        "Download an SDXL .safetensors (e.g. sd_xl_base_1.0) and drop it there.",
    ),
    (
        re.compile(r"CUDA out of memory|torch\.cuda\.OutOfMemoryError|allocation.*GiB", re.I),
        "oom",
        "GPU ran out of VRAM. Either: lower image size to 512x512 / 768x768, or stop "
        "vLLM first (curl -X POST http://127.0.0.1:18000/admin/stop) so ComfyUI has full VRAM.",
    ),
    (
        re.compile(r"No module named '?([\w_.]+)'?", re.I),
        "missing_module",
        "A custom node depends on a Python package not in ComfyUI's venv. "
        "Run inside the container: docker exec -it jarvis-comfyui pip install <package>. "
        "Agent does NOT have sudo inside the comfyui container by design.",
    ),
    (
        re.compile(r"custom_node.*failed|node.*import.*error", re.I),
        "custom_node_load_failure",
        "A custom node failed to import. Check docker logs jarvis-comfyui for the "
        "full traceback. Often the node's requirements aren't installed.",
    ),
    (
        re.compile(r"VAE.*not.*found|vae.*safetensors.*missing", re.I),
        "missing_vae",
        "VAE file missing. Add it to C:/Users/djohn/Documents/ComfyUI/models/vae/",
    ),
]


def diagnose_error(error_text: str) -> dict[str, str]:
    """Try to identify the failure mode from ComfyUI's error message.

    Returns a dict with `kind` and `fix`. Falls back to "unknown" with the
    raw text trimmed."""
    for pattern, kind, fix in _ERROR_PATTERNS:
        if pattern.search(error_text):
            return {"kind": kind, "fix": fix}
    return {"kind": "unknown", "fix": f"Raw error (trimmed): {error_text[:400]}"}


async def fetch_recent_logs(lines: int = 100) -> str:
    """Read ComfyUI container logs via the Docker socket. Useful when a
    generation fails - the actual stack trace is in the container log."""
    try:
        async with httpx.AsyncClient(
            timeout=5.0,
            transport=httpx.AsyncHTTPTransport(uds="/var/run/docker.sock"),
        ) as c:
            r = await c.get(
                f"http://localhost/containers/jarvis-comfyui/logs"
                f"?stdout=1&stderr=1&tail={lines}",
            )
            return r.text if r.status_code == 200 else f"(could not fetch logs: HTTP {r.status_code})"
    except Exception as exc:
        return f"(could not fetch logs: {exc})"
