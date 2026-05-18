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
    vllm_model: str = "Qwen/Qwen3-14B-AWQ"

    discord_jarvis_tasks_webhook_url: str = ""

    poll_interval_seconds: int = 5
    heartbeat_interval_seconds: int = 30

    workspace_root: Path = Path("/workspace")
    repos_yml_path: Path = Path("/workspace/agent/repos.yml")

    log_level: Literal["debug", "info", "warning", "error"] = "info"


settings = Settings()
