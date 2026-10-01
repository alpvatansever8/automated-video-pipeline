from pathlib import Path

from video_pipeline.main import main

SAMPLE = Path(__file__).resolve().parents[1] / "examples" / "sample_script.txt"


def test_dry_run_succeeds_without_keys_and_writes_nothing(tmp_path, capsys):
    out = tmp_path / "out"
    code = main(["--script", str(SAMPLE), "--dry-run", "--output-dir", str(out)])
    assert code == 0
    assert not out.exists()


def test_missing_script_gives_clear_error(tmp_path):
    assert main(["--script", str(tmp_path / "nope.txt"), "--dry-run"]) == 2


def test_missing_api_key_stops_real_run(tmp_path, caplog):
    code = main(["--script", str(SAMPLE), "--mode", "video", "--output-dir", str(tmp_path / "o")])
    assert code == 2
    assert "PEXELS_API_KEY" in caplog.text


def test_invalid_section_range(tmp_path):
    assert main(["--script", str(SAMPLE), "--dry-run", "--from-section", "99"]) == 2


def test_modules_import():
    import importlib
    for name in ("ai", "attribution", "capcut_export", "collector", "config", "dedup",
                 "geo_filter", "landmarks", "script_parser", "util", "voiceover",
                 "footage.pexels", "footage.pixabay", "footage.wikimedia"):
        importlib.import_module(f"video_pipeline.{name}")
