from .base import Backend, BackendResult
from .claude import ClaudeBackend
from .qwen import QwenBackend


def get_backend(name: str) -> Backend:
    if name == "claude":
        return ClaudeBackend()
    if name == "qwen":
        return QwenBackend()
    raise ValueError(f"unknown backend: {name}")


__all__ = ["Backend", "BackendResult", "ClaudeBackend", "QwenBackend", "get_backend"]
