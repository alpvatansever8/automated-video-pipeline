"""Pixabay video and photo search (https://pixabay.com/api/docs/)."""

from __future__ import annotations

import logging
from pathlib import Path

import requests

from video_pipeline.ai import is_relevant_ai, is_relevant_quick
from video_pipeline.config import cfg, get_env
from video_pipeline.dedup import is_duplicate_url, register
from video_pipeline.footage.common import confidence_check, download_file
from video_pipeline.util import request_with_retry

log = logging.getLogger(__name__)

LICENSE = "Pixabay License"
LICENSE_URL = "https://pixabay.com/service/license/"


def search_video(query: str, location_terms: list[str], paragraph: str,
                 out: Path, label: str) -> dict | None:
    """Search Pixabay for a video, download the first relevant result, return its attribution."""
    api_key = get_env("PIXABAY_API_KEY")
    if not api_key:
        return None
    try:
        r = request_with_retry(
            "GET", "https://pixabay.com/api/videos/",
            params={"key": api_key, "q": query, "per_page": cfg("footage.pixabay_per_page", 10)},
        )
        videos = r.json().get("hits", [])
    except (requests.RequestException, ValueError) as exc:
        log.warning("Pixabay video search failed: %s", exc)
        return None

    for video in videos:
        vid_url = f"https://pixabay.com/videos/id-{video.get('id', '')}/"
        if is_duplicate_url(vid_url):
            continue
        tags = video.get("tags", "")
        user = video.get("user", "?")

        confidence = confidence_check(tags, paragraph, location_terms)
        if confidence in ("SKIP", "LOW"):
            continue
        if confidence == "MEDIUM" and not is_relevant_ai(label, f"{query} {tags}", paragraph,
                                                         location_terms):
            log.info("AI rejected Pixabay clip: %s", tags[:45])
            continue

        vids = video.get("videos", {})
        dl_url = ""
        for quality in ("large", "medium", "small", "tiny"):
            dl_url = vids.get(quality, {}).get("url", "")
            if dl_url:
                break
        if not dl_url or is_duplicate_url(dl_url):
            continue
        if download_file(dl_url, out):
            log.info("Pixabay video %s: %s", label, tags[:40])
            register(vid_url, out)
            return {"source": "Pixabay", "creator": user, "url": vid_url,
                    "license": LICENSE, "license_url": LICENSE_URL}
    return None


def search_photo(query: str, location_terms: list[str], out: Path, label: str) -> dict | None:
    """Search Pixabay for a photo and download the first acceptable result."""
    api_key = get_env("PIXABAY_API_KEY")
    if not api_key:
        return None
    try:
        r = request_with_retry(
            "GET", "https://pixabay.com/api/",
            params={"key": api_key, "q": query, "per_page": 5,
                    "image_type": "photo", "safesearch": "true"},
        )
        photos = r.json().get("hits", [])
    except (requests.RequestException, ValueError) as exc:
        log.warning("Pixabay photo search failed: %s", exc)
        return None

    for photo in photos:
        tags = photo.get("tags", "")
        ok, _ = is_relevant_quick(f"{tags} {query}", "", location_terms)
        if not ok:
            continue
        dl_url = photo.get("largeImageURL") or photo.get("webformatURL")
        if not dl_url or is_duplicate_url(dl_url):
            continue
        photo_out = out.with_suffix(".jpg")
        if download_file(dl_url, photo_out):
            user = photo.get("user", "?")
            page_url = f"https://pixabay.com/photos/id-{photo.get('id', '')}/"
            log.info("Pixabay photo %s by %s", label, user)
            register(page_url, photo_out)
            return {"source": "Pixabay Photo", "creator": user, "url": page_url,
                    "license": LICENSE, "license_url": LICENSE_URL}
    return None
