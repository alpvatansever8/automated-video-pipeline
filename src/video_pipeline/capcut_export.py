"""CapCut project export.

Writes a CapCut desktop draft project (``draft_info.json``, reverse-engineered format
version 97.0.0) so the timeline opens in CapCut without rendering anything.

* Video track: downloaded clips from ``<output>/project_assets``. Photos are turned into
  short clips with ffmpeg first.
* Audio track: voice-overs from ``<output>/voiceovers``.
* Text track: a section title (with fade in/out) at the start of every section.

Timing: section lengths are recomputed from the real voice-over durations (falling back
to character counts) so they add up to the requested total. Each paragraph lasts as long
as its voice-over plus an equal share of the section's spare time. That span is filled with
several short cuts whose maximum length grows over the video (see ``capcut.cadence_bands``).
When a paragraph has too few clips, clips are reused and the gap is reported.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import re
import subprocess
import time
import uuid
from itertools import cycle
from pathlib import Path

from video_pipeline.config import assets_dir, cfg, voiceovers_dir
from video_pipeline.util import paragraph_index_from_suffix, section_dir_name

log = logging.getLogger(__name__)

US = 1_000_000  # microseconds per second
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
PHOTO_CLIP_SECONDS = 6.0


# ---------------------------------------------------------------------------
# Platform and small helpers
# ---------------------------------------------------------------------------

def platform_name() -> str:
    """Return the platform label CapCut stores in drafts: "windows" or "mac"."""
    return "windows" if platform.system() == "Windows" else "mac"


def capcut_projects_root() -> Path:
    """Folder where CapCut keeps its draft projects (setting ``capcut.projects_dir`` wins)."""
    configured = cfg("capcut.projects_dir", "")
    if configured:
        return Path(configured).expanduser()
    if platform.system() == "Windows":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        return base / "CapCut" / "User Data" / "Projects" / "com.lveditor.draft"
    return Path.home() / "Movies" / "CapCut" / "User Data" / "Projects" / "com.lveditor.draft"


def _uid() -> str:
    """Upper-case UUID, the id format CapCut uses."""
    return str(uuid.uuid4()).upper()


def _now_us() -> int:
    """Timestamp in microseconds (CapCut uses microseconds, not milliseconds)."""
    return int(time.time() * 1_000_000)


def _max_interval_us(cursor_us: int) -> int:
    """Maximum cut length at the current timeline position, in microseconds."""
    s = cursor_us / US
    for edge_s, max_s in cfg("capcut.cadence_bands"):
        if s < edge_s:
            return int(max_s * US)
    return int(cfg("capcut.cadence_default_s") * US)


def _media_duration_us(path: Path) -> int:
    """Media duration in microseconds via ffprobe (10 s if it cannot be read)."""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, timeout=15)
        return int(float(result.stdout.strip()) * US)
    except (OSError, ValueError, subprocess.SubprocessError):
        log.warning("Could not read duration of %s; assuming 10 s", path.name)
        return 10 * US


def _media_dimensions(path: Path) -> tuple[int, int]:
    """Video width and height via ffprobe (1920x1080 if it cannot be read)."""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height", "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=15)
        parts = result.stdout.strip().split(",")
        return int(parts[0]), int(parts[1])
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        return 1920, 1080


def _image_to_video(image_path: Path, duration_s: float = PHOTO_CLIP_SECONDS) -> Path | None:
    """Turn an image into a still 1920x1080 mp4 clip with ffmpeg."""
    out = image_path.with_suffix(".mp4")
    if out.exists() and out.stat().st_size > 5000:
        return out
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-loop", "1", "-i", str(image_path),
             "-t", str(round(duration_s, 3)),
             "-vf", "scale=1920:1080:force_original_aspect_ratio=decrease,"
                    "pad=1920:1080:(ow-iw)/2:(oh-ih)/2:color=black",
             "-c:v", "libx264", "-pix_fmt", "yuv420p", "-r", "30", str(out)],
            capture_output=True, timeout=90)
        if out.exists() and out.stat().st_size > 5000:
            return out
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("ffmpeg failed for %s: %s", image_path.name, exc)
    return None


def _collect_clips(section_dir: Path) -> list[Path]:
    """All usable clips of a section: mp4 files plus photos converted to clips."""
    if not section_dir.exists():
        return []
    for image in sorted(p for p in section_dir.iterdir()
                        if p.is_file() and p.suffix.lower() in IMAGE_EXTS):
        _image_to_video(image)
    return sorted(section_dir.glob("*.mp4"))


def _single_gap_fill_clip(query_text: str, section_dir: Path, duration_s: float,
                          tag: str) -> Path | None:
    """If a paragraph has no clip at all, fetch a Wikimedia image for its own text."""
    from video_pipeline.footage.wikimedia import download_wikimedia_image

    gap_dir = section_dir / "gap_fill"
    gap_dir.mkdir(parents=True, exist_ok=True)

    img_path = gap_dir / f"missing_{tag}.jpg"
    if not img_path.exists():
        download_wikimedia_image(query_text[:80], img_path, f"missing_{tag}")
    if img_path.exists():
        return _image_to_video(img_path, duration_s=max(1.0, duration_s))
    return None


def _para_index_from_audio_name(stem: str) -> int:
    """Convert a voice-over file name (01a, 01b, ..., 01aa) to a 1-based paragraph index."""
    m = re.match(r"^\d+([a-z]+)$", stem)
    return paragraph_index_from_suffix(m.group(1)) if m else 1


def _paragraph_text_for(sections: list[dict], s_idx: int, para_idx: int) -> str:
    """Text of the given section/paragraph (used as the fallback image query)."""
    try:
        return sections[s_idx - 1]["paragraphs"][para_idx - 1]
    except (IndexError, KeyError):
        return sections[s_idx - 1]["title"] if 0 <= s_idx - 1 < len(sections) else ""


def _recompute_section_durations(sections: list[dict], total_minutes: float,
                                 voiceovers_base: Path) -> None:
    """Recompute section lengths from real voice-over durations (or character-count estimates).

    Overwrites duration_seconds, time_start_seconds and time_end_seconds in place so the
    sections add up to exactly ``total_minutes``.
    """
    target_total_us = int(total_minutes * 60 * US)

    weights = []
    for s_idx, section in enumerate(sections, start=1):
        audio_files = sorted(voiceovers_base.glob(f"{s_idx:02d}*.mp3")) if voiceovers_base.exists() else []
        real_audio_us = sum(_media_duration_us(af) for af in audio_files)
        if real_audio_us > 0:
            weights.append(real_audio_us)
        else:
            weights.append(max(int(section.get("duration_seconds", 0) * US), 1))

    total_w = sum(weights) or 1
    cumulative_us = 0
    for section, w in zip(sections, weights):
        dur_us = int(target_total_us * (w / total_w))
        section["duration_seconds"] = dur_us / US
        section["time_start_seconds"] = cumulative_us / US
        cumulative_us += dur_us
        section["time_end_seconds"] = cumulative_us / US

    # Put the rounding remainder on the last section so the total matches exactly.
    if sections:
        diff_us = target_total_us - cumulative_us
        sections[-1]["duration_seconds"] += diff_us / US
        sections[-1]["time_end_seconds"] += diff_us / US

    log.info("Section lengths recomputed from voice-over durations (target %s min):", total_minutes)
    for i, s in enumerate(sections, 1):
        log.info("  %2d. %-38s %.2f min [%.2f-%.2f min]", i, s["title"][:38],
                 s["duration_seconds"] / 60, s["time_start_seconds"] / 60,
                 s["time_end_seconds"] / 60)


# ---------------------------------------------------------------------------
# Draft JSON builders
# ---------------------------------------------------------------------------

def _append_video_segment(vf: Path, start_us: int, use_dur_us: int, canvas_id: str,
                          mat_videos: list, mat_speeds: list, mat_sound_channel_mappings: list,
                          mat_vocal_separations: list, mat_material_animations: list,
                          video_segments: list) -> None:
    real_dur_us = _media_duration_us(vf)
    w, h = _media_dimensions(vf)

    mat_id, speed_id, sound_map_id, vocal_sep_id, anim_id = (_uid() for _ in range(5))

    mat_speeds.append({"curve_speed": None, "id": speed_id, "mode": 0, "speed": 1.0, "type": "speed"})
    mat_sound_channel_mappings.append({
        "audio_channel_mapping": 0, "id": sound_map_id, "is_config_open": False, "type": ""})
    mat_vocal_separations.append({
        "choice": 0, "id": vocal_sep_id, "production_path": "", "time_range": None,
        "type": "vocal_separation"})
    mat_material_animations.append({"animations": [], "id": anim_id, "type": "sticker_animation"})

    mat_videos.append({
        "aigc_type": "none", "audio_fade": None, "cartoon_path": "", "category_id": "",
        "category_name": "local", "check_flag": 63487,
        "crop": {
            "lower_left_x": 0.0, "lower_left_y": 1.0,
            "lower_right_x": 1.0, "lower_right_y": 1.0,
            "upper_left_x": 0.0, "upper_left_y": 0.0,
            "upper_right_x": 1.0, "upper_right_y": 0.0,
        },
        "crop_ratio": "free", "crop_scale": 1.0, "duration": real_dur_us,
        "extra_type_option": 0, "formula_id": "", "freeze": None, "gameplay": None,
        "has_audio": True, "height": h, "id": mat_id,
        "intensifies_audio_path": "", "intensifies_path": "",
        "is_ai_generate_content": False, "is_copyright": False,
        "is_unified_beauty_mode": False, "local_id": "",
        "local_material_id": _uid().lower().replace("-", ""),
        "material_id": "", "material_name": vf.name, "material_url": "",
        "matting": {
            "flag": 0, "has_use_quick_brush": False, "has_use_quick_eraser": False,
            "interactiveTime": [], "path": "", "strokes": [],
        },
        "media_path": "", "object_locked": None, "origin_material_id": "",
        "path": str(vf.resolve()), "picture_from": "none",
        "picture_set_category_id": "", "picture_set_category_name": "",
        "request_id": "", "reverse_intensifies_path": "", "reverse_path": "",
        "smart_motion": None, "source": 0, "source_platform": 0,
        "stable": {"matrix_path": "", "stable_level": 0, "time_range": {"duration": 0, "start": 0}},
        "team_id": "", "type": "video",
        "video_algorithm": {
            "algorithms": [], "deflicker": None, "motion_blur_config": None,
            "noise_reduction": None, "path": "", "quality_enhance": None, "time_range": None,
        },
        "width": w,
    })

    video_segments.append({
        "cartoon": False,
        "clip": {
            "alpha": 1.0, "flip": {"horizontal": False, "vertical": False},
            "rotation": 0.0, "scale": {"x": 1.0, "y": 1.0}, "transform": {"x": 0.0, "y": 0.0},
        },
        "common_keyframes": [], "enable_adjust": True, "enable_color_curves": True,
        "enable_color_match_adjust": False, "enable_color_wheels": True,
        "enable_lut": True, "enable_smart_color_adjust": False,
        "extra_material_refs": [speed_id, canvas_id, anim_id, sound_map_id, vocal_sep_id],
        "group_id": "", "hdr_settings": {"intensity": 1.0, "mode": 1, "nits": 1000},
        "id": _uid(), "intensifies_audio": False, "is_placeholder": False,
        "is_tone_modify": False, "keyframe_refs": [], "last_nonzero_volume": 1.0,
        "material_id": mat_id, "render_index": 0,
        "responsive_layout": {
            "enable": False, "horizontal_pos_layout": 0, "size_layout": 0,
            "target_follow": "", "vertical_pos_layout": 0,
        },
        "reverse": False,
        "source_timerange": {"duration": use_dur_us, "start": 0},
        "speed": 1.0,
        "target_timerange": {"duration": use_dur_us, "start": start_us},
        "template_id": "", "template_scene": "default", "track_attribute": 0,
        "track_render_index": 0, "uniform_scale": {"on": True, "value": 1.0},
        "visible": True, "volume": 1.0,
    })


def _append_audio_segment(af: Path, start_us: int, dur_us: int, mat_audios: list,
                          mat_speeds: list, mat_material_animations: list,
                          audio_segments: list) -> None:
    mat_id, speed_id, anim_id = _uid(), _uid(), _uid()

    mat_speeds.append({"curve_speed": None, "id": speed_id, "mode": 0, "speed": 1.0, "type": "speed"})
    mat_material_animations.append({"animations": [], "id": anim_id, "type": "sticker_animation"})

    mat_audios.append({
        "app_id": 0, "category_id": "", "category_name": "", "check_flag": 1,
        "duration": dur_us, "effect_id": "", "formula_id": "", "id": mat_id,
        "intensifies_path": "", "local_material_id": _uid().lower().replace("-", ""),
        "music_id": "", "name": af.name, "path": str(af.resolve()), "query": "",
        "request_id": "", "resource_id": "", "search_id": "", "source_platform": 0,
        "team_id": "", "text": "", "tone_folder_name": "", "type": "extract_music",
        "wave_points": [],
    })

    audio_segments.append({
        "cartoon": False,
        "clip": {
            "alpha": 1.0, "flip": {"horizontal": False, "vertical": False},
            "rotation": 0.0, "scale": {"x": 1.0, "y": 1.0}, "transform": {"x": 0.0, "y": 0.0},
        },
        "common_keyframes": [], "enable_adjust": False, "enable_color_curves": True,
        "enable_color_match_adjust": False, "enable_color_wheels": True,
        "enable_lut": False, "enable_smart_color_adjust": False,
        "extra_material_refs": [speed_id, anim_id],
        "group_id": "", "hdr_settings": None, "id": _uid(),
        "intensifies_audio": False, "is_placeholder": False, "is_tone_modify": False,
        "keyframe_refs": [], "last_nonzero_volume": 1.0, "material_id": mat_id,
        "render_index": 11000,
        "responsive_layout": {
            "enable": False, "horizontal_pos_layout": 0, "size_layout": 0,
            "target_follow": "", "vertical_pos_layout": 0,
        },
        "reverse": False,
        "source_timerange": {"duration": dur_us, "start": 0},
        "speed": 1.0,
        "target_timerange": {"duration": dur_us, "start": start_us},
        "template_id": "", "template_scene": "default", "track_attribute": 0,
        "track_render_index": 0, "uniform_scale": {"on": True, "value": 1.0},
        "visible": True, "volume": 1.0,
    })


def _title_font() -> tuple[str, str]:
    """Return (font path, font name) for titles, or empty strings for CapCut's default font."""
    font_path = cfg("capcut.title_font_path", "")
    if not font_path:
        return "", ""
    if not Path(font_path).expanduser().exists():
        log.warning("capcut.title_font_path does not exist (%s); using CapCut's default font.",
                    font_path)
        return "", ""
    font_name = cfg("capcut.title_font_name", "") or Path(font_path).stem
    return str(Path(font_path).expanduser()), font_name


