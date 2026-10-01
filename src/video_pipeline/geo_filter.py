"""Geographic filter.

Instead of trusting the AI alone, location words are forced into every search query and
downloaded content is checked for them. For a section called "Aksum" in a document about
Ethiopia, queries must contain "Aksum" or "Ethiopia".
"""

from __future__ import annotations

from typing import Optional

# Document-level country/region context (set once from the first section title).
_document_country: str = ""

_STOP_WORDS = {"the", "and", "of", "in", "at", "on", "a", "an",
               "lake", "blue", "white", "old", "new", "great"}

CONFLICTING_COUNTRIES = {
    "turkey", "turkish", "turkiye",
    "india", "indian", "china", "chinese",
    "japan", "japanese", "brazil", "brazilian",
    "mexico", "mexican", "france", "french",
    "germany", "german", "russia", "russian",
    "usa", "american", "united states", "uk ", "british",
    "kenya", "tanzania", "nigeria", "ghana", "egypt", "egyptian",
    "morocco", "moroccan", "south africa",
}


def set_document_country(country: str) -> None:
    """Store the main country/region of the document (for example 'Ethiopia')."""
    global _document_country
    _document_country = country.strip()


def get_document_country() -> str:
    """Return the stored country/region, or an empty string."""
    return _document_country


def extract_location_terms(section_title: str) -> list[str]:
    """Extract mandatory location search terms from a section title (most specific first).

    "Aksum" -> ["Aksum", "Ethiopia"]
    "Simien Mountains" -> ["Simien Mountains", "Simien", "Mountains", "Ethiopia"]
    """
    title_clean = section_title.strip()
    terms = [title_clean]

    # Multi-word titles also contribute each significant word (4+ letters).
    terms.extend(w for w in title_clean.split()
                 if len(w) >= 4 and w.lower() not in _STOP_WORDS)

    if _document_country and _document_country not in terms:
        terms.append(_document_country)

    seen: set[str] = set()
    unique: list[str] = []
    for t in terms:
        if t.lower() not in seen:
            seen.add(t.lower())
            unique.append(t)
    return unique


def inject_location(query: str, location_terms: list[str]) -> str:
    """Add the location to a query unless it is already there.

    "ancient ruins" + ["Aksum", "Ethiopia"] -> "Aksum ancient ruins Ethiopia"
    """
    query_lower = query.lower()
    primary = location_terms[0] if location_terms else ""
    country = _document_country

    has_primary = primary.lower() in query_lower
    has_country = country.lower() in query_lower if country else True

    if has_primary and has_country:
        return query

    parts = []
    if not has_primary and primary:
        parts.append(primary)
    parts.append(query)
    if not has_country and country:
        parts.append(country)
    return " ".join(parts)


def title_contains_location(title: str, location_terms: list[str],
                            require_all: bool = False) -> bool:
    """Check whether a title contains at least one (or all) of the location terms.

    Matching is loose: "Aksumite" matches "Aksum" and "Ethiopian" matches "Ethiopia".
    """
    title_lower = title.lower()

    def matches(term: str) -> bool:
        t = term.lower()
        if t in title_lower:
            return True
        return len(t) >= 4 and any(word.startswith(t[:4]) for word in title_lower.split())

    if require_all:
        return all(matches(t) for t in location_terms)
    return any(matches(t) for t in location_terms)


def hard_reject(video_title: str, video_description: str,
                location_terms: list[str]) -> Optional[str]:
    """Return a rejection reason when content is clearly from the wrong country, else None.

    Only the strictest check runs: another country is named explicitly and the document's
    country is not mentioned.
    """
    if not _document_country:
        return None

    country_lower = _document_country.lower()
    title_lower = (video_title + " " + video_description[:200]).lower()

    # The document's country appears (prefix match: "ethi" -> ethiopia/ethiopian): accept.
    if country_lower[:4] in title_lower:
        return None

    found = [c for c in CONFLICTING_COUNTRIES if c in title_lower]
    if found:
        return f"wrong geography: mentions '{found[0]}'"

    return None
