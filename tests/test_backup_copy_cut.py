"""A rescued voice turn never repeats what realtime already delivered (#455).

The backup recording runs beside live transcription and covers every turn
since it started. When live transcription fails partway through a long
turn, the earlier pieces already have their words, and a copy transcribed
whole would say them again. The browser now says where in the recording
the turn's own words begin (`from_ms`), and the server keeps only the
words from there on, cut on Scribe's word times by each word's middle.
Pins: the cut itself, a word the cut runs through, spaces and sounds, a
missing or broken word list keeping the whole text, and the /stt route
asking for word times only when there's a cut to make, logging the cut
without a word of what was said."""

import pytest
from fastapi.testclient import TestClient

from backend import voice
from backend.app import create_app
from backend.config import Settings
from backend.routers import voice as voice_router


def _words(*spoken):
    """Scribe's word list for (start, end, word) triples, with the spaces
    between them, as Scribe sends it."""
    out = []
    for i, (a, b, w) in enumerate(spoken):
        if i:
            prev_end = spoken[i - 1][1]
            out.append({"text": " ", "start": prev_end, "end": a,
                        "type": "spacing"})
        out.append({"text": w, "start": a, "end": b, "type": "word"})
    return out


SPOKEN = ((0.5, 0.9, "Alex"), (1.0, 1.6, "measured"), (1.7, 1.9, "the"),
          (2.0, 2.4, "deck."), (12.6, 12.9, "Then"), (13.0, 13.3, "Sam"),
          (13.4, 13.7, "cut"), (13.8, 14.3, "boards."))
WORDS = _words(*SPOKEN)
FULL = "".join(w["text"] for w in WORDS)


def test_the_words_after_the_cut_are_kept():
    assert voice.words_from(WORDS, 12.4) == ("Then Sam cut boards.", 4, 4)


def test_no_cut_keeps_everything():
    assert voice.words_from(WORDS, 0.0) == (FULL, 8, 0)


def test_a_word_the_cut_runs_through_goes_with_its_larger_side():
    # "Sam" runs 13.0 to 13.3, so its middle is 13.15.
    assert voice.words_from(WORDS, 13.1)[0] == "Sam cut boards."
    assert voice.words_from(WORDS, 13.2)[0] == "cut boards."


def test_a_cut_after_everything_leaves_nothing():
    assert voice.words_from(WORDS, 20.0) == ("", 0, 8)


def test_sounds_are_kept_by_the_same_rule():
    words = WORDS + [{"text": " ", "start": 14.3, "end": 14.5, "type": "spacing"},
                     {"text": "(laughs)", "start": 14.5, "end": 15.0,
                      "type": "audio_event"}]
    assert voice.words_from(words, 12.4)[0] == "Then Sam cut boards. (laughs)"


@pytest.mark.parametrize("words", [None, [], "words", [{"text": "Alex"}],
                                   [{"text": "Alex", "start": None, "end": 1}]])
def test_without_word_times_there_is_no_cut(words):
    assert voice.words_from(words, 5.0) is None


# ---- the route ----

@pytest.fixture
def app(tmp_path):
    return create_app(Settings(data_dir=str(tmp_path / "data"),
                               memory_url="http://127.0.0.1:1"))


@pytest.fixture
def scribe(monkeypatch):
    """Scribe's batch answer: FULL, with WORDS when asked for them."""
    calls = []

    def fake(data, mime, cfg, with_words=False):
        calls.append(with_words)
        return (FULL, "scribe_v2", WORDS) if with_words else (FULL, "scribe_v2")

    monkeypatch.setattr(voice_router.voice, "provider_for",
                        lambda cfg: voice_router.voice.PROVIDER_ELEVENLABS)
    monkeypatch.setattr(voice_router.voice, "transcribe", fake)
    return calls


def _post(client, chat_id, **data):
    return client.post(f"/api/chats/{chat_id}/stt",
                       files={"file": ("utterance.webm", b"\x1a\x45" * 64,
                                       "audio/webm")},
                       data={"duration_ms": "3000", **data})


def test_the_route_cuts_at_from_ms_and_logs_no_words(app, scribe, caplog):
    caplog.set_level("INFO", logger="crossband.voice")
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={}).json()
        r = _post(c, chat["id"], from_ms="12400", why="late")
    assert r.status_code == 200
    assert r.json()["text"] == "Then Sam cut boards."
    assert scribe == [True]
    cut = [r.getMessage() for r in caplog.records
           if r.getMessage().startswith("stt backup cut")]
    assert cut == [f"stt backup cut: chat={chat['id']} from_s=12.4 "
                   "words_kept=4 words_dropped=4"]
    for word in ("Alex", "measured", "Sam", "boards"):
        assert not any(word in r.getMessage() for r in caplog.records)


def test_without_from_ms_the_route_is_unchanged(app, scribe):
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={}).json()
        for data in ({}, {"from_ms": "0"}):
            r = _post(c, chat["id"], **data)
            assert r.json()["text"] == FULL
    # word times are never asked for without a cut to make
    assert scribe == [False, False]


def test_with_no_word_times_the_whole_text_stands(app, monkeypatch, caplog):
    monkeypatch.setattr(voice_router.voice, "provider_for",
                        lambda cfg: voice_router.voice.PROVIDER_ELEVENLABS)
    monkeypatch.setattr(voice_router.voice, "transcribe",
                        lambda data, mime, cfg, with_words=False:
                        (FULL, "scribe_v2", None))
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={}).json()
        r = _post(c, chat["id"], from_ms="12400")
    assert r.json()["text"] == FULL
    assert any("no word times" in r.getMessage() for r in caplog.records)


def test_transcribe_asks_scribe_once_and_returns_its_words(monkeypatch):
    class R:
        status_code = 200

        def json(self):
            return {"text": " " + FULL + " ", "words": WORDS}

    sent = []
    monkeypatch.setattr(voice.httpx, "post",
                        lambda *a, **kw: sent.append(kw["data"]) or R())
    monkeypatch.setattr(voice, "_headers", lambda: {})
    assert voice.transcribe(b"x", "audio/webm", {}) == (FULL, "scribe_v2")
    assert voice.transcribe(b"x", "audio/webm", {}, with_words=True) == (
        FULL, "scribe_v2", WORDS)
    assert sent == [{"model_id": "scribe_v2"}] * 2
