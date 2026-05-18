from __future__ import annotations

import yaml

from .config import settings


def load_repos() -> list[str]:
    """Read the whitelist from repos.yml. Re-read on every call so edits land
    without restarting the container."""
    path = settings.repos_yml_path
    if not path.exists():
        return []
    raw = yaml.safe_load(path.read_text()) or {}
    repos = raw.get("repos") or []
    return [str(r).strip() for r in repos if isinstance(r, str) and r.strip()]


def is_whitelisted(repo: str) -> bool:
    return repo in load_repos()
