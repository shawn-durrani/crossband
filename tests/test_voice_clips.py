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
                                 "score", "quarantined"}
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
