from .base import Backend, BackendResult
from .claude import ClaudeBackend
from .cline_free import ClineFreeBackend
from .codex import CodexBackend
from .hermes import HermesBackend
from .qwen import QwenBackend
from .smart import SmartBackend


def get_backend(name: str) -> Backend:
    if name == "claude":
        return ClaudeBackend()
    if name == "qwen":
        return QwenBackend()
    if name == "hermes":
        return HermesBackend()
    if name == "codex":
        return CodexBackend()
    if name in ("cline_free", "cline-free", "clinefree"):
        return ClineFreeBackend()
    if name in ("smart", "auto"):
        return SmartBackend()
    raise ValueError(
        f"unknown backend: {name!r} — valid: claude, qwen, hermes, codex, cline_free, smart, auto"
    )


__all__ = [
    "Backend", "BackendResult",
    "ClaudeBackend", "QwenBackend", "HermesBackend",
    "CodexBackend", "ClineFreeBackend", "SmartBackend",
    "get_backend",
]
