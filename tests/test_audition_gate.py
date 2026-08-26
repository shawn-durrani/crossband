"""A sufficient bank nobody vouched for must earn the owner's ear (#83).

The #65 phantoms were internally consistent - banks wholly of someone
else's voice under the wrong name pass every automated check. The one
signal that needs no new machinery: crossing the sufficiency line without
a single introduction or owner correction. The contract under test:

- an accumulation-only bank that crosses sufficiency needs audition and
  loses remembered-first rights: it is dropped from the shared candidate
  list every identification path uses, so the #65 event replayed cannot
  name or seat anyone in a fresh session;
- a person already SEATED in the live chat keeps being identified (the
  pause guards re-seating, not the seat) - rostered_ids is the exception;
- a bank built through introduction or correction never prompts;
- the owner's audition confirmation restores rights;
- a LEGACY sufficient bank (predates the crossing stamp) is flagged for
  the owner's ear but keeps working - shipping this must not pause the
  installed base;
- vouching survives a merge.
"""

import struct

import pytest
from fastapi.testclient import TestClient

from backend import anchors
from backend.app import create_app
from backend.config import Settings
from backend.diarize import remembered_candidates
from tests.conftest import speech_pcm


@pytest.fixture
def app(tmp_path):
    return create_app(Settings(data_dir=str(tmp_path / "data"),
                               memory_url="http://127.0.0.1:1"))


def _pcm(seconds=2.0, rate=16000, amp=6000):
    # Speech-shaped since #218: the anchor gate rejects a Nyquist square.
    return speech_pcm(seconds, rate, amp=amp)


def _fill(store, pid, source="accumulated", clips=4):
    for _ in range(clips):
        assert store.add_clip(pid, _pcm(), 16000, source=source)


def _flags(store, pid):
    p = [x for x in store.people() if x["person_id"] == pid][0]
    return {k: p[k] for k in ("sufficient", "vouched", "needs_audition",
                              "id_paused")}


def _candidate_ids(store, rostered=frozenset()):
    return {c["person_id"]
            for c in remembered_candidates(store.people(), rostered)}


def test_the_65_event_replayed_cannot_reseat_anyone(app):
    with TestClient(app, base_url="http://127.0.0.1"):
        store = anchors.store()
        pid = store.ensure_person("Alex")
        _fill(store, pid)                       # cold start + accumulation only
        assert _flags(store, pid) == {"sufficient": True, "vouched": False,
                                      "needs_audition": True,
                                      "id_paused": True}
        # a fresh session: not a candidate for any identification path
        assert _candidate_ids(store) == set()
        # the chat that seated them (elimination, or a human-placed seat):
        # still identified - the pause guards RE-seating, not the seat
        assert _candidate_ids(store, rostered={pid}) == {pid}


def test_an_introduced_or_corrected_bank_never_prompts(app):
    with TestClient(app, base_url="http://127.0.0.1"):
        store = anchors.store()
        a = store.ensure_person("Sam")
        store.add_clip(a, _pcm(), 16000, source="introduction")
        _fill(store, a)
        assert _flags(store, a) == {"sufficient": True, "vouched": True,
                                    "needs_audition": False,
                                    "id_paused": False}
        assert _candidate_ids(store) == {a}

        b = store.ensure_person("Robin")
        _fill(store, b, clips=2)                # not yet sufficient
        store.add_clip(b, _pcm(), 16000, source="correction")
        _fill(store, b)
        assert _flags(store, b)["needs_audition"] is False
        assert b in _candidate_ids(store)


def test_the_owner_audition_restores_rights(app):
    with TestClient(app, base_url="http://127.0.0.1") as c:
        store = anchors.store()
        pid = store.ensure_person("Alex")
        _fill(store, pid)
        assert pid not in _candidate_ids(store)
        r = c.post(f"/api/voice/people/{pid}/audition")
        assert r.json() == {"ok": True}
        assert _flags(store, pid) == {"sufficient": True, "vouched": True,
                                      "needs_audition": False,
                                      "id_paused": False}
        assert _candidate_ids(store) == {pid}
        assert c.post("/api/voice/people/nobody-000000/audition"
                      ).status_code == 404


def test_a_legacy_bank_is_flagged_but_keeps_working(app):
    with TestClient(app, base_url="http://127.0.0.1"):
        store = anchors.store()
        pid = store.ensure_person("Alex")
        _fill(store, pid)
        # simulate a bank from before the crossing stamp existed
        data = store._load()
        data["people"][pid].pop("sufficiency_crossed_at")
        store._save(data)
        assert _flags(store, pid) == {"sufficient": True, "vouched": False,
                                      "needs_audition": True,
                                      "id_paused": False}
        assert _candidate_ids(store) == {pid}


def test_vouching_survives_a_merge(app):
    with TestClient(app, base_url="http://127.0.0.1"):
        store = anchors.store()
        a = store.ensure_person("Sam")
        store.add_clip(a, _pcm(), 16000, source="introduction")
        b = store.ensure_person("Sammy")
        _fill(store, b)
        survivor = store.merge_people(a, b)
        assert survivor is not None
        assert _flags(store, survivor)["vouched"] is True
        assert _flags(store, survivor)["needs_audition"] is False
