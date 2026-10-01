"""Pexels video and photo search (https://www.pexels.com/api/)."""

from __future__ import annotations

import logging
import re
from pathlib import Path
from urllib.parse import urlparse

import requests

from video_pipeline.ai import is_relevant_ai, is_relevant_quick
from video_pipeline.config import cfg, get_env
from video_pipeline.dedup import is_duplicate_url, register
from video_pipeline.footage.common import confidence_check, download_file
from video_pipeline.util import request_with_retry

log = logging.getLogger(__name__)

LICENSE = "Pexels License"
LICENSE_URL = "https://www.pexels.com/license/"

# (min width, min fps) pairs, best first.
QUALITY_PRIORITY = [
    (3840, 60), (3840, 30), (3840, 25), (3840, 0),
    (2560, 60), (2560, 30), (2560, 0),
    (1920, 60), (1920, 30), (1920, 0),
    (1280, 60), (1280, 30), (1280, 0),
    (0, 0),
]


def _best_file(files: list[dict]) -> dict | None:
    """Pick the highest-quality file according to QUALITY_PRIORITY."""
    if not files:
        return None

    def score(f: dict) -> int:
        w, fps = f.get("width", 0), f.get("fps", 0)
        for i, (qw, qfps) in enumerate(QUALITY_PRIORITY):
            if w >= qw and (qfps == 0 or fps >= qfps):
                return -i
        return -999

    return max(files, key=score)


def _label_from_page_url(page_url: str) -> str:
    """Readable description from a Pexels page URL.

    The API gives no title for videos, but the URL slug describes the clip:
    ".../video/dynamic-time-lapse-of-shibuya-crossing-35488258/" -> "dynamic time lapse of shibuya crossing".
    """
    slug = urlparse(page_url).path.rstrip("/").rsplit("/", 1)[-1]
    slug = re.sub(r"-\d+$", "", slug)
    return slug.replace("-", " ").strip()


def search_video(query: str, location_terms: list[str], paragraph: str,
                 out: Path, label: str) -> dict | None:
    """Search Pexels for a video, download the first relevant result, return its attribution."""
    api_key = get_env("PEXELS_API_KEY")
    if not api_key:
        return None
    try:
        r = request_with_retry(
            "GET", "https://api.pexels.com/videos/search",
            headers={"Authorization": api_key},
            params={"query": query, "per_page": cfg("footage.pexels_per_page", 10), "size": "large"},
        )
        videos = r.json().get("videos", [])
    except (requests.RequestException, ValueError) as exc:
        log.warning("Pexels video search failed: %s", exc)
        return None

    for video in videos:
        vid_url = video.get("url", "")
        if is_duplicate_url(vid_url):
            continue
        creator = video.get("user", {}).get("name", "?")
        content_label = _label_from_page_url(vid_url) or query

        confidence = confidence_check(content_label, paragraph, location_terms)
        if confidence in ("SKIP", "LOW"):
            continue
        if confidence == "MEDIUM" and not is_relevant_ai(label, content_label, paragraph, location_terms):
            log.info("AI rejected Pexels clip: %s", content_label[:45])
            continue

        best = _best_file(video.get("video_files", []))
        if not best or is_duplicate_url(best["link"]):
            continue
        if download_file(best["link"], out):
            log.info("Pexels video %s: %dp %sfps by %s", label, best.get("width", 0),
                     best.get("fps", 0), creator)
            register(vid_url, out)
            return {"source": "Pexels", "creator": creator, "url": vid_url,
                    "license": LICENSE, "license_url": LICENSE_URL}
    return None


def search_photo(query: str, location_terms: list[str], out: Path, label: str) -> dict | None:
    """Search Pexels for a photo and download the first acceptable result."""
    api_key = get_env("PEXELS_API_KEY")
    if not api_key:
        return None
    try:
        r = request_with_retry(
            "GET", "https://api.pexels.com/v1/search",
            headers={"Authorization": api_key},
            params={"query": query, "per_page": 8},
        )
        photos = r.json().get("photos", [])
    except (requests.RequestException, ValueError) as exc:
        log.warning("Pexels photo search failed: %s", exc)
        return None

    for photo in photos:
        photo_url = photo.get("url", "")
        if is_duplicate_url(photo_url):
            continue
        alt = photo.get("alt", "") or query
        ok, _ = is_relevant_quick(alt, "", location_terms)
        if not ok:
            continue
        src = photo.get("src", {})
        dl_url = src.get("original") or src.get("large2x") or src.get("large")
        if not dl_url or is_duplicate_url(dl_url):
            continue
        photo_out = out.with_suffix(".jpg")
        if download_file(dl_url, photo_out):
            creator = photo.get("photographer", "?")
            log.info("Pexels photo %s by %s", label, creator)
            register(photo_url, photo_out)
            return {"source": "Pexels Photo", "creator": creator, "url": photo_url,
                    "license": LICENSE, "license_url": LICENSE_URL}
    return None
