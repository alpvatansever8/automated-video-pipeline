"""Wikipedia / Wikimedia Commons image collection.

For a search query or sentence, find the most fitting Wikipedia article and download the
article's main image (or other images from it) together with author and license data.
Images under non-commercial (NC) or no-derivatives (ND) licenses are skipped.
"""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path

import requests

from video_pipeline.ai import ask_ai
from video_pipeline.config import cfg
from video_pipeline.util import request_with_retry

log = logging.getLogger(__name__)

WIKIMEDIA_API = "https://commons.wikimedia.org/w/api.php"
WIKIPEDIA_API = "https://en.wikipedia.org/w/api.php"
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}

SKIP_WORDS = [
    "icon", "logo", "symbol", "stub", "question", "edit",
    "red_x", "check", "map", "location", "crest",
    "coat_of_arms", "signature", "blank", "placeholder",
]


def _headers() -> dict:
    """Request headers. Wikimedia asks API clients to identify themselves."""
    agent = "AutomatedVideoPipeline/1.0"
    contact = cfg("footage.wikimedia_contact", "")
    if contact:
        agent += f" ({contact})"
    return {"User-Agent": agent, "Accept": "application/json"}


def _get_json(url: str, params: dict, pause_s: float, timeout: int = 15) -> dict:
    """GET a MediaWiki API endpoint politely (pause first) and return the JSON body."""
    time.sleep(pause_s)
    return request_with_retry("GET", url, params=params, headers=_headers(), timeout=timeout).json()


# ---------------------------------------------------------------------------
# Sentence -> Wikipedia article
# ---------------------------------------------------------------------------

def _sentence_to_wikipedia_article(sentence: str, paragraph: str, section_title: str) -> str:
    """Ask the AI which Wikipedia article's main photo best illustrates a sentence."""
    prompt = (
        f"Documentary image search. ONE Wikipedia article title only.\n"
        f"Context: \"{section_title}\"\n"
        f"Sentence: \"{sentence}\"\n\n"
        f"What would a viewer SEE on screen while this sentence is narrated?\n"
        f"Choose the Wikipedia article whose MAIN PHOTO best illustrates this sentence.\n\n"
        f"RULES:\n"
        f"- Return EXACTLY ONE Wikipedia article title, nothing else\n"
        f"- Use the full official article name\n"
        f"- If the sentence names a person, place, species, event or object, use that article\n"
        f"- For generic sentences, choose the article closest to the section topic\n\n"
        f"Output ONLY the Wikipedia article title:"
    )

    response = ask_ai(prompt).strip()
    if response:
        lines = [ln.strip().strip("-*'\"") for ln in response.splitlines() if ln.strip()]
        if lines and 1 <= len(lines[0].split()) <= 10:
            return lines[0]

    return section_title  # no AI available: fall back to the section topic


# ---------------------------------------------------------------------------
# Wikipedia API
# ---------------------------------------------------------------------------

def _resolve_article(entity: str) -> str | None:
    """Return the title of the best-matching Wikipedia article for a search phrase."""
    try:
        data = _get_json(WIKIPEDIA_API, {
            "action": "query", "list": "search", "srsearch": entity,
            "srlimit": 5, "format": "json",
        }, pause_s=0.5)
        results = data.get("query", {}).get("search", [])
        if results:
            return results[0]["title"]
    except (requests.RequestException, ValueError) as exc:
        log.warning("Wikipedia search failed for '%s': %s", entity, exc)
    return None


def _is_allowed_license(license_name: str) -> bool:
    """Reject non-commercial (NC) and no-derivatives (ND) licenses."""
    ll = license_name.lower()
    return not (("nc" in ll and "cc" in ll) or "nd" in ll)


