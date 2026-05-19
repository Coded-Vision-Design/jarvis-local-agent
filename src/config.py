from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore")

    jarvis_base: str = "https://jarvis.codedvisiondesign.co.uk"
    jarvis_local_agent_token: str = ""

    anthropic_api_key: str = ""

    vllm_base: str = "http://host.docker.internal:18000"
    vllm_model: str = "Qwen/Qwen2.5-Coder-14B-Instruct-AWQ"

    discord_jarvis_tasks_webhook_url: str = ""

    poll_interval_seconds: int = 5
    heartbeat_interval_seconds: int = 30

    # Phase 22 — WebSocket gateway for cloud->agent task push.
    # When set, the agent opens a persistent WS to this URL and
    # triggers an immediate claim on every {type:"new_task"} push,
    # cutting task pickup from ~5 s to ~50 ms. The poll_loop stays
    # alive as a reconnect/cold-start fallback (cadence raised to
    # ws_fallback_poll_seconds once the WS is connected).
    jarvis_ws_url: str = "wss://jarvis-ws.codedvisiondesign.co.uk/agent"
    jarvis_ws_enabled: bool = True
    ws_fallback_poll_seconds: int = 30

    # Phase 22 — code-server URL surfaced by the agent's /ui HTML
    # ("Open VS Code" button). Default points at the local docker-
    # compose code-server (loopback). VPS agents set this to their
    # public Cloudflare tunnel URL (e.g.
    # https://vps-code.codedvisiondesign.co.uk) so clicking the
    # button from the workspace pane lands on the VPS editor
    # instead of the user's localhost.
    code_server_url: str = "http://127.0.0.1:17921"

    workspace_root: Path = Path("/workspace")
    repos_yml_path: Path = Path("/workspace/agent/repos.yml")

    log_level: Literal["debug", "info", "warning", "error"] = "info"

    qwen_home: Path = Path("/root/.qwen")

    # Hermes backend (creative / planning tasks via a second vLLM instance)
    # NOTE: port 18001 is now the real vLLM (behind the idle proxy on 18000).
    # Hermes runs on 18002 to avoid collision; you must spin up a second vLLM
    # process there manually. If unreachable, SmartBackend falls back to Qwen.
    hermes_base: str = "http://host.docker.internal:18002"
    hermes_model: str = "NousResearch/Hermes-3-Llama-3.1-8B"

    # Tier 4 (last-resort safety net): Cline Free.
    # When tiers 1-3 (Claude / Qwen) all fail, route to Cline's free proxy.
    # Sign in at cline.bot and paste the account API key into .env.
    cline_api_key: str = ""
    cline_api_base: str = "https://api.cline.bot/v1"
    cline_model: str = ""  # blank = let Cline pick the strongest available free model

    # GitHub token — needs repo + read:org scopes for private repo creation and health checks
    github_token: str = ""

    # ── Agent identity (multi-host hive mind) ───────────────────────────────
    # Unique label for this worker. Defaults to the container hostname.
    # Recommended convention: "<host>-<role>" e.g. "djohn-pc-gpu", "kvm2-cpu",
    # "mbp-claude-only". Surfaced on every heartbeat and run event so the VPS
    # UI can show which worker did what.
    agent_id: str = ""

    # Comma-separated tags advertising what this worker can do. The VPS will
    # honour metadata.local_agent_required_capabilities to target work.
    # Common tags: gpu, claude, qwen, hermes, codex, mobile-dev, comfyui
    # Auto-augmented at startup with detected capabilities; this is a manual
    # override / additional tags only.
    agent_capabilities: str = ""

    # Max tasks this agent runs concurrently. Default 1 per backend slot.
    # Bump on hosts with spare CPU + multiple Anthropic accounts.
    agent_max_concurrent: int = 1

    # Proactive health loop
    health_loop_enabled: bool = False
    health_check_interval_seconds: int = 600  # 10 minutes

    # VPS deploy target (Claudia subdomain pipeline)
    # Sites land at /var/www/cdv-sites/<slug>/ on the VPS, served via Traefik + nginx wildcard.
    deploy_host: str = "root@168.231.78.207"
    deploy_base_path: str = "/var/www/cdv-sites"
    deploy_domain: str = "codedvisiondesign.co.uk"
    deploy_enabled: bool = True


settings = Settings()
