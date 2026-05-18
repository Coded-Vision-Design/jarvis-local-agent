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


def add_to_whitelist(repo: str) -> None:
    """Append a repo to repos.yml if it is not already listed.

    Uses a read-modify-write with an atomic rename so concurrent access
    from the health loop or MCP server does not corrupt the file."""
    import tempfile, os

    path = settings.repos_yml_path
    path.parent.mkdir(parents=True, exist_ok=True)

    current = load_repos()
    if repo in current:
        return

    current.append(repo)
    data = {"repos": current}

    tmp_fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), suffix=".yml.tmp")
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            yaml.dump(data, f, default_flow_style=False, allow_unicode=True)
        os.replace(tmp_path, str(path))
    except Exception:
        os.unlink(tmp_path)
        raise
