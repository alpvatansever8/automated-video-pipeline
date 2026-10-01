"""Script parser: reads a .docx or .txt script and splits it into sections and paragraphs.

DOCX: a paragraph whose text is entirely bold is a section title; everything else is body text.
TXT:  lines of the form "# Title" are section titles.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from video_pipeline.config import output_dir

log = logging.getLogger(__name__)


def _parse_docx(file_path: str | Path) -> list[dict]:
    """Parse a Word document into sections."""
    from docx import Document

    doc = Document(str(file_path))

    sections: list[dict] = []
    current_title: str | None = None
    current_paragraphs: list[str] = []

    def flush() -> None:
        if current_title and current_paragraphs:
            sections.append({"title": current_title, "paragraphs": list(current_paragraphs)})

    for para in doc.paragraphs:
        stripped = para.text.strip()
        if not stripped:
            continue

        # A paragraph is a title only if ALL of its text is bold.
        runs = [r for r in para.runs if r.text.strip()]
        if runs and all(r.bold for r in runs):
            flush()
            current_title = stripped
            current_paragraphs = []
            continue

        if current_title is None:
            current_title = "Introduction"
        current_paragraphs.append(stripped)

    flush()
    return sections


def _parse_txt(file_path: str | Path) -> list[dict]:
    """Parse a text file with "# Title" headings into sections."""
    text = Path(file_path).read_text(encoding="utf-8")
    raw = re.split(r"^# (.+)$", text, flags=re.MULTILINE)
    if len(raw) < 3:
        raise ValueError("No sections found. Use '# Title' lines to mark section titles.")
    sections = []
    for i in range(1, len(raw), 2):
        title = raw[i].strip()
        paragraphs = [p.strip() for p in raw[i + 1].strip().split("\n\n") if p.strip()]
        if paragraphs:
            sections.append({"title": title, "paragraphs": paragraphs})
    return sections


def parse_script(file_path: str | Path, total_minutes: float, save_plan: bool = True) -> list[dict]:
    """Parse a script and distribute ``total_minutes`` over the sections by character count.

    Each returned section has: title, paragraphs, content, char_count, duration_minutes,
    duration_seconds, time_start_minutes, time_end_minutes and paragraph_queries.
    """
    ext = Path(file_path).suffix.lower()
    if ext == ".docx":
        sections = _parse_docx(file_path)
    elif ext == ".txt":
        sections = _parse_txt(file_path)
    else:
        raise ValueError(f"Unsupported script format: '{ext}' (use .docx or .txt)")

    if not sections:
        raise ValueError("No sections found in the script.")

    for s in sections:
        s["content"] = "\n\n".join(s["paragraphs"])
        s["char_count"] = len(s["content"])

    total_chars = sum(s["char_count"] for s in sections)
    cumulative = 0.0

    for s in sections:
        ratio = s["char_count"] / total_chars if total_chars > 0 else 0
        s["duration_minutes"] = round(ratio * total_minutes, 2)
        s["duration_seconds"] = round(ratio * total_minutes * 60, 1)
        s["time_start_minutes"] = round(cumulative, 2)
        s["time_end_minutes"] = round(cumulative + s["duration_minutes"], 2)
        cumulative += s["duration_minutes"]
        s["paragraph_queries"] = [None] * len(s["paragraphs"])

    log.info("%-35s %7s  %16s  %s", "SECTION", "LENGTH", "RANGE", "PARAGRAPHS")
    for s in sections:
        span = f"{s['time_start_minutes']:.1f}-{s['time_end_minutes']:.1f} min"
        log.info("%-35s %4.1f min  %16s  %d", s["title"], s["duration_minutes"], span,
                 len(s["paragraphs"]))

    if save_plan:
        save_plan_file(sections, file_path, total_minutes)
    return sections


def save_plan_file(sections: list[dict], source_file: str | Path, total_minutes: float) -> Path:
    """Write a human-readable plan next to the other outputs."""
    plan_path = output_dir() / f"{Path(source_file).stem}_plan.txt"
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        f"VIDEO PLAN - {Path(source_file).name}",
        f"Total length: {total_minutes} minutes",
        f"Sections: {len(sections)}",
        "=" * 60, "",
    ]
    for i, s in enumerate(sections, 1):
        lines.append(f"SECTION {i}: {s['title']}")
        lines.append(f"  Length    : {s['duration_minutes']} min")
        lines.append(f"  Range     : {s['time_start_minutes']:.1f} - {s['time_end_minutes']:.1f} min")
        lines.append(f"  Paragraphs: {len(s['paragraphs'])}")
        lines.append("")
        for j, p in enumerate(s["paragraphs"], 1):
            lines.append(f"  [{j}] {p[:120]}{'...' if len(p) > 120 else ''}")
        lines.append("")
    plan_path.write_text("\n".join(lines), encoding="utf-8")
    log.info("Plan saved to %s", plan_path)
    return plan_path
