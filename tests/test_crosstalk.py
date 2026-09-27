"""Crosstalk: what a two-voice turn's label may show, and what a correction
keeps (#28 phase 4, #482).

The physics, from the design on the issue: on a single microphone the quieter
voice's overlapped words are often unrecoverable, and their ABSENCE from the
transcript is undetectable. What can be done honestly:

1. MARK: a turn two voices spoke in carries a crosstalk marker in
   voice_labels - message content stays immutable - and counts as
   uncertain everywhere downstream (memory ingests it as guest:unknown,
   pinned in test_memory_attribution).
2. SPLIT, ONLY WHEN IT READS AS THE MESSAGE: the per-voice split rides the
   metadata only when its words, joined, read as the text the message
   carries; otherwise the bare marker stands. How the split is made on this
   computer is pinned in test_local_crosstalk.
3. A CORRECTION answers who spoke, not what was lost: it keeps the marker
   and drops the split.
"""

import json

import pytest
from fastapi.testclient import TestClient

from backend import db, diarize
from backend.app import create_app
from backend.config import Settings
from roomkit import _insert_user_message, _message_labels


def test_segments_align_matches_normalised_text_only():
    segs = [{"label": "A", "text": "Pass the salt"},
            {"label": "B", "text": "and pepper!"}]
    assert diarize.segments_align(segs, "pass the salt, AND pepper") is True
    # a different word means the two transcribers disagree - no split shown
    assert diarize.segments_align(segs, "pass the pepper and salt") is False
    assert diarize.segments_align([], "anything") is False
    assert diarize.segments_align(segs, "") is False


def test_fit_to_message_drops_a_split_that_disagrees_with_the_text():
    payload = {"labels": ["Alex", "Sam"], "crosstalk": True,
               "segments": [{"label": "Alex", "text": "hello"},
                            {"label": "Sam", "text": "world"}]}
    assert diarize.fit_to_message(payload, "Hello, world!") == payload
    trimmed = diarize.fit_to_message(payload, "hello there world")
    assert "segments" not in trimmed and trimmed["crosstalk"] is True
    assert diarize.fit_to_message(None, "anything") is None


@pytest.fixture
def app(tmp_path):
    settings = Settings(data_dir=str(tmp_path / "data"),
                        memory_url="http://127.0.0.1:1")
    return create_app(settings)


def test_correction_keeps_the_marker_but_drops_the_split(app):
    """Tap-to-correct answers WHO spoke, not WHAT was lost: the corrected
    payload keeps crosstalk/overlap (so ingest keeps quarantining the turn)
    and drops the split whose per-voice labels no longer apply."""
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={"participant_ids": []}).json()
        msg = _insert_user_message(chat["id"], "hello world")
        con = db.connect()
        db.set_message_voice_labels(con, msg["id"], {
            "clusters": ["s0", "s1"], "labels": ["Voice 1", "Voice 2"],
            "uncertain": ["Voice 1", "Voice 2"], "crosstalk": True,
            "overlap": False,
            "segments": [{"label": "Voice 1", "text": "hello",
                          "uncertain": True}]})
        con.close()
        r = c.post(f"/api/chats/{chat['id']}/messages/{msg['id']}/speaker",
                   json={"name": "Alex"})
        assert r.status_code == 200
    data = json.loads(_message_labels(msg["id"]))
    assert data["labels"] == ["Alex"] and data["corrected"] is True
    assert data["crosstalk"] is True and data["overlap"] is False
    assert "segments" not in data
