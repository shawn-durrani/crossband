"""The speaker matcher loads at app startup (#473).

The matcher used to load lazily: the first voice check after a restart
claimed the warm, got nothing back and deferred "unavailable", so that turn
went unnamed. Crossband restarts on every deploy, so the owner's first
spoken turn after each deploy was always unnamed. Startup now claims the
same one warm, in the background, when the feature is on and the model file
is already on disk.

These run keyless and offline. conftest.py stubs voiceid._spawn_fetch to a
no-op; each test here swaps in its own recorder, and never downloads.
"""

import types

import pytest
from fastapi.testclient import TestClient

from backend import voiceid
from backend.app import create_app
from backend.config import Settings

_CANDIDATES = [{"person_id": "p-sam", "name": "Sam"}]


@pytest.fixture(autouse=True)
def _no_fake_model_left_behind(tmp_path):
    """The stand-in model file must not outlive its test: the data dir stays
    configured after a test, and the real-extractor integration test in
    test_voice_id.py loads whatever file sits at voiceid.model_path()."""
    yield
    (tmp_path / "data" / voiceid.MODELS_DIR_NAME
     / voiceid.MODEL_FILENAME).unlink(missing_ok=True)


@pytest.fixture
def spawned(monkeypatch):
    """Pretend sherpa-onnx is installed and count warm requests."""
    calls = []
    monkeypatch.setattr(voiceid, "sherpa_onnx", object())
    monkeypatch.setattr(voiceid, "_spawn_fetch", lambda cfg: calls.append(cfg))
    return calls


def _settings(tmp_path, *, model_on_disk=True, **over):
    data = tmp_path / "data"
    if model_on_disk:
        models = data / voiceid.MODELS_DIR_NAME
        models.mkdir(parents=True)
        (models / voiceid.MODEL_FILENAME).write_bytes(b"not a real model")
    return Settings(data_dir=str(data), memory_url="http://127.0.0.1:1",
                    **over)


def _start(settings):
    return TestClient(create_app(settings), base_url="http://127.0.0.1")


def test_startup_requests_the_warm_when_voice_id_is_on(tmp_path, spawned):
    settings = _settings(tmp_path)
    with _start(settings):
        assert len(spawned) == 1
        assert voiceid.matcher_status(settings.as_cfg()) == "fetching"


def test_a_check_before_the_warm_finishes_defers_as_today(tmp_path, spawned):
    """A turn that lands while the model is still loading defers exactly as
    the lazy path always did, and never starts a second warm."""
    settings = _settings(tmp_path)
    with _start(settings):
        v = voiceid.identify_utterance(b"\x00\x00" * 16000, 16000,
                                       _CANDIDATES, settings.as_cfg())
        assert v["status"] == "defer" and v["reason"] == "unavailable"
        assert len(spawned) == 1


def test_the_first_check_after_a_finished_warm_gets_the_matcher(
        tmp_path, monkeypatch):
    """The point of the change: once the startup warm has run, the first
    voice check finds the matcher ready instead of claiming the warm."""
    sentinel = object()
    fake = types.SimpleNamespace(
        SpeakerEmbeddingExtractor=lambda config: sentinel,
        SpeakerEmbeddingExtractorConfig=lambda **kw: kw)
    monkeypatch.setattr(voiceid, "sherpa_onnx", fake)
    monkeypatch.setattr(voiceid, "ensure_model", lambda cfg: voiceid.model_path())
    monkeypatch.setattr(voiceid, "_spawn_fetch", voiceid._warm)  # run inline
    settings = _settings(tmp_path)
    with _start(settings):
        assert voiceid.matcher_status(settings.as_cfg()) == "ready"
        assert voiceid._get_extractor(settings.as_cfg()) is sentinel


def test_startup_does_not_warm_with_voice_id_off(tmp_path, spawned):
    settings = _settings(tmp_path, voice_id_enabled=False)
    with _start(settings):
        assert spawned == []
        assert voiceid._state == "cold"
        assert voiceid.matcher_status(settings.as_cfg()) == "disabled"


def test_startup_never_downloads_the_model(tmp_path, spawned):
    """No model file yet: startup leaves the matcher cold, so nothing is
    fetched, and the first voice check still claims the warm lazily."""
    settings = _settings(tmp_path, model_on_disk=False)
    with _start(settings):
        assert spawned == []
        assert voiceid._state == "cold"
        v = voiceid.identify_utterance(b"\x00\x00" * 16000, 16000,
                                       _CANDIDATES, settings.as_cfg())
        assert v["reason"] == "unavailable"
        assert len(spawned) == 1
        assert voiceid._state == "fetching"


def test_startup_warm_needs_sherpa_onnx(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(voiceid, "sherpa_onnx", None)
    monkeypatch.setattr(voiceid, "_spawn_fetch", lambda cfg: calls.append(cfg))
    settings = _settings(tmp_path)
    with _start(settings):
        assert calls == []
        assert voiceid.matcher_status(settings.as_cfg()) == "unavailable"


def test_warm_at_startup_never_raises(tmp_path, spawned, monkeypatch):
    def boom():
        raise OSError("disk went away")
    monkeypatch.setattr(voiceid, "model_path", boom)
    assert voiceid.warm_at_startup({"voice_id_enabled": True}) is False
    assert spawned == []