def _get_main_image(article_title: str) -> dict | None:
    """Return the article's main (infobox) image with license data, via the pageimages API."""
    try:
        data = _get_json(WIKIPEDIA_API, {
            "action": "query", "titles": article_title, "prop": "pageimages",
            "piprop": "thumbnail|name", "pithumbsize": 1200, "format": "json",
        }, pause_s=1.5)
        for page in data.get("query", {}).get("pages", {}).values():
            if page.get("pageid") == -1 or "missing" in page:
                return None
            thumb = page.get("thumbnail", {})
            img_name = page.get("pageimage", "")
            if not thumb.get("source"):
                return None
            url = re.sub(r"/\d+px-", "/1200px-", thumb["source"])
            info = {
                "license": "CC BY-SA 4.0",
                "license_url": "https://creativecommons.org/licenses/by-sa/4.0/",
                "author": "Wikipedia contributors",
                "page_url": f"https://en.wikipedia.org/wiki/{article_title.replace(' ', '_')}",
            }
            if img_name:
                try:
                    meta_data = _get_json(WIKIMEDIA_API, {
                        "action": "query", "titles": f"File:{img_name}",
                        "prop": "imageinfo", "iiprop": "extmetadata", "format": "json",
                    }, pause_s=1.0)
                    for p in meta_data.get("query", {}).get("pages", {}).values():
                        info_list = p.get("imageinfo", [])
                        if not info_list:
                            continue
                        meta = info_list[0].get("extmetadata", {})
                        license_name = meta.get("LicenseShortName", {}).get("value", "CC BY-SA 4.0")
                        if not _is_allowed_license(license_name):
                            return None
                        artist = re.sub(r"<[^>]+>", "",
                                        meta.get("Artist", {}).get("value", "Wikipedia contributors")).strip()
                        info = {
                            "license": license_name,
                            "license_url": meta.get("LicenseUrl", {}).get(
                                "value", "https://creativecommons.org/licenses/by-sa/4.0/"),
                            "author": artist or "Wikipedia contributors",
                            "page_url": f"https://commons.wikimedia.org/wiki/File:{img_name.replace(' ', '_')}",
                        }
                except (requests.RequestException, ValueError) as exc:
                    log.debug("Could not read image metadata for %s: %s", img_name, exc)
            return {
                "title": f"File:{img_name}" if img_name else article_title,
                "url": url,
                "license": info["license"],
                "license_url": info["license_url"],
                "author": info["author"],
                "page_url": info["page_url"],
                "article": article_title,
            }
    except (requests.RequestException, ValueError) as exc:
        log.warning("pageimages lookup failed for '%s': %s", article_title, exc)
    return None


def _get_article_images(article_title: str, max_images: int = 10) -> list[dict]:
    """Return images of an article (with URL and license data), skipping icons and maps."""
    try:
        data = _get_json(WIKIPEDIA_API, {
            "action": "query", "titles": article_title, "prop": "images",
            "imlimit": 20, "format": "json",
        }, pause_s=0.8)
        pages = data.get("query", {}).get("pages", {})
        if not pages:
            return []

        file_titles = []
        for img in list(pages.values())[0].get("images", []):
            t = img.get("title", "")
            ext = Path(t.split("?")[0]).suffix.lower()
            if ext in IMAGE_EXTS and not any(s in t.lower() for s in SKIP_WORDS):
                file_titles.append(t)
        if not file_titles:
            return []

        info_data = _get_json(WIKIMEDIA_API, {
            "action": "query", "titles": "|".join(file_titles[:max_images]),
            "prop": "imageinfo", "iiprop": "url|extmetadata|size", "format": "json",
        }, pause_s=0.8, timeout=20)

        results = []
        for p in info_data.get("query", {}).get("pages", {}).values():
            info_list = p.get("imageinfo", [])
            if not info_list:
                continue
            info = info_list[0]
            url = info.get("url", "")
            if Path(url.split("?")[0]).suffix.lower() not in IMAGE_EXTS:
                continue
            if (info.get("width") or 0) < 300 or (info.get("height") or 0) < 200:
                continue

            meta = info.get("extmetadata", {})
            license_name = meta.get("LicenseShortName", {}).get("value", "")
            if not _is_allowed_license(license_name):
                continue
            author = re.sub(r"<[^>]+>", "", meta.get("Artist", {}).get("value", "")).strip()
            results.append({
                "title": p.get("title", ""),
                "url": url,
                "license": license_name or "Unknown",
                "license_url": meta.get("LicenseUrl", {}).get("value", "") or "https://commons.wikimedia.org/",
                "author": author or "Unknown",
                "article": article_title,
                "page_url": f"https://commons.wikimedia.org/wiki/{p.get('title', '').replace(' ', '_')}",
            })
        return results
    except (requests.RequestException, ValueError) as exc:
        log.warning("Could not list images of '%s': %s", article_title, exc)
        return []


def _split_sentences(text: str) -> list[str]:
    raw = re.split(r"(?<=[.!?])\s+(?=[A-Z])", text.strip())
    return [s.strip() for s in raw if len(s.strip()) > 15]


# ---------------------------------------------------------------------------
# Single download (used by the footage collector)
# ---------------------------------------------------------------------------

def download_wikimedia_image(query: str, out: Path, label: str) -> dict | None:
    """Download one image for a search phrase and return its attribution, or None."""
    article = _resolve_article(query)
    if not article:
        return None
    for item in _get_article_images(article, max_images=5):
        try:
            r = request_with_retry("GET", item["url"], stream=True, headers=_headers(),
                                   timeout=cfg("http.download_timeout_s", 60))
            data = r.content
            if len(data) < 5000:
                continue
            out.write_bytes(data)
            log.info("Wikipedia image %s: %s - %s", label, item["article"], item["title"][:40])
            return {
                "source": "Wikimedia Commons (via Wikipedia)",
                "creator": item["author"], "url": item["page_url"],
                "license": item["license"], "license_url": item["license_url"],
            }
        except (requests.RequestException, OSError):
            if out.exists():
                out.unlink()
    return None


