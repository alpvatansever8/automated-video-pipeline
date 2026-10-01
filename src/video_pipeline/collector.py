"""Footage collector: finds and downloads footage for every paragraph of a script.

For each search query the configured sources are tried in order
(``footage.source_order``). Videos and photos are saved per section as
``s<section>p<paragraph>_<n>_<source>.<ext>`` and attribution data is collected.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

from video_pipeline.ai import ai_status, extract_visual_queries
from video_pipeline.config import ConfigError, assets_dir, cfg, get_env
from video_pipeline.footage import pexels, pixabay
from video_pipeline.footage.wikimedia import download_wikimedia_image
from video_pipeline.geo_filter import (extract_location_terms, get_document_country,
                                       set_document_country)
from video_pipeline.util import section_dir_name

log = logging.getLogger(__name__)

# Source name -> (environment variable it needs or None, short description)
SOURCES: dict[str, tuple[str | None, str]] = {
    "pexels_video": ("PEXELS_API_KEY", "Pexels video search"),
    "pixabay_video": ("PIXABAY_API_KEY", "Pixabay video search"),
    "wikimedia": (None, "Wikimedia Commons images"),
    "pexels_photo": ("PEXELS_API_KEY", "Pexels photo search"),
    "pixabay_photo": ("PIXABAY_API_KEY", "Pixabay photo search"),
}


@dataclass
class ParagraphResult:
    """How much footage was found for one paragraph."""

    section_idx: int
    section_title: str
    paragraph_idx: int
    found: int
    target: int


def source_order() -> list[str]:
    """Return the configured source order after validating the names."""
    order = list(cfg("footage.source_order", []))
    unknown = [s for s in order if s not in SOURCES]
    if unknown or not order:
        raise ConfigError(
            f"Invalid footage.source_order {order}. Valid sources: {', '.join(SOURCES)}."
        )
    return order


def missing_env_for_sources(order: list[str]) -> list[tuple[str, str]]:
    """Return (env variable, source) pairs for API keys required by ``order`` but not set."""
    missing = []
    for source in order:
        env_name = SOURCES[source][0]
        if env_name and not get_env(env_name):
            missing.append((env_name, source))
    return missing


def check_footage_config() -> None:
    """Raise ConfigError when a configured source needs an API key that is missing."""
    missing = missing_env_for_sources(source_order())
    if missing:
        details = "; ".join(f"{env} (needed by '{src}')" for env, src in missing)
        raise ConfigError(
            f"Missing environment variable(s): {details}. Copy .env.example to .env and fill in "
            f"your own keys, or remove those sources from footage.source_order in the settings."
        )


def _try_sources(query: str, location_terms: list[str], paragraph: str,
                 section_dir: Path, label: str) -> dict | None:
    """Try every configured source for one query; return the first attribution found."""
    for source in source_order():
        attr = None
        if source == "pexels_video":
            attr = pexels.search_video(query, location_terms, paragraph,
                                       section_dir / f"{label}_pexels.mp4", label)
        elif source == "pixabay_video":
            attr = pixabay.search_video(query, location_terms, paragraph,
                                        section_dir / f"{label}_pixabay.mp4", label)
        elif source == "wikimedia":
            attr = download_wikimedia_image(query, section_dir / f"{label}_wikimedia.jpg", label)
        elif source == "pexels_photo":
            attr = pexels.search_photo(query, location_terms,
                                       section_dir / f"{label}_pexels.jpg", label)
        elif source == "pixabay_photo":
            attr = pixabay.search_photo(query, location_terms,
                                        section_dir / f"{label}_pixabay.jpg", label)
        if attr:
            return attr
    return None


def download_for_paragraph(section_title: str, paragraph: str, para_idx: int, section_idx: int,
                           section_dir: Path, location_terms: list[str],
                           is_intro: bool = False) -> tuple[list[dict], int, int]:
    """Collect footage for one paragraph.

    Returns ``(attributions, found, target)``.
    """
    attributions: list[dict] = []
    queries = extract_visual_queries(paragraph, section_title, location_terms)
    log.info("P%d queries: %s", para_idx, queries)

    target = int(cfg("footage.clips_intro", 9) if is_intro else cfg("footage.clips_per_paragraph", 3))
    found = 0
    next_idx = 1

    def label() -> str:
        return f"s{section_idx:02d}p{para_idx:02d}_{next_idx:02d}"

    for query in queries:
        if found >= target:
            break
        attr = _try_sources(query, location_terms, paragraph, section_dir, label())
        if attr:
            attributions.append(attr)
            found += 1
            next_idx += 1

    # The intro gets extra effort when it is still short of its target.
    if is_intro and found < target:
        country = get_document_country()
        for query in extract_visual_queries(
                f"cinematic aerial {section_title} {country}", section_title, location_terms):
            if found >= target:
                break
            attr = _try_sources(query, location_terms, paragraph, section_dir, label())
            if attr:
                attributions.append(attr)
                found += 1
                next_idx += 1

    log.info("P%d: found %d of %d items", para_idx, found, target)
    return attributions, found, target


def process_sections(sections: list[dict], first_section: int = 1,
                     last_section: int | None = None) -> tuple[list[dict], list[ParagraphResult]]:
    """Collect footage for a range of sections (1-based, inclusive).

    Returns ``(attributions, per-paragraph results)``. Section numbers keep their position in
    the full script so a partial run names folders and files correctly.
    """
    base = assets_dir()
    base.mkdir(parents=True, exist_ok=True)
    order = source_order()
    log.info("AI backend: %s", ai_status())
    log.info("Source order: %s", " > ".join(order))

    first_title = sections[0]["title"] if sections else ""
    if first_title and len(first_title.split()) <= 3:
        set_document_country(first_title)
        log.info("Main country/region: '%s'", first_title)

    last = last_section if last_section is not None else len(sections)
    all_attributions: list[dict] = []
    results: list[ParagraphResult] = []
    last_region_title = first_title

    for s_idx, section in enumerate(sections, start=1):
        title = section["title"]

        # A title containing a number (for example "Kyoto 2") reuses the previous region's
        # title for search queries. The folder name keeps the real title.
        if re.search(r"\d", title):
            effective_title = last_region_title
        else:
            effective_title = title
            last_region_title = title

        if not first_section <= s_idx <= last:
            continue

        s_dir = base / section_dir_name(s_idx, title)
        s_dir.mkdir(parents=True, exist_ok=True)
        loc_terms = extract_location_terms(effective_title)
        is_intro = s_idx == 1

        log.info("[%d/%d] \"%s\"%s", s_idx, len(sections), title, " (intro)" if is_intro else "")
        if effective_title != title:
            log.info("Title contains a number; using '%s' for queries", effective_title)
        log.info("Location terms: %s", loc_terms[:3])

        for p_idx, paragraph in enumerate(section["paragraphs"], start=1):
            log.info("P%d/%d: %s...", p_idx, len(section["paragraphs"]), paragraph[:70])
            attrs, found, target = download_for_paragraph(
                section_title=effective_title, paragraph=paragraph, para_idx=p_idx,
                section_idx=s_idx, section_dir=s_dir, location_terms=loc_terms,
                is_intro=is_intro,
            )
            all_attributions.extend(attrs)
            results.append(ParagraphResult(s_idx, title, p_idx, found, target))

    return all_attributions, results


def log_shortfalls(results: list[ParagraphResult]) -> int:
    """Warn about paragraphs with less footage than targeted. Returns how many are short."""
    short = [r for r in results if r.found < r.target]
    total_found = sum(r.found for r in results)
    total_target = sum(r.target for r in results)
    log.info("Footage collected: %d of %d targeted items.", total_found, total_target)
    if short:
        log.warning(
            "Not enough stock footage was found for %d of %d paragraphs. The video will be "
            "built from what was found (clips may repeat). Try more specific script wording, "
            "other sources in footage.source_order, or a shorter video.",
            len(short), len(results))
        for r in short:
            log.warning("  Section %d \"%s\", paragraph %d: %d of %d items",
                        r.section_idx, r.section_title, r.paragraph_idx, r.found, r.target)
    return len(short)
