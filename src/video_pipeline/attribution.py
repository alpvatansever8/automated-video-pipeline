"""Writes the attribution (credits and license) file for all downloaded content."""

from __future__ import annotations

import logging
from pathlib import Path

from video_pipeline.config import output_dir

log = logging.getLogger(__name__)


def attribution_path() -> Path:
    """Location of the attribution file."""
    return output_dir() / "attribution.txt"


def save_attributions(items: list[dict], mode: str = "w") -> Path:
    """Write attribution entries. ``mode="a"`` appends (used when resuming from a later section)."""
    path = attribution_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, mode, encoding="utf-8") as f:
        if mode == "w":
            f.write("USED CONTENT - ATTRIBUTION INFORMATION\n")
            f.write("=" * 60 + "\n\n")
        else:
            f.write("\n--- CONTINUED ---\n\n")

        for idx, item in enumerate(items, start=1):
            f.write(f"[{idx}]\n")
            f.write(f"Source      : {item['source']}\n")
            f.write(f"Creator     : {item['creator']}\n")
            f.write(f"URL         : {item['url']}\n")
            f.write(f"License     : {item['license']}\n")
            f.write(f"License URL : {item['license_url']}\n")
            f.write("\n")

    log.info("Saved %d attribution entries to %s", len(items), path)
    return path
