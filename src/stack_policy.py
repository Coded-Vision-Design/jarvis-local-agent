"""Stack contract enforcement for delegated tasks.

Jarvis resolves the profile at queue time and stores it in
metadata.local_agent_stack. The runner prepends it to the prompt and
validates the workspace before commit on build-class tasks.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .task_evidence import is_build_class_task


def contract_from_metadata(metadata: dict[str, Any]) -> dict[str, Any] | None:
    raw = metadata.get("local_agent_stack")
    if isinstance(raw, dict) and raw.get("profile_id"):
        return raw
    return None


def format_stack_prompt_block(contract: dict[str, Any]) -> str:
    """XML-ish block prepended after coding standards."""
    payload = json.dumps(contract, indent=2)
    return (
        "<stack_contract trust=\"policy\">\n"
        "This block is authoritative for framework choice. "
        "Do not substitute a different stack unless the user explicitly "
        "overrode it in the task body.\n\n"
        f"{payload}\n"
        "</stack_contract>\n\n"
    )


def _read_package_deps(workspace: Path) -> dict[str, str]:
    pkg = workspace / "package.json"
    if not pkg.exists():
        return {}
    try:
        data = json.loads(pkg.read_text(encoding="utf-8"))
    except Exception:
        return {}
    deps: dict[str, str] = {}
    for key in ("dependencies", "devDependencies", "peerDependencies"):
        block = data.get(key) or {}
        if isinstance(block, dict):
            for name, ver in block.items():
                if isinstance(name, str):
                    deps[name] = str(ver) if ver is not None else ""
    return deps


def _path_exists(workspace: Path, rel: str) -> bool:
    return (workspace / rel).exists()


def validate_workspace_against_contract(
    workspace: Path,
    contract: dict[str, Any],
) -> tuple[bool, list[str]]:
    """Return (ok, list of failure messages). Empty contract skips checks."""
    profile = contract.get("profile_id") or ""
    if profile in ("inherit-repo", "ops-bash", "python-fastapi", ""):
        return True, []

    failures: list[str] = []

    deps = _read_package_deps(workspace)
    for name in contract.get("required_deps") or []:
        if isinstance(name, str) and name not in deps:
            failures.append(f"missing required dependency `{name}` in package.json")

    for name in contract.get("forbidden_deps") or []:
        if isinstance(name, str) and name in deps:
            failures.append(f"forbidden dependency `{name}` present in package.json")

    for group in contract.get("required_path_groups") or []:
        if not isinstance(group, list):
            continue
        paths = [p for p in group if isinstance(p, str)]
        if paths and not any(_path_exists(workspace, p) for p in paths):
            failures.append(
                f"missing required path (need one of): {', '.join(paths)}"
            )

    if profile == "next-static-export":
        for cfg in ("next.config.mjs", "next.config.ts", "next.config.js"):
            p = workspace / cfg
            if p.exists():
                text = p.read_text(encoding="utf-8", errors="replace").lower()
                if "export" not in text and "output" not in text:
                    failures.append(
                        f"{cfg} must set `output: 'export'` for cdv-sites-static deploy"
                    )
                break

    if profile in ("astro-static-demo", "next-static-export", "react-vite-spa"):
        src = workspace / "src"
        if src.is_dir():
            source_files = [
                f for f in src.rglob("*")
                if f.is_file() and f.suffix in {".astro", ".tsx", ".jsx", ".vue", ".ts", ".js"}
            ]
            if len(source_files) < 2:
                failures.append(
                    "scaffold looks incomplete — expected multiple source files under src/"
                )
        elif profile != "inherit-repo":
            failures.append("missing src/ directory for web scaffold")

    return len(failures) == 0, failures


def should_validate_stack(task: dict[str, Any], metadata: dict[str, Any]) -> bool:
    """Run stack validation on build-class tasks with a resolved profile."""
    contract = contract_from_metadata(metadata)
    if not contract:
        return False
    profile = contract.get("profile_id")
    if profile in ("inherit-repo", "ops-bash", None):
        return False
    return is_build_class_task(task)
