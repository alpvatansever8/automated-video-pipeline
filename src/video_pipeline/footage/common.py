"""Helpers shared by the stock-footage sources."""

from __future__ import annotations

import logging
from pathlib import Path

import requests

from video_pipeline.ai import ai_available, is_relevant_quick, score_visual_match
from video_pipeline.config import cfg
from video_pipeline.dedup import is_duplicate_file, is_duplicate_url, register
from video_pipeline.util import request_with_retry

log = logging.getLogger(__name__)

USER_AGENT = "AutomatedVideoPipeline/1.0"


def download_file(url: str, out: Path) -> bool:
    """Download ``url`` to ``out``. Returns False for duplicates and on failure."""
    if is_duplicate_url(url):
        return False
    try:
        r = request_with_retry("GET", url, stream=True,
                               timeout=cfg("http.download_timeout_s", 120),
                               headers={"User-Agent": USER_AGENT})
        with open(out, "wb") as f:
            for chunk in r.iter_content(65536):
                f.write(chunk)
    except (requests.RequestException, OSError) as exc:
        log.warning("Download failed: %s", exc)
        if out.exists():
            out.unlink()
        return False
    if is_duplicate_file(out):
        out.unlink()
        return False
    register(url, out)
    return True


def confidence_check(content_label: str, paragraph: str, location_terms: list[str]) -> str:
    """Judge how well a stock item fits the paragraph, using only its own label.

    Returns:
      "HIGH"   - download it
      "MEDIUM" - ask the AI one more time
      "LOW"    - skip this item
      "SKIP"   - reject immediately (unsuitable content)

    Without any AI backend the item is accepted when it passes the quick checks.
    """
    ok, _reason = is_relevant_quick(content_label, "", location_terms)
    if not ok:
        return "SKIP"

    if not ai_available():
        return "HIGH"

    score = score_visual_match(paragraph, content_label)
    log.info("Relevance score %d/10: %s", score, content_label[:45])

    if score >= cfg("footage.confidence_high", 7):
        return "HIGH"
    if score >= cfg("footage.confidence_low", 5):
        return "MEDIUM"
    return "LOW"
