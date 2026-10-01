"""Small shared helpers: naming conventions and HTTP requests with retries."""

from __future__ import annotations

import logging
import re
import time
from urllib.parse import urlparse

import requests

from video_pipeline.config import cfg

log = logging.getLogger(__name__)

RETRY_STATUS = {429, 500, 502, 503, 504}


def section_dir_name(section_idx: int, title: str) -> str:
    """Folder name for a section, e.g. ``02_Blue_Lagoon``. Used by every module."""
    safe = re.sub(r'[/\\:*?"<>|]', "-", title).replace(" ", "_")[:50]
    return f"{section_idx:02d}_{safe}"


def paragraph_suffix(p_idx: int) -> str:
    """Zero-based paragraph index to letters: 0 -> a, 25 -> z, 26 -> aa, 27 -> ab."""
    if p_idx < 26:
        return chr(97 + p_idx)
    return chr(97 + (p_idx // 26) - 1) + chr(97 + (p_idx % 26))


def paragraph_index_from_suffix(suffix: str) -> int:
    """Inverse of :func:`paragraph_suffix`, returned as a 1-based paragraph number."""
    if len(suffix) == 1:
        zero_based = ord(suffix) - 97
    else:
        zero_based = 26 + (ord(suffix[0]) - 97) * 26 + (ord(suffix[1]) - 97)
    return zero_based + 1


def request_with_retry(method: str, url: str, **kwargs) -> requests.Response:
    """Send an HTTP request, retrying on network errors, HTTP 429 and 5xx with backoff.

    Raises ``requests.RequestException`` when all attempts fail or on non-retryable errors.
    """
    retries = int(cfg("http.max_retries", 3))
    backoff = float(cfg("http.backoff_s", 2.0))
    kwargs.setdefault("timeout", cfg("http.timeout_s", 15))
    host = urlparse(url).netloc
    last_error: Exception | None = None

    for attempt in range(retries + 1):
        delay = backoff * (2 ** attempt)
        try:
            response = requests.request(method, url, **kwargs)
        except (requests.ConnectionError, requests.Timeout) as exc:
            last_error = exc
        else:
            if response.status_code not in RETRY_STATUS:
                response.raise_for_status()
                return response
            last_error = requests.HTTPError(f"HTTP {response.status_code}", response=response)
            retry_after = response.headers.get("Retry-After", "")
            try:
                delay = max(delay, float(retry_after))
            except ValueError:
                pass
        if attempt == retries:
            break
        log.warning("Request to %s failed (%s); retrying in %.1fs (%d/%d)",
                    host, last_error, delay, attempt + 1, retries)
        time.sleep(delay)

    assert last_error is not None
    raise last_error
