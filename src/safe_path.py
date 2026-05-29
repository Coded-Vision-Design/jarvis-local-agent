"""Confine caller-supplied filesystem paths to a set of allowed roots.

Endpoints accept paths from the cloud (image_path, apk_path, ...). Without a
guard, a crafted value like ``../../etc/passwd`` lets a caller read arbitrary
files (the vision backend base64-encodes the bytes into its reply). Every such
path must pass through :func:`safe_local_path`, which validates the input at
the string level and rebuilds the path from a trusted root - never invoking
any filesystem operation (``Path.resolve``, ``os.path.realpath``, ...) on the
raw value.
"""
from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path, PurePath

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
    """Validate ``raw`` and rebuild it under an allowed root.

    The tainted input is never passed to a filesystem-touching operation. We
    normalise the string with :func:`os.path.normpath`, reject any residual
    ``..`` segment, then confirm containment with :func:`os.path.commonpath`
    (the CodeQL-recognised sanitiser for path-injection). The returned
    :class:`~pathlib.Path` is built from the trusted, pre-resolved root plus
    only the validated suffix components.
    """
    s = _sanitise(raw)
    if not os.path.isabs(s):
        raise ValueError("path must be absolute")

    # Pure string normalisation - no filesystem access on tainted input.
    norm = os.path.normpath(s)
    pure_parts = PurePath(norm).parts
    if any(part == ".." for part in pure_parts):
        raise ValueError("path traversal not allowed")

    for root in _allowed_roots():
        root_str = str(root)
        try:
            common = os.path.commonpath([norm, root_str])
        except ValueError:
            # Different drives on Windows - cannot share a common path.
            continue
        if common != root_str:
            continue
        # Rebuild from the *trusted* root with only the vetted suffix parts.
        suffix_parts = pure_parts[len(PurePath(root_str).parts):]
        return root.joinpath(*suffix_parts)

    raise ValueError(f"path outside allowed roots: {raw!r}")
