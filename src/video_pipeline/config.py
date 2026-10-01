"""Configuration loading: secrets from environment variables (.env) and tunables from YAML."""

from __future__ import annotations

import copy
import logging
import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

log = logging.getLogger(__name__)


class ConfigError(RuntimeError):
    """Raised when required configuration (such as an API key) is missing or invalid."""


DEFAULTS: dict[str, Any] = {
    "total_minutes": 5,
    "paths": {"output_dir": "output"},
    "http": {
        "max_retries": 3,
        "backoff_s": 2.0,
        "timeout_s": 15,
        "download_timeout_s": 120,
    },
    "ai": {
        "use_ollama": True,
        "ollama_url": "http://localhost:11434",
        "ollama_model": "mistral",
        "gemini_model": "gemini-2.0-flash",
        "timeout_s": 60,
    },
    "footage": {
        # Order in which sources are tried for every search query.
        # Valid names: pexels_video, pixabay_video, wikimedia, pexels_photo, pixabay_photo
        "source_order": [
            "pexels_video",
            "pixabay_video",
            "wikimedia",
            "pexels_photo",
            "pixabay_photo",
        ],
        "clips_per_paragraph": 3,
        "clips_intro": 9,
        # AI relevance score (0-10): >= high downloads, < low is rejected.
        "confidence_high": 7,
        "confidence_low": 5,
        "pexels_per_page": 10,
        "pixabay_per_page": 10,
        # Optional contact string appended to the Wikimedia User-Agent (Wikimedia asks for one).
        "wikimedia_contact": "",
    },
    "voiceover": {
        "voice_id": "",
        "model_id": "eleven_turbo_v2",
        "output_format": "mp3_44100_128",
        "stability": 0.50,
        "similarity_boost": 0.75,
        "style": 0.0,
        "use_speaker_boost": True,
        "speed": 0.94,
    },
    "capcut": {
        # Leave empty to use the default CapCut projects folder of your OS.
        "projects_dir": "",
        # Optional title font. Leave empty to use CapCut's default font.
        "title_font_path": "",
        "title_font_name": "",
        "title_font_size": 20.0,
        "title_scale": 0.6,
        "title_duration_s": 5,
        "title_fade_s": 1,
        # (until_second, max_cut_length_second) bands: cuts get longer as the video goes on.
        "cadence_bands": [[60, 3], [120, 5], [600, 8], [900, 10]],
        "cadence_default_s": 15,
        "canvas_width": 1920,
        "canvas_height": 1080,
        "fps": 30,
    },
}

_settings: dict[str, Any] = copy.deepcopy(DEFAULTS)


def _deep_merge(base: dict, extra: dict) -> dict:
    """Recursively merge ``extra`` into ``base`` and return ``base``."""
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value
    return base


def load_settings(path: str | Path | None = None, overrides: dict | None = None) -> dict:
    """Load .env and the YAML settings file (if any), merge over the defaults, and return them."""
    global _settings
    # Only the .env in the current folder is read; parent folders are never searched.
    load_dotenv(Path.cwd() / ".env")
    merged = copy.deepcopy(DEFAULTS)

    candidate = Path(path) if path else Path("config/settings.yaml")
    if candidate.is_file():
        data = yaml.safe_load(candidate.read_text(encoding="utf-8")) or {}
        if not isinstance(data, dict):
            raise ConfigError(f"Settings file {candidate} must contain a YAML mapping.")
        _deep_merge(merged, data)
        log.info("Loaded settings from %s", candidate)
    elif path:
        raise ConfigError(f"Settings file not found: {candidate}")
    else:
        log.info("No config/settings.yaml found; using built-in defaults.")

    if overrides:
        _deep_merge(merged, overrides)
    _settings = merged
    return merged


def cfg(dotted: str, default: Any = None) -> Any:
    """Read a setting by dotted path, e.g. ``cfg("voiceover.speed")``."""
    node: Any = _settings
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node


def get_env(name: str, default: str = "") -> str:
    """Return an environment variable, stripped, or ``default`` when it is unset."""
    return os.getenv(name, default).strip()


def require_env(name: str, purpose: str) -> str:
    """Return an environment variable or raise a readable ConfigError when it is missing."""
    value = get_env(name)
    if not value:
        raise ConfigError(
            f"Missing environment variable {name} ({purpose}). "
            f"Copy .env.example to .env and fill in your own key."
        )
    return value


def output_dir() -> Path:
    """Root folder for everything the pipeline generates."""
    return Path(cfg("paths.output_dir", "output"))


def assets_dir() -> Path:
    """Folder holding downloaded footage, one subfolder per section."""
    return output_dir() / "project_assets"


def voiceovers_dir() -> Path:
    """Folder holding generated voice-over files."""
    return output_dir() / "voiceovers"


def cache_dir() -> Path:
    """Folder holding caches and registries."""
    return output_dir() / "cache"