# ---------------------------------------------------------------------------
# Bulk collection: one image per sentence
# ---------------------------------------------------------------------------

def collect_wikimedia_for_section(section_title: str, section_dir: Path,
                                  section_paragraphs: list[str] | None = None) -> list[dict]:
    """Collect one image per sentence of a section into ``<section_dir>/wikimedia/``."""
    wm_dir = Path(section_dir) / "wikimedia"
    wm_dir.mkdir(parents=True, exist_ok=True)

    paragraphs = section_paragraphs or [section_title]
    collected: list[dict] = []
    seen_urls: set[str] = set()
    img_idx = 1

    all_sentences: list[tuple[str, str]] = []
    for p in paragraphs:
        sentences = _split_sentences(p)
        all_sentences.extend((p, s) for s in (sentences or [p[:200]]))

    log.info("Wikipedia: \"%s\" - %d sentences, one image per sentence",
             section_title, len(all_sentences))

    for para, sentence in all_sentences:
        article_name = _sentence_to_wikipedia_article(sentence, para, section_title)
        log.info("\"%s\" -> %s", sentence[:60], article_name)

        resolved = _resolve_article(article_name)
        if not resolved:
            log.info("No article found for '%s'", article_name)
            continue
        article_name = resolved

        main_img = _get_main_image(article_name)
        if main_img and main_img["url"] not in seen_urls:
            images = [main_img]
        else:
            images = _get_article_images(article_name, max_images=10)
        if not images:
            log.info("No images in article '%s'", article_name)
            continue

        downloaded = False
        for item in images:
            if item["url"] in seen_urls:
                continue
            ext = Path(item["url"].split("?")[0]).suffix.lower() or ".jpg"
            out_path = wm_dir / f"wm_{img_idx:02d}{ext}"
            try:
                r = request_with_retry("GET", item["url"], stream=True, headers=_headers(),
                                       timeout=cfg("http.download_timeout_s", 60))
                content = r.content
                if len(content) < 5000:
                    continue
                out_path.write_bytes(content)
            except (requests.RequestException, OSError):
                if out_path.exists():
                    out_path.unlink()
                continue

            seen_urls.add(item["url"])
            log.info("Saved image %02d: %s", img_idx, item["title"][:55])
            collected.append({
                "file": out_path.name,
                "title": item["title"],
                "source": "Wikimedia Commons (via Wikipedia)",
                "creator": item["author"],
                "page_url": item["page_url"],
                "license": item["license"],
                "license_url": item["license_url"],
                "sentence": sentence[:100],
                "article": article_name,
            })
            img_idx += 1
            downloaded = True
            break

        if not downloaded:
            log.info("Could not download an image for: %s", sentence[:60])

    (wm_dir / "attribution_wikimedia.json").write_text(
        json.dumps(collected, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [
        f"Wikimedia Commons - {section_title}",
        f"Total: {len(collected)} images / {len(all_sentences)} sentences",
        "=" * 60, "",
    ]
    for i, item in enumerate(collected, 1):
        lines += [
            f"[{i:02d}] {item['file']}",
            f"     Article : {item.get('article', '')}",
            f"     Sentence: {item.get('sentence', '')[:80]}",
            f"     Title   : {item['title']}",
            f"     Creator : {item['creator']}",
            f"     License : {item['license']}",
            f"     Source  : {item['page_url']}", "",
        ]
    (wm_dir / "attribution_wikimedia.txt").write_text("\n".join(lines), encoding="utf-8")

    log.info("Wikipedia done: %d/%d images in %s", len(collected), len(all_sentences), wm_dir)
    return collected


def collect_wikimedia_for_all_sections(sections: list[dict], assets_base: Path,
                                       first_section: int = 1,
                                       last_section: int | None = None) -> dict[str, list[dict]]:
    """Run the per-sentence collection for a range of sections (1-based, inclusive)."""
    from video_pipeline.util import section_dir_name

    last = last_section if last_section is not None else len(sections)
    results: dict[str, list[dict]] = {}
    for s_idx, section in enumerate(sections, start=1):
        if not first_section <= s_idx <= last:
            continue
        s_dir = Path(assets_base) / section_dir_name(s_idx, section["title"])
        s_dir.mkdir(parents=True, exist_ok=True)
        results[section["title"]] = collect_wikimedia_for_section(
            section["title"], s_dir, section.get("paragraphs", []))

    total = sum(len(v) for v in results.values())
    log.info("Wikipedia collection finished: %d images in %d sections", total, len(results))
    return results
