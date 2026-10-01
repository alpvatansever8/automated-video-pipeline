"""Location knowledge: asks the AI for iconic, filmable things at a location and builds queries."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from video_pipeline.ai import ask_ai
from video_pipeline.config import cache_dir

log = logging.getLogger(__name__)

_cache: dict = {}


def _cache_file() -> Path:
    return cache_dir() / "landmark_cache.json"


def _load() -> None:
    global _cache
    path = _cache_file()
    if path.exists():
        try:
            _cache = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            _cache = {}


def _save() -> None:
    path = _cache_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_cache, ensure_ascii=False, indent=2), encoding="utf-8")


def _strip_code_fence(text: str) -> str:
    """Remove a surrounding markdown code fence (```json ... ```) from an AI answer."""
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else ""
    if text.endswith("```"):
        text = text[:-3]
    return text.strip()


def get_landmarks(section_title: str, document_country: str = "") -> list[dict]:
    """Return up to 5 iconic, filmable things at a location, cached on disk.

    Example item: ``{"name": "Church of Saint George", "query": "Bet Giyorgis church Lalibela"}``
    """
    _load()
    key = f"{section_title}|{document_country}"
    if key in _cache:
        return _cache[key]

    country_ctx = f" ({document_country})" if document_country else ""
    prompt = (
        f"Travel documentary landmark researcher.\n"
        f"Location: \"{section_title}\"{country_ctx}\n\n"
        f"List the 5 most visually iconic and filmable things at this location "
        f"that a documentary filmmaker would capture.\n\n"
        f"For each, provide:\n"
        f"- name: the landmark or scene name\n"
        f"- query: 4-7 word stock footage search query INCLUDING the location name\n\n"
        f"Output ONLY a JSON array, no markdown:\n"
        f'[{{"name":"...", "query":"..."}}, ...]'
    )

    response = ask_ai(prompt)
    landmarks: list[dict] = []

    if response:
        try:
            data = json.loads(_strip_code_fence(response))
            if isinstance(data, list):
                landmarks = [
                    {"name": str(d.get("name", "")), "query": str(d.get("query", ""))}
                    for d in data if isinstance(d, dict) and d.get("name") and d.get("query")
                ][:5]
        except ValueError:
            log.warning("Could not parse landmark list for '%s'; using generic queries.", section_title)

    if not landmarks:
        country = document_country or ""
        landmarks = [
            {"name": section_title, "query": f"{section_title} {country} landmark aerial"},
            {"name": f"{section_title} architecture", "query": f"{section_title} {country} architecture"},
            {"name": f"{section_title} people", "query": f"{section_title} {country} people culture"},
        ]

    _cache[key] = landmarks
    _save()
    return landmarks


def extract_concrete_subjects(paragraph: str) -> list[str]:
    """Return concrete, filmable objects, foods, animals or activities named in a paragraph.

    Place names are excluded. Example: "...eating sushi..." -> ["sushi"]. Returns [] if none.
    """
    prompt = (
        f"Text: \"{paragraph[:400]}\"\n\n"
        f"List concrete, specific, filmable physical objects, foods, animals, "
        f"or activities explicitly mentioned in this text (NOT place names, "
        f"NOT abstract concepts, NOT emotions).\n"
        f"Output a comma-separated list of 1-3 word items, or NONE if nothing concrete is mentioned.\n"
        f"Example output: sushi, chopsticks\n"
        f"Output:"
    )
    response = ask_ai(prompt).strip()
    if not response or response.upper().startswith("NONE"):
        return []
    subjects = [s.strip().strip("-*\"'.") for s in response.split(",")]
    subjects = [s for s in subjects if s and 1 <= len(s.split()) <= 3]
    return subjects[:3]


def queries_for_paragraph(paragraph: str, section_title: str,
                          landmarks: list[dict], location_terms: list[str]) -> list[str]:
    """Match a paragraph to the location's landmarks and produce up to 3 specific queries."""
    from video_pipeline.geo_filter import inject_location

    if not landmarks:
        return [f"{section_title} {' '.join(location_terms[:1])}"]

    lm_text = "\n".join(f"{i + 1}. {lm['name']} - example query: {lm['query']}"
                        for i, lm in enumerate(landmarks))

    prompt = (
        f"Match paragraph to documentary footage queries.\n"
        f"Section: \"{section_title}\"\n"
        f"Paragraph: \"{paragraph[:400]}\"\n\n"
        f"Available landmarks at this location:\n{lm_text}\n\n"
        f"Pick the best matching landmark(s) for this paragraph "
        f"and write 3 specific stock footage search queries.\n"
        f"MUST include location name in every query.\n"
        f"Output only QUERY: lines:\n"
        f"QUERY: <query here>"
    )

    response = ask_ai(prompt)
    queries: list[str] = []

    if response:
        for line in response.splitlines():
            line = line.strip()
            if line.upper().startswith("QUERY:"):
                q = line[6:].strip().strip("-*\"'")
                if q and 2 <= len(q.split()) <= 8:
                    queries.append(inject_location(q, location_terms))

    if not queries:
        for lm in landmarks[:3]:
            queries.append(lm["query"])

    return queries[:3]
