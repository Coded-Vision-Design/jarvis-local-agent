"""Agent identity + capability detection for the multi-host hive mind.

Each worker (Windows GPU box, VPS KVM, MacBook Pro, etc.) runs the same
jarvis-agent container but advertises a unique ID and the set of things it
can actually do. Surfaces this on every heartbeat so the VPS can:

  - Show "who is online" in the operator UI
  - Route capability-tagged tasks (metadata.local_agent_required_capabilities)
  - Load-balance naturally via the existing atomic claim_next

No code change needed on the VPS for basic operation - this module just
attaches metadata. The targeted-routing layer is opt-in: tasks that don't
request capabilities are claimed by whoever polls first."""
from __future__ import annotations

import logging
import os
import platform
import shutil
import socket
from functools import lru_cache
from typing import Any

import httpx

from .config import settings

log = logging.getLogger("jarvis-agent.identity")


@lru_cache(maxsize=1)
def agent_id() -> str:
    """Stable identifier for this worker.

    Order of precedence:
      1. AGENT_ID env var (explicit)
      2. /etc/agent-id file (per-host pin)
      3. container hostname
    """
    if settings.agent_id:
        return settings.agent_id
    try:
        with open("/etc/agent-id", encoding="utf-8") as f:
            pinned = f.read().strip()
            if pinned:
                return pinned
    except (FileNotFoundError, PermissionError):
        pass
    # docker-compose's container_name lands as hostname inside the container
    return socket.gethostname() or "unknown-agent"


@lru_cache(maxsize=1)
def detect_capabilities() -> list[str]:
    """Auto-detect what this agent can do. Returns a sorted, deduped list.

    Detected capabilities:
      - gpu             - NVIDIA GPU reachable (vLLM, ComfyUI)
      - claude          - claude CLI present + ANTHROPIC_API_KEY set
      - qwen            - vLLM at VLLM_BASE responds
      - hermes          - vLLM at HERMES_BASE responds
      - codex           - codex CLI present + OPENAI_API_KEY set (TODO)
      - deploy          - rsync + SSH key present (can run VPS deploys)
      - mobile-dev      - Flutter/Dart/Kotlin SDK present (code-server only)

    Plus everything in AGENT_CAPABILITIES env var as manual overrides.
    """
    caps: set[str] = set()

    # GPU - check for nvidia-smi reachability
    if shutil.which("nvidia-smi") is not None:
        caps.add("gpu")

    # Claude - reachable if the CLI is installed AND either an API key is set
    # OR an OAuth credentials file exists (the user's subscription path).
    if shutil.which("claude") is not None:
        claude_creds = os.path.exists("/root/.claude/.credentials.json") or os.path.exists(
            os.path.expanduser("~/.claude/.credentials.json")
        )
        if settings.anthropic_api_key or claude_creds:
            caps.add("claude")

    # Codex - either OAuth-saved tokens (subscription) or API key
    if shutil.which("codex") is not None:
        from .backends.codex import is_logged_in, has_api_key
        if is_logged_in() or has_api_key():
            caps.add("codex")

    # Qwen via the VLLM_BASE
    if _backend_reachable(settings.vllm_base):
        caps.add("qwen")

    # Hermes (rarely on by default)
    if _backend_reachable(settings.hermes_base):
        caps.add("hermes")

    # Vision model (MiniCPM-V on :18003 by default - co-resident with Qwen)
    vision_base = os.environ.get("VISION_BASE", "http://host.docker.internal:18003")
    if _backend_reachable(vision_base):
        caps.add("vision")

    # Deploy (rsync + SSH key)
    if shutil.which("rsync") is not None and os.path.exists("/root/.ssh/id_ed25519"):
        caps.add("deploy")

    # Mobile dev (only in code-server image, but we check anyway)
    if shutil.which("flutter") is not None:
        caps.add("mobile-dev")

    # Manual overrides
    if settings.agent_capabilities:
        for tag in settings.agent_capabilities.split(","):
            t = tag.strip().lower()
            if t:
                caps.add(t)

    return sorted(caps)


def _backend_reachable(base: str, timeout: float = 1.0) -> bool:
    """Quick GET /v1/models check. Used at boot to advertise; not on every heartbeat."""
    try:
        with httpx.Client(timeout=timeout) as c:
            r = c.get(f"{base.rstrip('/')}/v1/models")
            return r.status_code == 200
    except Exception:
        return False


def identity_payload() -> dict[str, Any]:
    """The blob sent on every heartbeat / claim so the VPS knows who we are.

    Cheap to compute - capabilities are cached after the first call."""
    return {
        "agent_id": agent_id(),
        "capabilities": detect_capabilities(),
        "platform": {
            "system": platform.system(),     # Linux / Darwin / Windows
            "release": platform.release(),
            "machine": platform.machine(),   # x86_64 / arm64
        },
        "max_concurrent": settings.agent_max_concurrent,
    }


def can_run(task_metadata: dict[str, Any]) -> tuple[bool, str]:
    """Check whether this agent satisfies a task's required capabilities.

    Tasks may include `metadata.local_agent_required_capabilities` as a
    comma-separated string or a list. Returns (True, "") if all required
    capabilities are present, else (False, "reason").

    Used by the poller to skip tasks targeted at other agents.
    """
    required = task_metadata.get("local_agent_required_capabilities")
    if not required:
        return True, ""  # no constraint - any agent can claim

    if isinstance(required, str):
        required_set = {c.strip().lower() for c in required.split(",") if c.strip()}
    elif isinstance(required, list):
        required_set = {str(c).strip().lower() for c in required if c}
    else:
        return True, ""  # malformed - don't block

    our_caps = set(detect_capabilities())
    missing = required_set - our_caps
    if missing:
        return False, f"missing capabilities: {','.join(sorted(missing))}"
    return True, ""


def log_identity_banner() -> None:
    """Print the identity at startup for operator visibility."""
    payload = identity_payload()
    log.info(
        "agent identity: id=%s caps=%s platform=%s/%s",
        payload["agent_id"],
        ",".join(payload["capabilities"]) or "(none)",
        payload["platform"]["system"],
        payload["platform"]["machine"],
    )
