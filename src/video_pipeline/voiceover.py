"""Voice-over generation with the ElevenLabs text-to-speech API."""

from __future__ import annotations

import logging
from pathlib import Path

import requests

from video_pipeline.config import ConfigError, cfg, require_env, voiceovers_dir
from video_pipeline.util import paragraph_suffix, request_with_retry

log = logging.getLogger(__name__)


def check_voiceover_config() -> None:
    """Raise ConfigError when the ElevenLabs key or voice ID is missing."""
    require_env("ELEVENLABS_API_KEY", "ElevenLabs text-to-speech")
    if not cfg("voiceover.voice_id"):
        raise ConfigError(
            "Missing voiceover.voice_id in the settings file. Pick a voice in the "
            "ElevenLabs voice library and copy its ID into config/settings.yaml."
        )


def generate_tts(text: str, filename: Path) -> bool:
    """Convert text to speech with ElevenLabs and save it to ``filename``. Returns success."""
    voice_id = cfg("voiceover.voice_id")
    url = (f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
           f"?output_format={cfg('voiceover.output_format')}")
    headers = {
        "xi-api-key": require_env("ELEVENLABS_API_KEY", "ElevenLabs text-to-speech"),
        "Content-Type": "application/json",
    }
    data = {
        "text": text,
        "model_id": cfg("voiceover.model_id"),
        "voice_settings": {
            "stability": cfg("voiceover.stability"),
            "similarity_boost": cfg("voiceover.similarity_boost"),
            "style": cfg("voiceover.style"),
            "use_speaker_boost": cfg("voiceover.use_speaker_boost"),
            "speed": cfg("voiceover.speed"),
        },
    }
    try:
        r = request_with_retry("POST", url, json=data, headers=headers, timeout=60)
        filename.write_bytes(r.content)
        return True
    except requests.RequestException as exc:
        log.error("ElevenLabs request failed for %s: %s", filename.name, exc)
        return False


def generate_voiceovers(sections: list[dict], first_section: int = 1,
                        last_section: int | None = None) -> tuple[int, int]:
    """Generate one voice-over file per paragraph: 01a.mp3, 01b.mp3, ... 02a.mp3, ...

    Section numbers keep their position in the full script, so a partial run
    (``first_section``/``last_section``, 1-based, inclusive) names files correctly.
    Returns ``(succeeded, failed)``.
    """
    out_dir = voiceovers_dir()
    out_dir.mkdir(parents=True, exist_ok=True)

    last = last_section if last_section is not None else len(sections)
    selected = [(i, s) for i, s in enumerate(sections, start=1) if first_section <= i <= last]
    total_paragraphs = sum(len(s["paragraphs"]) for _, s in selected)
    done = failed = 0

    for s_idx, section in selected:
        log.info("Section %d: \"%s\" - %d paragraphs", s_idx, section["title"],
                 len(section["paragraphs"]))
        for p_idx, paragraph in enumerate(section["paragraphs"]):
            filename = out_dir / f"{s_idx:02d}{paragraph_suffix(p_idx)}.mp3"
            ok = generate_tts(paragraph, filename)
            done += 1
            if not ok:
                failed += 1
            log.info("[%s] %s (%d/%d)", "ok" if ok else "FAILED", filename.name,
                     done, total_paragraphs)

    if failed:
        log.warning("%d of %d voice-over files could not be generated.", failed, done)
    return done - failed, failed