def _make_text_material(mat_id: str, title: str) -> dict:
    """Title text material (uses the configured font if any, else CapCut's default)."""
    font_path, font_name = _title_font()
    font_size = float(cfg("capcut.title_font_size"))

    content = json.dumps({
        "styles": [{
            "fill": {"alpha": 1, "content": {
                "render_type": "solid", "solid": {"alpha": 1, "color": [1.0, 1.0, 1.0]}}},
            "font": {"id": "", "path": font_path, "title": font_name},
            "range": [0, len(title)],
            "size": font_size,
            "useLetterColor": False,
        }],
        "text": title,
    }, ensure_ascii=False)

    return {
        "add_type": 0, "alignment": 1, "background_alpha": 1.0, "background_color": "",
        "background_height": 0.14, "background_horizontal_offset": 0.0,
        "background_round_radius": 0.0, "background_style": 0,
        "background_vertical_offset": 0.0, "background_width": 0.82,
        "base_content": "", "bold_width": 0.0, "border_alpha": 1.0, "border_color": "",
        "border_width": 0.0,
        "caption_template_info": {
            "category_id": "", "category_name": "", "effect_id": "",
            "resource_id": "", "resource_name": "",
        },
        "check_flag": 7, "combo_info": {"text_templates": []}, "content": content,
        "font_category_id": "", "font_category_name": "", "font_id": "",
        "font_name": font_name, "font_path": font_path, "font_resource_id": "",
        "font_size": font_size, "font_source_platform": 0, "font_team_id": "",
        "font_title": font_name, "font_url": "", "fonts": [],
        "force_apply_line_max_width": False, "global_alpha": 1.0, "group_id": "",
        "has_shadow": True, "id": mat_id, "initial_scale": 1.0, "inner_padding": -1.0,
        "is_rich_text": False, "italic_degree": 0, "ktv_color": "", "language": "",
        "layer_weight": 1, "letter_spacing": 0.0, "line_feed": 1, "line_max_width": 0.82,
        "line_spacing": 0.02, "multi_language_current": "none", "name": "",
        "original_size": [], "preset_id": "", "recognize_task_id": "",
        "recognize_type": 0, "relevance_segment": [], "shadow_alpha": 0.8,
        "shadow_angle": -45.0, "shadow_blur": 4.0, "shadow_color": "#000000",
        "shadow_distance": 6.0,
        "shadow_point": {"x": 1.1313708498984766, "y": -1.1313708498984766},
        "shape_clip_x": False, "shape_clip_y": False, "source_from": "", "style_name": "",
        "sub_type": "", "subtitle_keywords": None,
        "subtitle_template_original_fontsize": 0.0, "text_alpha": 1.0,
        "text_color": "#FFFFFF", "text_curve": None, "text_preset_resource_id": "",
        "text_size": font_size, "text_to_audio_ids": [], "tts_auto_update": False,
        "type": "text", "typesetting": 0, "underline": False, "underline_offset": 0.22,
        "underline_width": 0.05, "use_effect_default_color": True, "words": {},
    }


