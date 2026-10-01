"""AI helper: local Ollama or Gemini, used for search queries and relevance scoring."""

from __future__ import annotations

import logging

import requests

from video_pipeline.config import cfg, get_env

log = logging.getLogger(__name__)

_ollama_status: bool | None = None


def _ollama_ok() -> bool:
    """Return True when Ollama is enabled in the settings and reachable (checked once)."""
    global _ollama_status
    if not cfg("ai.use_ollama", True):
        return False
    if _ollama_status is not None:
        return _ollama_status
    try:
        r = requests.get(f"{cfg('ai.ollama_url')}/api/tags", timeout=3)
        _ollama_status = r.status_code == 200
    except requests.RequestException:
        _ollama_status = False
    return _ollama_status


def ai_available() -> bool:
    """Return True when at least one AI backend (Ollama or Gemini) can be used."""
    return _ollama_ok() or bool(get_env("GEMINI_API_KEY"))


def ai_status() -> str:
    """Human-readable name of the active AI backend."""
    if _ollama_ok():
        return f"Ollama ({cfg('ai.ollama_model')})"
    if get_env("GEMINI_API_KEY"):
        return f"Gemini ({cfg('ai.gemini_model')})"
    return "none"


def ask_ai(prompt: str) -> str:
    """Send a prompt to Ollama, falling back to Gemini. Returns an empty string on failure."""
    if _ollama_ok():
        try:
            r = requests.post(
                f"{cfg('ai.ollama_url')}/api/generate",
                json={"model": cfg("ai.ollama_model"), "prompt": prompt, "stream": False},
                timeout=cfg("ai.timeout_s", 60),
            )
            return r.json().get("response", "").strip()
        except (requests.RequestException, ValueError) as exc:
            log.warning("Ollama request failed: %s", exc)
    gemini_key = get_env("GEMINI_API_KEY")
    if gemini_key:
        try:
            from google import genai

            client = genai.Client(api_key=gemini_key)
            resp = client.models.generate_content(model=cfg("ai.gemini_model"), contents=prompt)
            return (resp.text or "").strip()
        except Exception as exc:  # the SDK raises many different error types
            log.warning("Gemini request failed: %s", exc)
    return ""


def extract_visual_queries(paragraph: str, section_title: str,
                           location_terms: list[str] | None = None) -> list[str]:
    """Build up to three stock-footage search queries for a paragraph.

    Priority:
    0. Concrete subjects named in the text (for example "sushi"), searched without location
       forcing so the footage shows the subject rather than a landscape.
    1. Landmarks of the section location, matched to the paragraph, with the location injected.
    """
    from video_pipeline.geo_filter import get_document_country, inject_location
    from video_pipeline.landmarks import extract_concrete_subjects, get_landmarks, queries_for_paragraph

    country = get_document_country()
    loc_terms = location_terms or [section_title]

    subject_queries: list[str] = []
    for subject in extract_concrete_subjects(paragraph):
        subject_queries.append(subject)
        subject_queries.append(f"{subject} close up")

    landmarks = get_landmarks(section_title, country)
    location_queries = queries_for_paragraph(paragraph, section_title, landmarks, loc_terms)
    location_queries = [inject_location(q, loc_terms) for q in location_queries]

    queries = subject_queries + location_queries

    while len(queries) < 3:
        base = loc_terms[0] if loc_terms else section_title
        queries.append(f"{base} {country} documentary footage".strip())

    return queries[:3]


INSTANT_REJECT = {
    "protest", "my body my choice", "abortion", "gun control",
    "political rally", "demonstration", "breaking news",
    "weather report", "flag waving", "election campaign",
}


def is_relevant_quick(video_title: str, video_description: str,
                      location_terms: list[str]) -> tuple[bool, str]:
    """Fast check without AI: reject unsuitable topics and clearly wrong geography."""
    from video_pipeline.geo_filter import hard_reject

    combined = (video_title + " " + video_description[:150]).lower()
    for term in INSTANT_REJECT:
        if term in combined:
            return False, f"unsuitable topic: '{term}'"

    reason = hard_reject(video_title, video_description, location_terms)
    if reason:
        return False, reason

    return True, "OK"


def is_relevant_ai(topic: str, video_title: str, paragraph: str,
                   location_terms: list[str]) -> bool:
    """Ask the AI whether a piece of footage fits the paragraph and location (True if no AI)."""
    from video_pipeline.geo_filter import get_document_country

    country = get_document_country()
    loc_str = " / ".join(location_terms[:2]) if location_terms else topic

    prompt = (
        f"B-roll relevance: Is this footage from {loc_str} ({country}) "
        f"and relevant to: \"{paragraph[:150]}\"?\n"
        f"Footage: \"{video_title}\"\n"
        f"YES or NO only."
    )
    answer = ask_ai(prompt).upper()
    return answer.startswith("YES") if answer else True


def score_visual_match(paragraph: str, video_title: str) -> int:
    """Ask the AI to rate (0-10) how well footage matches a paragraph. Returns 5 on failure."""
    prompt = (
        f"Rate B-roll match 0-10.\n"
        f"Para: \"{paragraph[:200]}\"\nFootage: \"{video_title}\"\n"
        f"Output only the number."
    )
    answer = ask_ai(prompt).strip()
    try:
        return min(10, max(0, int("".join(filter(str.isdigit, answer.split()[0])))))
    except (ValueError, IndexError):
        return 5
