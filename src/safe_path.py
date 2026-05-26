"""Confine caller-supplied filesystem paths to a set of allowed roots.

Endpoints accept paths from the cloud (image_path, apk_path, ...). Without a
guard, a crafted value like ``../../etc/passwd`` lets a caller read arbitrary
files (the vision backend base64-encodes the bytes into its reply). Every such
path must pass through :func:`safe_local_path`, which sanitises the input,
resolves symlinks/``..`` and rejects anything that escapes the allowed roots.
"""
from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path

from .config import settings

_MAX_PATH_LEN = 4096
_FORBIDDEN_PATTERNS = re.compile(r"[\x00-\x1f]|^\\\\|^//")


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


def _sanitise(raw: str | Path) -> str:
    """Reject obviously hostile input before any filesystem call."""
    s = os.fspath(raw)
    if not s or len(s) > _MAX_PATH_LEN:
        raise ValueError("path empty or too long")
    if _FORBIDDEN_PATTERNS.search(s):
        raise ValueError("path contains forbidden characters")
    return s


def safe_local_path(raw: str | Path) -> Path:
    """Resolve ``raw`` and confine it to an allowed root.

    Returns the resolved :class:`~pathlib.Path` on success. Raises
    :class:`ValueError` if the path is malformed or escapes every allowed root.
    The returned Path is guaranteed to be inside one of the allowed roots and
    safe for downstream filesystem operations.
    """
    s = _sanitise(raw)
    resolved = Path(s).resolve()
    for root in _allowed_roots():
        try:
            common = os.path.commonpath([str(resolved), str(root)])
            if common == str(root):
                return resolved
        except ValueError:
            # Different drive on Windows -> not under this root.
            continue
    raise ValueError(f"path outside allowed roots: {raw!r}")