def _make_opacity_kf_group(seg_id: str, duration_us: int) -> dict:
    """Opacity keyframes: fade in at the start and fade out at the end."""
    fade_us = min(int(cfg("capcut.title_fade_s") * US), max(US // 10, duration_us // 4))

    def kf(t: int, v: float) -> dict:
        return {"curveType": "Line", "graphTime": t, "id": _uid(),
                "localTime": t, "time_offset": 0, "values": [v]}

    keyframes = [kf(0, 0.0), kf(fade_us, 1.0)]
    if duration_us - fade_us > fade_us:
        keyframes.append(kf(duration_us - fade_us, 1.0))
    keyframes.append(kf(duration_us, 0.0))

    return {"id": _uid(), "keyframe_list": keyframes,
            "material_id": seg_id, "property_type": "KFTypeOpacity"}


def _append_text_segment(title: str, start_us: int, duration_us: int, mat_texts: list,
                         text_segments: list, text_kf_groups: list) -> None:
    txt_mat_id, txt_seg_id = _uid(), _uid()
    scale = float(cfg("capcut.title_scale"))

    mat_texts.append(_make_text_material(txt_mat_id, title))
    kf_group = _make_opacity_kf_group(txt_seg_id, duration_us)
    text_kf_groups.append(kf_group)

    text_segments.append({
        "cartoon": False,
        "clip": {
            "alpha": 1.0, "flip": {"horizontal": False, "vertical": False},
            "rotation": 0.0, "scale": {"x": scale, "y": scale}, "transform": {"x": 0.0, "y": 0.0},
        },
        "common_keyframes": [kf_group["id"]],
        "enable_adjust": False, "enable_color_curves": True,
        "enable_color_match_adjust": False, "enable_color_wheels": True,
        "enable_lut": False, "enable_smart_color_adjust": False,
        "extra_material_refs": [], "group_id": "", "hdr_settings": None,
        "id": txt_seg_id, "intensifies_audio": False, "is_placeholder": False,
        "is_tone_modify": False, "keyframe_refs": [], "last_nonzero_volume": 1.0,
        "material_id": txt_mat_id, "render_index": 14000,
        "responsive_layout": {
            "enable": False, "horizontal_pos_layout": 0, "size_layout": 0,
            "target_follow": "", "vertical_pos_layout": 0,
        },
        "reverse": False,
        "source_timerange": {"duration": duration_us, "start": 0},
        "speed": 1.0,
        "target_timerange": {"duration": duration_us, "start": start_us},
        "template_id": "", "template_scene": "default", "track_attribute": 0,
        "track_render_index": 0, "uniform_scale": {"on": True, "value": 1.0},
        "visible": True, "volume": 1.0,
    })


def _platform_block() -> dict:
    """The device/platform block CapCut stores in every draft (random ids, no real device data)."""
    return {
        "app_id": 359289, "app_source": "cc", "app_version": "3.2.0",
        "device_id": uuid.uuid4().hex, "hard_disk_id": uuid.uuid4().hex,
        "mac_address": uuid.uuid4().hex[:12], "os": platform_name(),
        "os_version": "15.0",
    }


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def build_capcut_project(sections: list[dict], total_minutes: float,
                         project_name: str = "AI_Video") -> Path:
    """Create a CapCut draft project for the given sections and return its folder.

    Open (or restart) CapCut afterwards; the project appears at the top of the project list.
    """
    capcut_root = capcut_projects_root()
    if not capcut_root.exists():
        log.warning("CapCut projects folder did not exist and will be created: %s. If CapCut "
                    "keeps projects elsewhere, set capcut.projects_dir in the settings.", capcut_root)

    safe_name = project_name.replace("/", "_").replace("\\", "_")
    project_dir = capcut_root / safe_name
    project_dir.mkdir(parents=True, exist_ok=True)

    assets_base = assets_dir()
    voiceovers_base = voiceovers_dir()
    title_dur_s = float(cfg("capcut.title_duration_s"))

    _recompute_section_durations(sections, total_minutes, voiceovers_base)
    target_total_us = int(total_minutes * 60 * US)

    mat_videos: list = []
    mat_audios: list = []
    mat_texts: list = []
    mat_speeds: list = []
    mat_canvases: list = []
    mat_sound_channel_mappings: list = []
    mat_vocal_separations: list = []
    mat_material_animations: list = []
    video_segments: list = []
    audio_segments: list = []
    text_segments: list = []
    text_kf_groups: list = []

    canvas_id = _uid()
    mat_canvases.append({
        "album_image": "", "blur": 0.0, "color": "", "id": canvas_id, "image": "",
        "image_id": "", "image_name": "", "source_platform": 0, "team_id": "",
        "type": "canvas_color",
    })

    missing_report: list[tuple[int, str, int, str, str | None]] = []
    timeline_cursor = 0  # video and audio tracks always advance together

    for s_idx, section in enumerate(sections, start=1):
        section_dir = assets_base / section_dir_name(s_idx, section["title"])
        target_dur_us = int(round(section["duration_seconds"] * US))
        section_start_us = timeline_cursor

        title_dur_us = min(int(title_dur_s * US), target_dur_us) if target_dur_us > 0 else 0
        if title_dur_us > int(0.3 * US):
            _append_text_segment(section["title"], section_start_us, title_dur_us,
                                 mat_texts, text_segments, text_kf_groups)

        # Voice-overs of the section: one file per paragraph, never split.
        audio_files = sorted(voiceovers_base.glob(f"{s_idx:02d}*.mp3")) if voiceovers_base.exists() else []
        paragraphs = [{"path": af, "dur_us": _media_duration_us(af),
                       "para_idx": _para_index_from_audio_name(af.stem)} for af in audio_files]

        if not paragraphs:
            # No voice-over: keep the section and fill its whole length with footage.
            paragraphs = [{"path": None, "dur_us": 0, "para_idx": 1}]
            log.warning("No voice-over found for '%s'; the section will contain video only.",
                        section["title"])

        video_files = _collect_clips(section_dir)
        videos_by_para: dict[int, list[Path]] = {}
        for vf in video_files:
            m = re.match(r"s\d+p(\d+)_", vf.name)
            if m:
                videos_by_para.setdefault(int(m.group(1)), []).append(vf)
        section_pool = video_files  # fallback when a paragraph has no clips of its own

        sum_dur_us = sum(p["dur_us"] for p in paragraphs)
        n = len(paragraphs)
        gap_per_para_us = max(0, (target_dur_us - sum_dur_us) // n) if n else 0
        if sum_dur_us > target_dur_us:
            log.warning("'%s': voice-over total (%.1f s) exceeds the section length (%.1f s); "
                        "paragraphs will be placed back to back.",
                        section["title"][:35], sum_dur_us / US, target_dur_us / US)

        for i, p in enumerate(paragraphs):
            para_span_us = p["dur_us"] + gap_per_para_us
            if i == n - 1:  # rounding remainder goes to the last paragraph
                remaining = target_dur_us - (timeline_cursor - section_start_us)
                para_span_us = max(para_span_us, remaining)

            para_start_us = timeline_cursor

            if p["path"] is not None and p["dur_us"] > 0:
                _append_audio_segment(p["path"], para_start_us, p["dur_us"],
                                      mat_audios, mat_speeds, mat_material_animations,
                                      audio_segments)

            para_idx = p["para_idx"]
            pool_list = videos_by_para.get(para_idx) or section_pool
            if not pool_list:
                fallback_clip = _single_gap_fill_clip(
                    section["title"] if p["path"] is None
                    else _paragraph_text_for(sections, s_idx, para_idx),
                    section_dir, para_span_us / US, tag=f"{s_idx:02d}p{para_idx:02d}")
                pool_list = [fallback_clip] if fallback_clip else []
                missing_report.append((
                    s_idx, section["title"], para_idx,
                    "no clip for the paragraph or the section; a topic image was downloaded instead"
                    if fallback_clip else
                    "no clip for the paragraph or the section; the fallback image could not be downloaded",
                    fallback_clip.name if fallback_clip else None))
            elif para_idx not in videos_by_para:
                missing_report.append((
                    s_idx, section["title"], para_idx,
                    "no clip for this paragraph; other clips of the section were used", None))

            if pool_list:
                pool = cycle(pool_list)
                covered_us = 0
                used_count = 0
                while covered_us < para_span_us:
                    vf = next(pool)
                    used_count += 1
                    if used_count > len(pool_list) and used_count % len(pool_list) == 1:
                        missing_report.append((
                            s_idx, section["title"], para_idx,
                            "not enough clips; available clips were reused", vf.name))
                    real_dur_us = _media_duration_us(vf)
                    remaining_us = para_span_us - covered_us
                    seg_len_us = min(real_dur_us, remaining_us, _max_interval_us(timeline_cursor))
                    if seg_len_us <= 0:
                        break
                    _append_video_segment(
                        vf, timeline_cursor, seg_len_us, canvas_id, mat_videos, mat_speeds,
                        mat_sound_channel_mappings, mat_vocal_separations,
                        mat_material_animations, video_segments)
                    timeline_cursor += seg_len_us
                    covered_us += seg_len_us
            else:
                # No footage at all: the timeline still advances so audio and length stay correct.
                timeline_cursor += para_span_us

        log.info("Timeline '%s': %.1f s of %.1f s target (%d paragraphs, %.2f s spare per paragraph)",
                 section["title"][:35], (timeline_cursor - section_start_us) / US,
                 target_dur_us / US, n, gap_per_para_us / US)

    total_duration = max(timeline_cursor, target_total_us, 1)
    project_id = _uid()
    now = _now_us()

    draft_info = {
        "canvas_config": {"height": cfg("capcut.canvas_height"), "ratio": "16:9",
                          "width": cfg("capcut.canvas_width")},
        "color_space": 0,
        "config": {
            "adjust_max_index": 1, "attachment_info": [], "combination_max_index": 1,
            "export_range": None, "extract_audio_last_index": 1, "lyrics_recognition_id": "",
            "lyrics_sync": True, "lyrics_taskinfo": [], "maintrack_adsorb": True,
            "material_save_mode": 0, "original_sound_last_index": 1,
            "record_audio_last_index": 1, "sticker_max_index": 1,
            "subtitle_recognition_id": "", "subtitle_sync": True, "subtitle_taskinfo": [],
            "system_font_list": [], "video_mute": False, "zoom_info_params": None,
        },
        "cover": None, "create_time": 0, "duration": total_duration, "extra_info": None,
        "fps": float(cfg("capcut.fps")), "free_render_index_mode_on": False,
        "group_container": None, "id": project_id, "keyframe_graph_list": [],
        "keyframes": {"adjusts": [], "audios": [], "effects": [], "filters": [],
                      "handwrites": [], "stickers": [], "texts": text_kf_groups, "videos": []},
        "last_modified_platform": _platform_block(),
        "materials": {
            "audio_balances": [], "audio_effects": [], "audio_fades": [],
            "audio_track_indexes": [], "audios": mat_audios, "beats": [],
            "canvases": mat_canvases, "chromas": [], "color_curves": [],
            "digital_humans": [], "drafts": [], "effects": [], "flowers": [],
            "green_screens": [], "handwrites": [], "hsl": [], "images": [],
            "log_color_wheels": [], "loudnesses": [], "manual_deformations": [],
            "masks": [], "material_animations": mat_material_animations,
            "material_colors": [], "placeholders": [], "plugin_effects": [],
            "primary_color_wheels": [], "realtime_denoises": [], "shapes": [],
            "smart_crops": [], "smart_relights": [],
            "sound_channel_mappings": mat_sound_channel_mappings, "speeds": mat_speeds,
            "stickers": [], "tail_leaders": [], "text_templates": [], "texts": mat_texts,
            "transitions": [], "video_effects": [], "video_trackings": [],
            "videos": mat_videos, "vocal_beautifys": [],
            "vocal_separations": mat_vocal_separations,
        },
        "mutable_config": None, "name": safe_name, "new_version": "97.0.0",
        "platform": _platform_block(), "relationships": [],
        "render_index_track_mode_on": True, "retouch_cover": None, "source": "default",
        "static_cover_image_path": "", "tracks": [], "update_time": 0, "version": 360000,
    }

    for track_type, segments in (("video", video_segments), ("audio", audio_segments),
                                 ("text", text_segments)):
        if segments:
            draft_info["tracks"].append({
                "attribute": 0, "flag": 0, "id": _uid(), "is_default_name": True,
                "name": "", "segments": segments, "type": track_type,
            })

    # Register the project in CapCut's root_meta_info.json so it shows up in the project list.
    root_meta_path = capcut_root / "root_meta_info.json"
    root_meta = {"all_draft_store": [], "draft_ids": 0, "root_path": str(capcut_root)}
    if root_meta_path.exists():
        try:
            root_meta = json.loads(root_meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            log.warning("Could not read %s; creating a new one.", root_meta_path)

    root_meta["all_draft_store"] = [d for d in root_meta.get("all_draft_store", [])
                                    if d.get("draft_name") != safe_name]

    draft_meta = {
        "draft_cloud_last_action_download": False, "draft_cloud_purchase_info": "",
        "draft_cloud_template_id": "", "draft_cloud_tutorial_info": "",
        "draft_cloud_videocut_purchase_info": "",
        "draft_cover": str(project_dir / "draft_cover.jpg"),
        "draft_fold_path": str(project_dir), "draft_id": project_id,
        "draft_is_ai_shorts": False, "draft_is_invisible": False,
        "draft_json_file": str(project_dir / "draft_info.json"),
        "draft_name": safe_name, "draft_new_version": "",
        "draft_root_path": str(capcut_root), "draft_timeline_materials_size": 0,
        "draft_type": "", "tm_draft_cloud_completed": "", "tm_draft_cloud_modified": 0,
        "tm_draft_create": now, "tm_draft_modified": now, "tm_draft_removed": 0,
        "tm_duration": total_duration,
    }
    root_meta["all_draft_store"].insert(0, dict(draft_meta))
    root_meta["draft_ids"] = len(root_meta["all_draft_store"])
    root_meta["root_path"] = str(capcut_root)

    (project_dir / "draft_info.json").write_text(
        json.dumps(draft_info, ensure_ascii=False), encoding="utf-8")
    (project_dir / "draft_meta_info.json").write_text(
        json.dumps(draft_meta, ensure_ascii=False), encoding="utf-8")
    (project_dir / "attachment_pc_common.json").write_text(
        json.dumps({"info_list": []}), encoding="utf-8")
    (project_dir / "key_value.json").write_text(json.dumps({}), encoding="utf-8")
    root_meta_path.write_text(json.dumps(root_meta, ensure_ascii=False), encoding="utf-8")

    log.info("CapCut project created: %s", project_dir)
    log.info("Segments: %d video, %d audio, %d title", len(video_segments),
             len(audio_segments), len(text_segments))
    log.info("Length: %.1f s (%.2f min), requested %s min", total_duration / US,
             total_duration / US / 60, total_minutes)
    log.info("Restart CapCut; the project appears at the top of the project list.")

    if missing_report:
        report_lines = ["MISSING / REUSED FOOTAGE REPORT", "=" * 60, ""]
        for s_idx, title, para_idx, reason, clip_name in missing_report:
            report_lines.append(f"[Section {s_idx} - \"{title}\" - Paragraph {para_idx}] {reason}"
                                + (f" ({clip_name})" if clip_name else ""))
        report_path = project_dir / "missing_visuals_report.txt"
        report_path.write_text("\n".join(report_lines), encoding="utf-8")
        log.warning("Ideal footage was missing for %d segments (clips were reused or replaced). "
                    "See %s", len(missing_report), report_path)

    return project_dir
