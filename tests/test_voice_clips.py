"""Clip audition surfaces (#68): list, play, delete one clip.

The owner suspected a polluted anchor and had no way to verify it by ear.
The contract under test:

- The clip list is metadata only, newest first, quarantine visible.
- Audio is served ONLY for a file token the person's own index lists - a
  token is never trusted as a path, so another person's clip (or any file
  on disk) is unreachable through this route.
- Playback is read-only with respect to anchor state.
- Deleting one clip removes exactly that clip; sufficiency recomputes; the
  last clip's deletion leaves the person known but unlearnt.
"""

import struct

import pytest
from fastapi.testclient import TestClient

from backend import anchors
from backend.app import create_app
from backend.config import Settings


@pytest.fixture
def app(tmp_path):
    return create_app(Settings(data_dir=str(tmp_path / "data"),
                               memory_url="http://127.0.0.1:1"))


def _pcm(seconds=2.0, rate=16000, amp=6000):
    n = int(seconds * rate)
    return struct.pack(f"<{n}h", *([amp, -amp] * (n // 2)))


def _grow(name, clips=3):
    store = anchors.store()
    pid = store.ensure_person(name)
    for _ in range(clips):
        assert store.add_clip(pid, _pcm(), 16000, source="accumulated")
    return pid


def test_clip_list_is_metadata_only_newest_first(app):
    with TestClient(app, base_url="http://127.0.0.1") as c:
        pid = _grow("Alex")
        r = c.get(f"/api/voice/people/{pid}/clips")
        assert r.status_code == 200
        clips = r.json()["clips"]
        assert len(clips) == 3
        assert set(clips[0]) == {"file", "source", "added_at", "seconds",
                                 "score", "quarantined", "moved"}
        assert [c_["added_at"] for c_ in clips] == sorted(
            (c_["added_at"] for c_ in clips), reverse=True)
        assert c.get("/api/voice/people/nobody-000000/clips").status_code == 404


def test_audio_serves_only_the_persons_own_clips(app):
    with TestClient(app, base_url="http://127.0.0.1") as c:
        pid = _grow("Alex")
        other = _grow("Sam")
        mine = c.get(f"/api/voice/people/{pid}/clips").json()["clips"][0]["file"]
        theirs = c.get(f"/api/voice/people/{other}/clips").json()["clips"][0]["file"]

        r = c.get(f"/api/voice/people/{pid}/clips/{mine}/audio")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("audio/wav")
        assert r.content[:4] == b"RIFF"

        # someone else's clip through my id: refused
        assert c.get(f"/api/voice/people/{pid}/clips/{theirs}/audio"
                     ).status_code == 404
        # a path, not a token: refused (the index lookup can never match)
        assert c.get(f"/api/voice/people/{pid}/clips/..%2Findex.json/audio"
                     ).status_code == 404


def test_playback_is_readonly_for_anchor_state(app):
    with TestClient(app, base_url="http://127.0.0.1") as c:
        pid = _grow("Alex")
        before = [p for p in anchors.store().people()
                  if p["person_id"] == pid][0]
        fname = c.get(f"/api/voice/people/{pid}/clips").json()["clips"][0]["file"]
        for _ in range(3):
            assert c.get(f"/api/voice/people/{pid}/clips/{fname}/audio"
                         ).status_code == 200
        after = [p for p in anchors.store().people()
                 if p["person_id"] == pid][0]
        assert before == after


def test_delete_removes_exactly_one_and_recomputes(app):
    with TestClient(app, base_url="http://127.0.0.1") as c:
        pid = _grow("Alex", clips=3)
        clips = c.get(f"/api/voice/people/{pid}/clips").json()["clips"]
        doomed = clips[0]["file"]
        path = anchors.store().clip_path(pid, doomed)
        assert path is not None and path.exists()

        r = c.delete(f"/api/voice/people/{pid}/clips/{doomed}")
        assert r.status_code == 200
        assert not path.exists()
        left = c.get(f"/api/voice/people/{pid}/clips").json()["clips"]
        assert len(left) == 2 and doomed not in [c_["file"] for c_ in left]
        # deleting it again: gone is gone
        assert c.delete(f"/api/voice/people/{pid}/clips/{doomed}"
                        ).status_code == 404


def test_create_person_by_name(app):
    """#90: a person can exist before any voice - anchor-pending, exactly
    as an introduction leaves them. Participant names are refused at this
    door too, and a taken name offers the existing person."""
    with TestClient(app, base_url="http://127.0.0.1") as c:
        r = c.post("/api/voice/people", json={"name": "Faye"})
        assert r.status_code == 200
        pid = r.json()["person_id"]
        assert c.get(f"/api/voice/people/{pid}/clips").json()["clips"] == []

        assert c.post("/api/voice/people", json={"name": ""}).status_code == 400
        # the #77 boundary holds at every door, variants included
        assert c.post("/api/voice/people",
                      json={"name": "Claude"}).status_code == 400
        assert c.post("/api/voice/people",
                      json={"name": "Clyde"}).status_code == 400
        r = c.post("/api/voice/people", json={"name": "faye"})
        assert r.status_code == 409
        assert r.json()["detail"]["conflict"]["person_id"] == pid


def test_move_clip_refiles_without_touching_audio(app):
    """#90: the live contamination case end to end - a clip banked under
    the wrong person moves to the right one; audio untouched, quarantine
    cleared, both banks re-derive."""
    with TestClient(app, base_url="http://127.0.0.1") as c:
        kat = _grow("Sam", clips=4)          # 8s: sufficient
        faye = c.post("/api/voice/people",
                      json={"name": "Alex"}).json()["person_id"]
        clips = c.get(f"/api/voice/people/{kat}/clips").json()["clips"]
        wrong = clips[0]["file"]
        # simulate the hygiene audit having set it aside under the wrong owner
        store = anchors.store()
        with store._lock:
            data = store._load()
            for c_ in data["people"][kat]["clips"]:
                if c_["file"] == wrong:
                    c_["quarantined"] = True
            store._save(data)
        path = store.clip_path(kat, wrong)

        r = c.post(f"/api/voice/people/{kat}/clips/{wrong}/move",
                   json={"to": faye})
        assert r.status_code == 200
        assert path.exists()                                  # audio untouched
        src = c.get(f"/api/voice/people/{kat}/clips").json()["clips"]
        dst = c.get(f"/api/voice/people/{faye}/clips").json()["clips"]
        assert wrong not in [x["file"] for x in src]
        moved = next(x for x in dst if x["file"] == wrong)
        assert moved["quarantined"] is False                  # re-judged later
        assert moved["moved"] is True                         # owner provenance
        # audio now serves through the NEW person, not the old one
        assert c.get(f"/api/voice/people/{faye}/clips/{wrong}/audio"
                     ).status_code == 200
        assert c.get(f"/api/voice/people/{kat}/clips/{wrong}/audio"
                     ).status_code == 404

        assert c.post(f"/api/voice/people/{kat}/clips/{wrong}/move",
                      json={"to": faye}).status_code == 404   # already gone
        assert c.post(f"/api/voice/people/{faye}/clips/{wrong}/move",
                      json={"to": faye}).status_code == 400   # to itself
        assert c.post(f"/api/voice/people/{faye}/clips/{wrong}/move",
                      json={"to": "nobody-000000"}).status_code == 404


def test_alias_records_another_spelling_without_renaming(app):
    """#90: a phonetic or misspelt form joins a person's identity names;
    the display name is untouched, a spelling that belongs to someone else
    offers the conflict, and the participant boundary holds here too."""
    with TestClient(app, base_url="http://127.0.0.1") as c:
        pid = _grow("Catriona", clips=1)
        r = c.post(f"/api/voice/people/{pid}/alias", json={"name": "Kat"})
        assert r.status_code == 200
        me = [p for p in anchors.store().people() if p["person_id"] == pid][0]
        assert "Kat" in me["merged_names"]
        assert me["preferred_name"] == "Catriona"       # display untouched
        # resolving by the new spelling finds the same person
        assert anchors.store().find_by_name("kat")["person_id"] == pid

        other = _grow("Dave", clips=1)
        r = c.post(f"/api/voice/people/{other}/alias", json={"name": "Kat"})
        assert r.status_code == 409                     # someone else's name
        assert r.json()["detail"]["conflict"]["person_id"] == pid
        assert c.post(f"/api/voice/people/{other}/alias",
                      json={"name": "Clyde"}).status_code == 400
        assert c.post("/api/voice/people/nobody-000000/alias",
                      json={"name": "Zed"}).status_code == 404


def test_deleting_the_last_clip_leaves_person_known_but_unlearnt(app):
    with TestClient(app, base_url="http://127.0.0.1") as c:
        pid = _grow("Alex", clips=4)   # 4 x 2s = 8s, past the 6s bar
        person = [p for p in anchors.store().people()
                  if p["person_id"] == pid][0]
        assert person["sufficient"] is True
        for c_ in list(c.get(f"/api/voice/people/{pid}/clips").json()["clips"]):
            assert c.delete(f"/api/voice/people/{pid}/clips/{c_['file']}"
                            ).status_code == 200
        person = [p for p in anchors.store().people()
                  if p["person_id"] == pid][0]
        assert person["sufficient"] is False
        assert person["clip_count"] == 0
        # still a known person - re-learnable, not forgotten
        assert c.get(f"/api/voice/people/{pid}/clips").json()["clips"] == []
