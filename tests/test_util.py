import pytest
import requests

from video_pipeline import util
from video_pipeline.capcut_export import _para_index_from_audio_name


def test_paragraph_suffix_roundtrip():
    for idx in (0, 1, 25, 26, 27, 51, 52):
        suffix = util.paragraph_suffix(idx)
        assert util.paragraph_index_from_suffix(suffix) == idx + 1
    assert _para_index_from_audio_name("03b") == 2


def test_section_dir_name_is_filesystem_safe():
    assert util.section_dir_name(2, 'Blue/Lagoon: "Spa"') == "02_Blue-Lagoon-_-Spa-"


class FakeResponse:
    def __init__(self, status, headers=None):
        self.status_code = status
        self.headers = headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}", response=self)


def test_retry_on_429_then_success(monkeypatch):
    responses = iter([FakeResponse(429, {"Retry-After": "0"}), FakeResponse(200)])
    monkeypatch.setattr(util.requests, "request", lambda *a, **k: next(responses))
    monkeypatch.setattr(util.time, "sleep", lambda s: None)
    assert util.request_with_retry("GET", "https://example.test").status_code == 200


def test_gives_up_after_retries(monkeypatch):
    monkeypatch.setattr(util.requests, "request", lambda *a, **k: FakeResponse(503))
    monkeypatch.setattr(util.time, "sleep", lambda s: None)
    with pytest.raises(requests.HTTPError):
        util.request_with_retry("GET", "https://example.test")


def test_client_errors_are_not_retried(monkeypatch):
    calls = []

    def fake(*a, **k):
        calls.append(1)
        return FakeResponse(404)

    monkeypatch.setattr(util.requests, "request", fake)
    with pytest.raises(requests.HTTPError):
        util.request_with_retry("GET", "https://example.test")
    assert len(calls) == 1
