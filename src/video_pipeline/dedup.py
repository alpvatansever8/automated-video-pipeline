"""Duplicate-download guard.

Stores the URL and content hash of every downloaded item so the same content is not
downloaded twice.
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path

from video_pipeline.config import cache_dir

log = logging.getLogger(__name__)

_registry: dict = {}  # {url: file path} and {md5 hash: file path}


def _registry_file() -> Path:
    return cache_dir() / "downloaded_registry.json"


def _load() -> None:
    global _registry
    path = _registry_file()
    if path.exists():
        try:
            _registry = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            _registry = {}


def _save() -> None:
    path = _registry_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_registry, ensure_ascii=False, indent=2), encoding="utf-8")


def _md5(path: Path) -> str | None:
    try:
        return hashlib.md5(path.read_bytes()).hexdigest()
    except OSError:
        return None


def is_duplicate_url(url: str) -> bool:
    """Return True if this URL was downloaded before."""
    _load()
    return url in _registry


def is_duplicate_file(path: Path) -> bool:
    """Return True if a file with the same content (hash) was downloaded before."""
    _load()
    if not path.exists():
        return False
    digest = _md5(path)
    return digest is not None and digest in _registry


def register(url: str, path: Path) -> None:
    """Record a URL and its file in the registry."""
    _load()
    _registry[url] = str(path)
    if path.exists():
        digest = _md5(path)
        if digest:
            _registry[digest] = str(path)
    _save()


def reset() -> None:
    """Clear the registry (use when starting a new project)."""
    global _registry
    _registry = {}
    path = _registry_file()
    if path.exists():
        path.unlink()
