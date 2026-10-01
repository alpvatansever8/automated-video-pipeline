"""Command-line entry point.

Examples:
    python -m video_pipeline.main --script examples/sample_script.txt --dry-run
    python -m video_pipeline.main --script my_script.docx --minutes 8 --mode all
"""

from __future__ import annotations

import argparse
import logging
import shutil
import sys
from pathlib import Path

from video_pipeline import __version__
from video_pipeline.config import (ConfigError, assets_dir, cfg, get_env, load_settings,
                                   output_dir, voiceovers_dir)

log = logging.getLogger("video_pipeline")

MODES = ("audio", "video", "all")


def build_parser() -> argparse.ArgumentParser:
    """Create the argument parser."""
    p = argparse.ArgumentParser(
        prog="video_pipeline",
        description="Automated pipeline for producing documentary-style videos: parses a "
                    "script, collects stock footage, generates a voice-over and exports a "
                    "CapCut project.",
    )
    p.add_argument("--script", required=True, help="Script file (.txt with '# Title' headings, or .docx).")
    p.add_argument("--mode", choices=MODES, default="all",
                   help="audio: voice-over only | video: footage only | all: footage, "
                        "voice-over and CapCut project (default: all).")
    p.add_argument("--minutes", type=float, default=None,
                   help="Target video length in minutes (default: total_minutes from the settings).")
    p.add_argument("--from-section", type=int, default=1, metavar="N",
                   help="First section to process, 1-based (footage and voice-over).")
    p.add_argument("--to-section", type=int, default=None, metavar="N",
                   help="Last section to process, inclusive (default: last section).")
    p.add_argument("--wikimedia", action="store_true",
                   help="Also collect one Wikimedia image per sentence into each section folder.")
    p.add_argument("--project-name", default=None,
                   help="Name of the CapCut project (default: the script file name).")
    p.add_argument("--config", default=None, help="Settings file (default: config/settings.yaml if it exists).")
    p.add_argument("--output-dir", default=None, help="Folder for all generated files (default: output).")
    p.add_argument("--reset-registry", action="store_true",
                   help="Forget previously downloaded items so they can be downloaded again.")
    p.add_argument("--dry-run", action="store_true",
                   help="Show the plan only: no network requests, no files written.")
    p.add_argument("-v", "--verbose", action="store_true", help="Show debug messages.")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return p


def _key_status(env_name: str) -> str:
    return "set" if get_env(env_name) else "MISSING"


def print_plan(sections: list[dict], args: argparse.Namespace, total_minutes: float) -> None:
    """Log what a real run would do. Makes no network requests and writes nothing."""
    from video_pipeline.capcut_export import capcut_projects_root
    from video_pipeline.collector import SOURCES, source_order
    from video_pipeline.util import paragraph_suffix, section_dir_name

    last = args.to_section if args.to_section is not None else len(sections)
    selected = [(i, s) for i, s in enumerate(sections, 1) if args.from_section <= i <= last]
    order = source_order()
    project_name = args.project_name or Path(args.script).stem

    log.info("DRY RUN - no network requests will be made and no files will be written.")
    log.info("Script: %s | mode: %s | target length: %s min", args.script, args.mode, total_minutes)
    log.info("Sections selected: %d of %d (%d-%d)", len(selected), len(sections),
             args.from_section, last)
    log.info("Footage sources, in order: %s", " > ".join(order))
    log.info("AI backend (Ollama or Gemini): not checked in a dry run")

    log.info("Configuration check:")
    for env_name in sorted({SOURCES[s][0] for s in order if SOURCES[s][0]}):
        log.info("  %-20s %s", env_name, _key_status(env_name))
    log.info("  %-20s %s", "ELEVENLABS_API_KEY", _key_status("ELEVENLABS_API_KEY"))
    log.info("  %-20s %s", "voiceover.voice_id", "set" if cfg("voiceover.voice_id") else "MISSING")
    for tool in ("ffmpeg", "ffprobe"):
        log.info("  %-20s %s", tool, "found" if shutil.which(tool) else "NOT FOUND")

    log.info("Plan:")
    for s_idx, section in selected:
        target = cfg("footage.clips_intro") if s_idx == 1 else cfg("footage.clips_per_paragraph")
        log.info("  Section %d: \"%s\" - %.1f min, %d paragraphs", s_idx, section["title"],
                 section["duration_minutes"], len(section["paragraphs"]))
        log.info("    footage folder : %s", assets_dir() / section_dir_name(s_idx, section["title"]))
        for p_idx, paragraph in enumerate(section["paragraphs"]):
            log.info("    paragraph %d: up to %d items | voice-over %s%s.mp3 | \"%s\"",
                     p_idx + 1, target, f"{s_idx:02d}", paragraph_suffix(p_idx), paragraph[:50])

    log.info("Output folder     : %s", output_dir())
    log.info("Voice-over folder : %s", voiceovers_dir())
    if args.mode == "all":
        log.info("CapCut project    : %s", capcut_projects_root() / project_name)
    log.info("Dry run finished.")


