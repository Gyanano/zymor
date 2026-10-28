"""Zymor: the dreaming memory system for AI agents."""

from importlib.metadata import version as _package_version

try:
    __version__ = _package_version("zymor")
except Exception:
    __version__ = "0.2.1"
