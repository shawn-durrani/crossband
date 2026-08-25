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


# ── confidence-driven re-audition (#221) ────────────────────────────────────
#
# Vouching is person-level and permanent, but accumulation can replace a
# vouched bank's entire CONTENT afterwards - the takeover shape of the
# 2026-08-24 incident. The save-time match scores now decide such a bank's
# standing: low pauses identification for the owner's ear (same #83 flow,
# same already-seated exception), high keeps working but is flagged. A
# fully self-collected bank always carries the ask, whatever its scores:
# save-time confidence is graded against the bank itself, and the polluted
# bank graded its own donor highly.

def _auto(added_at, score=None, seconds=2.0):
    c = {"file": f"f{added_at}", "seconds": seconds, "score": 1.0,
         "source": "accumulated", "added_at": added_at, "sample_rate": 16000}
    if score is not None:
        c["match_score"] = score
    return c


def test_trust_rule_and_floor():
    human = {"clips": [_auto(1, 0.5),
                       {"file": "i", "seconds": 2.0, "source": "introduction",
                        "added_at": 0}], "vouched_at": 1.0}
    assert anchors.bank_trust(human) == "human"
    moved = {"clips": [_auto(1, 0.5) | {"moved_at": 5}], "vouched_at": 1.0}
    assert anchors.bank_trust(moved) == "human"     # owner-moved counts
    # vouched once, human backing rotated out: the save-time scores decide
    weak = {"clips": [_auto(i, 0.55) for i in range(4)], "vouched_at": 1.0}
    strong = {"clips": [_auto(i, 0.72) for i in range(4)], "vouched_at": 1.0}
    assert anchors.bank_trust(weak) == "low"
    assert anchors.bank_trust(strong) == "high"
    # pre-#221 clips carry no scores: high, never an upgrade shock
    unscored = {"clips": [_auto(i) for i in range(4)], "vouched_at": 1.0}
    assert anchors.bank_trust(unscored) == "high"
    # the floor: never human-backed is 'self' whatever the scores say
    selfmade = {"clips": [_auto(i, 0.9) for i in range(4)]}
    assert anchors.bank_trust(selfmade) == "self"
    # an audition newer than the newest clip is the owner's ear on today's
    # content; clips banked after it reopen the question
    heard = {"clips": [_auto(i, 0.55) for i in range(4)],
             "vouched_at": 1.0, "audition_confirmed_at": 10.0}
    assert anchors.bank_trust(heard) == "human"
    stale = {"clips": [_auto(i, 0.55) for i in range(4)] + [_auto(99, 0.55)],
             "vouched_at": 1.0, "audition_confirmed_at": 10.0}
    assert anchors.bank_trust(stale) == "low"


def test_low_trust_asks_and_pauses_high_trust_only_flags():
    low = {"clips": [_auto(i, 0.55) for i in range(4)], "vouched_at": 1.0}
    assert anchors.needs_audition(low) is True
    assert anchors.identification_paused(low) is True   # no stamp needed
    high = {"clips": [_auto(i, 0.72) for i in range(4)], "vouched_at": 1.0}
    assert anchors.needs_audition(high) is False
    assert anchors.identification_paused(high) is False
    # the floor: fully self-collected always carries the ask, even with
    # strong scores - they were graded against the bank itself
    selfmade = {"clips": [_auto(i, 0.9) for i in range(4)]}
    assert anchors.needs_audition(selfmade) is True
    # an insufficient bank asks nothing yet
    young = {"clips": [_auto(1, 0.55)], "vouched_at": 1.0}
    assert anchors.needs_audition(young) is False


def test_low_trust_pause_honours_the_already_seated_exception(app):
    with TestClient(app, base_url="http://127.0.0.1"):
        store = anchors.store()
        pid = store.ensure_person("Alex")
        store.add_clip(pid, _pcm(), 16000, source="introduction")
        _fill(store, pid)
        # rotation replaces the human-backed clip; the survivors banked weak
        data = store._load()
        data["people"][pid]["clips"] = [
            c | {"source": "accumulated", "match_score": 0.55}
            for c in data["people"][pid]["clips"]]
        store._save(data)
        assert _flags(store, pid) == {"sufficient": True, "vouched": True,
                                      "needs_audition": True,
                                      "id_paused": True}
        # dropped from every fresh candidate list, but a person already
        # seated in the live chat keeps being identified (#83's exception)
        assert _candidate_ids(store) == set()
        assert _candidate_ids(store, rostered={pid}) == {pid}


def test_accumulation_stamps_the_match_score(app):
    with TestClient(app, base_url="http://127.0.0.1"):
        from backend import diarize
        store = anchors.store()
        pid = store.ensure_person("Alex")
        diarize._accumulate_fast_anchor(pid, _pcm(5.0), 16000, {}, score=0.71)
        clips = store._load()["people"][pid]["clips"]
        assert clips and all(c.get("match_score") == 0.71 for c in clips)
        assert {c["source"] for c in clips} == {"accumulated",
                                               "harvested-short"}