def run(args: argparse.Namespace) -> int:
    """Execute the pipeline for parsed arguments. Returns a process exit code."""
    from video_pipeline.script_parser import parse_script, save_plan_file

    script = Path(args.script)
    if not script.is_file():
        raise ConfigError(f"Script file not found: {script}")

    overrides: dict = {}
    if args.minutes is not None:
        if args.minutes <= 0:
            raise ConfigError("--minutes must be greater than zero.")
        overrides["total_minutes"] = args.minutes
    if args.output_dir:
        overrides["paths"] = {"output_dir": args.output_dir}
    load_settings(args.config, overrides)
    total_minutes = float(cfg("total_minutes"))

    sections = parse_script(script, total_minutes, save_plan=False)
    if args.from_section < 1 or args.from_section > len(sections):
        raise ConfigError(f"--from-section must be between 1 and {len(sections)}.")
    last = args.to_section if args.to_section is not None else len(sections)
    if last < args.from_section or last > len(sections):
        raise ConfigError(f"--to-section must be between {args.from_section} and {len(sections)}.")

    if args.dry_run:
        print_plan(sections, args, total_minutes)
        return 0

    from video_pipeline.attribution import save_attributions
    from video_pipeline.capcut_export import build_capcut_project
    from video_pipeline.collector import check_footage_config, log_shortfalls, process_sections
    from video_pipeline.dedup import reset
    from video_pipeline.footage.wikimedia import collect_wikimedia_for_all_sections
    from video_pipeline.voiceover import check_voiceover_config, generate_voiceovers

    # Validate everything that is needed before doing any work.
    if args.mode in ("video", "all"):
        check_footage_config()
    if args.mode in ("audio", "all"):
        check_voiceover_config()
    if args.mode == "all":
        for tool in ("ffmpeg", "ffprobe"):
            if not shutil.which(tool):
                raise ConfigError(f"{tool} was not found. Install FFmpeg and make sure it is on your PATH.")

    save_plan_file(sections, script, total_minutes)

    if args.reset_registry:
        reset()
        log.info("Download registry cleared.")

    if args.mode in ("video", "all"):
        log.info("Step 1: collecting footage")
        attributions, results = process_sections(sections, args.from_section, last)
        save_attributions(attributions, mode="a" if args.from_section > 1 else "w")
        log_shortfalls(results)

    if args.wikimedia:
        log.info("Collecting Wikimedia images (one per sentence)")
        collect_wikimedia_for_all_sections(sections, assets_dir(), args.from_section, last)

    if args.mode in ("audio", "all"):
        log.info("Step 2: generating voice-over")
        generate_voiceovers(sections, args.from_section, last)

    if args.mode == "all":
        log.info("Step 3: building the CapCut project")
        build_capcut_project(sections, total_minutes, args.project_name or script.stem)

    log.info("Done. Generated files are in %s", output_dir())
    return 0


def main(argv: list[str] | None = None) -> int:
    """Parse arguments, run the pipeline and translate errors into exit codes."""
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)-7s %(message)s")
    try:
        return run(args)
    except (ConfigError, ValueError) as exc:
        log.error("%s", exc)
        return 2
    except KeyboardInterrupt:
        log.error("Interrupted.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
