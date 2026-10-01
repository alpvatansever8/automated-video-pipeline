import pytest

from video_pipeline import config


@pytest.fixture(autouse=True)
def isolated_settings(tmp_path, monkeypatch):
    """Use default settings, an empty environment and a temporary output folder."""
    for name in ("PEXELS_API_KEY", "PIXABAY_API_KEY", "ELEVENLABS_API_KEY", "GEMINI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(config, "load_dotenv", lambda *a, **k: False)  # never read a real .env
    config.load_settings(overrides={"paths": {"output_dir": str(tmp_path / "output")}})
    yield
    config.load_settings()
