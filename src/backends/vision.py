"""Vision backend - one-shot image+text inference against a local vLLM serving
a vision-language model (MiniCPM-V by default, swappable).

Unlike claude/qwen/hermes, this backend doesn't run a long-form coding task.
It answers a single question about one or more images and returns the text.
The runner won't invoke this directly via the smart router - it's exposed
as a utility function that other code calls (e.g. the Playwright screenshot
inspection helper).

Usage from elsewhere in the codebase:

    from .backends.vision import describe_image, vision_chat

    desc = await describe_image(png_path, "Describe what is on screen")
    answer = await vision_chat([
        {"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}},
            {"type": "text", "text": "Is the header sticky to the top?"},
        ]},
    ])
"""
from __future__ import annotations

import base64
import logging
import os
from pathlib import Path
from typing import Any

import httpx

from ..safe_path import safe_local_path

log = logging.getLogger("jarvis-agent.backend.vision")

VISION_BASE = os.environ.get("VISION_BASE", "http://host.docker.internal:18003")
VISION_MODEL = os.environ.get("VISION_MODEL", "vision")  # served-model-name from the launcher
VISION_TIMEOUT = float(os.environ.get("VISION_TIMEOUT_S", "60"))


def _image_to_data_url(path: str | Path) -> str:
    """Encode a local image file as a data URL the OpenAI-compat API accepts."""
    p = safe_local_path(path)
    ext = p.suffix.lower().lstrip(".") or "png"
    mime = {"jpg": "jpeg"}.get(ext, ext)
    data = p.read_bytes()
    b64 = base64.b64encode(data).decode()
    return f"data:image/{mime};base64,{b64}"


async def vision_chat(
    messages: list[dict[str, Any]],
    *,
    max_tokens: int = 512,
    temperature: float = 0.1,
) -> str | None:
    """Low-level call - pass an OpenAI-shaped messages list with image_url
    parts. Returns the assistant text on success, None on failure."""
    payload: dict[str, Any] = {
        "model": VISION_MODEL,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    try:
        async with httpx.AsyncClient(timeout=VISION_TIMEOUT) as c:
            r = await c.post(
                f"{VISION_BASE.rstrip('/')}/v1/chat/completions",
                json=payload,
                headers={"Authorization": "Bearer unused"},
            )
            if r.status_code != 200:
                log.warning("vision_chat HTTP %s: %s", r.status_code, r.text[:200])
                return None
            return r.json()["choices"][0]["message"]["content"]
    except httpx.ConnectError:
        log.info("vision backend at %s not reachable - is it warm?", VISION_BASE)
        return None
    except Exception as exc:
        log.exception("vision_chat error: %s", exc)
        return None


async def describe_image(
    image_path: str | Path,
    prompt: str = "Describe what you see in this image in detail.",
    *,
    max_tokens: int = 512,
) -> str | None:
    """Highest-level helper - point at a local image file, get a description back."""
    return await vision_chat(
        messages=[{
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": _image_to_data_url(image_path)}},
                {"type": "text", "text": prompt},
            ],
        }],
        max_tokens=max_tokens,
    )


async def is_vision_available() -> bool:
    """Quick check used by the agent dashboard + capability advertising."""
    try:
        async with httpx.AsyncClient(timeout=2.0) as c:
            r = await c.get(f"{VISION_BASE.rstrip('/')}/v1/models")
            return r.status_code == 200
    except Exception:
        return False
