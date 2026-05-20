"""Confine caller-supplied filesystem paths to a set of allowed roots.

Endpoints accept paths from the cloud (image_path, apk_path, ...). Without a
guard, a crafted value like ``../../etc/passwd`` lets a caller read arbitrary
files (the vision backend base64-encodes the bytes into its reply). Every such
path must pass through :func:`safe_local_path`, which resolves symlinks/``..``
and rejects anything that escapes the allowed roots.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from .config import settings


def _allowed_roots() -> list[Path]:
    """Directories the agent may read caller-supplied files from.

    The workspace tree (where repos, screenshots and build outputs live) plus
    the system temp dir (transient screenshots / generated images).
    """
    roots = [settings.workspace_root, Path(tempfile.gettempdir())]
    resolved: list[Path] = []
    for r in roots:
        try:
            resolved.append(Path(r).resolve())
        except OSError:
            continue
    return resolved


def safe_local_path(raw: str | Path) -> Path:
    """Resolve ``raw`` and confine it to an allowed root.

    Returns the resolved :class:`~pathlib.Path` on success. Raises
    :class:`ValueError` if the path is malformed or escapes every allowed root.
    """
    resolved = Path(raw).resolve()
    for root in _allowed_roots():
        try:
            if resolved == root or resolved.is_relative_to(root):
                return resolved
        except ValueError:
            # Different drive on Windows -> not under this root.
            continue
    raise ValueError(f"path outside allowed roots: {raw!r}")
