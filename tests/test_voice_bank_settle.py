"""A settled voice keeps its best clips, and what it drops leaves membro too.

The owner's decision of 27 September: "only keep the best, and once that
has been established it shouldn't change much". Keyless and synthetic,
with the anchor store on a temp directory and no model. Pinned here:

1. The settle rule (anchors.bank_established, anchors.settle_offer).
   An established bank takes an automatic clip at most once a week, and
   only one from a capture day it doesn't hold yet or one that beats the
   weakest automatic clip, which it then replaces. A refusal is counted
   with the reason "voice is settled". Human-backed clips always go in,
   an automatic clip never displaces a protected one, and a length class
   with room fills under the same weekly allowance. A bank that isn't
   established learns as it always has.
2. Established means the calibrated scorer's readiness verdict while it
   runs (voice_calibration.ready_for, registered by start and removed by
   stop), and otherwise a sufficient bank of ten active clips or more
   from two capture days or more.
3. Drops enter the correction ledger as delete rows with a reason:
   rotation, the settle rule's replacement, the hygiene audit setting a
   clip aside, and a merge's evictions. Only for a person membro can
   hold clips for, never during a restore from membro, never twice for
   one set-aside clip, and bounded so the owner's own rows are never
   trimmed.
4. The manifest (anchors.kept_shas) lists the active clips, by membro's
   stamp for a pulled clip and by file hash otherwise.

The sync side (the drops reaching membro, the manifest push, an older
membro) is pinned in tests/test_person_sync.py.
"""

import datetime
import hashlib
import os

import pytest

from backend import anchors, db, voice_calibration as vc
from tests.conftest import speech_pcm

SR = 16000
DAY = 86400
ON = {"voice_id_enabled": True, "voice_calibrated_scorer": True,
      "user_name": "Alex", "room_roster_max": 6}


def _day0():
    """Local noon twenty days ago, so clips on day d share a calendar date
    in any time zone."""
    d = datetime.date.today() - datetime.timedelta(days=20)
    return datetime.datetime.combine(d, datetime.time(12)).timestamp()


D0 = _day0()


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(anchors, "_readiness_source", None)
    anchors._store = None
    return anchors.store()


def _clip(seconds, amp=6000):
    """Synthetic speech whose quality score is its length in seconds. A
    different `amp` gives different bytes at the same score."""
    return speech_pcm(seconds, SR, amp=amp)


def _bank(store, name, days=(0, 1), n=anchors.KEEP_CLIPS, start=3.0,
          source="accumulated"):
    """A full long class for `name`: n clips of start, start+0.1, ...
    seconds, spread over `days` (offsets from D0). Returns the id."""
    pid = store.ensure_person(name)
    for i in range(n):
        day = days[i % len(days)]
        assert store.add_clip(pid, _clip(start + 0.1 * i), SR, source,
                              added_at=D0 + day * DAY + i * 60)
    return pid


def _person(store, pid):
    return next(p for p in store.people() if p["person_id"] == pid)


def _files(store):
    return sorted(f for f in os.listdir(store.root) if f.endswith(".wav"))


def _sha(store, fname):
    return hashlib.sha256((store.root / fname).read_bytes()).hexdigest()


def _age_allowance(store, pid, days):
    """Move the person's last settled acceptance `days` into the past."""
    data = store._load()
    data["people"][pid]["settled_clip_at"] -= days * DAY
    store._save(data)


def _drops(store):
    return [(c["from"], c["sha"], c["why"])
            for c in store.pending_corrections() if c.get("why")]


# ── 1. the settle rule ─────────────────────────────────────────────────────

def test_an_established_bank_refuses_a_clip_that_adds_nothing(store):
    pid = _bank(store, "Alex")
    assert _person(store, pid)["settled"] is True
    files = _files(store)
    # from a day the bank holds, and weaker than its weakest clip
    assert not store.add_clip(pid, _clip(2.5), SR, "accumulated",
                              added_at=D0 + DAY + 3600)
    assert _files(store) == files                  # nothing written or lost
    alex = _person(store, pid)
    assert alex["refusal_reason"] == anchors.SETTLED_REASON == \
        "voice is settled"
    assert alex["refused_last_week"] == 1
    assert alex["clip_count"] == anchors.KEEP_CLIPS


def test_a_better_clip_replaces_the_weakest_automatic_clip(store):
    pid = _bank(store, "Alex")
    weakest = min(store.clips_of(pid), key=lambda c: c["score"])
    assert store.add_clip(pid, _clip(4.5), SR, "accumulated",
                          added_at=D0 + DAY + 3600)
    clips = store.clips_of(pid)
    assert len(clips) == anchors.KEEP_CLIPS
    assert weakest["file"] not in {c["file"] for c in clips}
    assert weakest["file"] not in _files(store)    # its file is gone too
    assert max(c["score"] for c in clips) == 4.5
    assert _person(store, pid)["refused_last_week"] == 0


def test_a_new_capture_day_gets_in_even_when_it_scores_lower(store):
    pid = _bank(store, "Alex")
    weakest = min(store.clips_of(pid), key=lambda c: c["score"])
    assert store.add_clip(pid, _clip(2.5), SR, "accumulated")     # today
    clips = store.clips_of(pid)
    assert len(clips) == anchors.KEEP_CLIPS
    assert weakest["file"] not in {c["file"] for c in clips}
    assert min(c["score"] for c in clips) == 2.5


def test_one_automatic_clip_a_week(store):
    pid = _bank(store, "Alex")
    assert store.add_clip(pid, _clip(4.5), SR, "accumulated")
    # the allowance is the person's, across sources and length classes
    assert not store.add_clip(pid, _clip(1.5), SR, "harvested-short")
    assert not store.add_clip(pid, _clip(5.5), SR, "cold-start")
    assert not store.add_clip(pid, _clip(6.0), SR, "session")
    assert _person(store, pid)["refused_last_week"] == 3
    _age_allowance(store, pid, 8)
    assert store.add_clip(pid, _clip(6.0), SR, "session")
    assert max(c["score"] for c in store.clips_of(pid)) == 6.0


def test_a_human_backed_clip_always_goes_in(store):
    pid = _bank(store, "Alex")
    assert store.add_clip(pid, _clip(4.5), SR, "accumulated")  # week used
    for source in ("introduction", "correction"):
        before = len(_files(store))
        assert store.add_clip(pid, _clip(2.2 if source == "correction"
                                         else 2.4), SR, source)
        kept = store.clips_of(pid)
        assert any(c["source"] == source for c in kept)
        assert len(kept) == anchors.KEEP_CLIPS
        assert len(_files(store)) == before        # one in, one rotated out
    assert _person(store, pid)["refused_last_week"] == 0


def test_an_automatic_clip_never_displaces_a_protected_one(store):
    pid = _bank(store, "Alex", source="introduction")
    assert _person(store, pid)["settled"] is True
    files = _files(store)
    # better, and from a new day, and still refused: nothing to replace
    assert not store.add_clip(pid, _clip(9.0), SR, "accumulated")
    assert _files(store) == files
    assert {c["source"] for c in store.clips_of(pid)} == {"introduction"}


def test_a_class_with_room_fills_under_the_same_allowance(store):
    pid = _bank(store, "Alex")                     # ten long, no short
    assert store.add_clip(pid, _clip(1.5), SR, "harvested-short")
    assert _person(store, pid)["short_clips"] == 1
    assert not store.add_clip(pid, _clip(1.6), SR, "harvested-short")
    assert _person(store, pid)["short_clips"] == 1
    assert _person(store, pid)["clip_count"] == anchors.KEEP_CLIPS + 1


def test_a_bank_that_isnt_established_learns_as_before(store):
    # ten clips from one day: sufficient, full, and not established
    pid = _bank(store, "Alex", days=(0,))
    assert _person(store, pid)["settled"] is False
    for secs in (5.0, 5.1, 5.2):
        assert store.add_clip(pid, _clip(secs), SR, "accumulated",
                              added_at=D0 + 3600)
    kept = store.clips_of(pid)
    assert len(kept) == anchors.KEEP_CLIPS
    assert {5.0, 5.1, 5.2} <= {c["score"] for c in kept}   # rotation, as ever
    alex = _person(store, pid)
    assert alex["refused_last_week"] == 0 and alex["refusal_reason"] == ""
    assert "settled_clip_at" not in store._load()["people"][pid]
    # a weaker clip still goes to rotation, which drops it
    assert store.add_clip(pid, _clip(2.2), SR, "accumulated",
                          added_at=D0 + 3700)
    assert _person(store, pid)["refused_last_week"] == 0


# ── 2. what "established" means ────────────────────────────────────────────

def test_the_fallback_needs_ten_clips_from_two_days(store):
    assert anchors.SETTLE_MIN_CLIPS == 10 and anchors.SETTLE_MIN_DAYS == 2
    nine = _bank(store, "Alex", n=9)
    assert _person(store, nine)["settled"] is False
    ten = _bank(store, "Sam")
    assert _person(store, ten)["settled"] is True
    # set-aside clips count for nothing
    store.set_hygiene({ten: {store.clips_of(ten)[0]["file"]: "not_speech"}},
                      [])
    assert _person(store, ten)["settled"] is False


def test_the_readiness_verdict_decides_while_the_scorer_runs(store,
                                                             monkeypatch):
    verdicts = {}
    monkeypatch.setattr(anchors, "_readiness_source", verdicts.get)
    few = _bank(store, "Alex", days=(0,), n=3)
    full = _bank(store, "Sam")
    assert _person(store, few)["settled"] is False     # no verdict: fallback
    assert _person(store, full)["settled"] is True
    verdicts.update({few: True, full: False})
    assert _person(store, few)["settled"] is True
    assert _person(store, full)["settled"] is False
    assert store.add_clip(few, _clip(4.0), SR, "accumulated")   # room
    assert not store.add_clip(few, _clip(4.1), SR, "accumulated")
    # a ready verdict never stops a human-backed clip
    assert store.add_clip(few, _clip(4.2), SR, "introduction")
    # not ready: learns as before, however full and varied
    for secs in (5.0, 5.1):
        assert store.add_clip(full, _clip(secs), SR, "accumulated",
                              added_at=D0 + 3600)
    assert _person(store, full)["refused_last_week"] == 0


def test_a_readiness_lookup_that_fails_falls_back(store, monkeypatch):
    def broken(_pid):
        raise RuntimeError("no snapshot")
    monkeypatch.setattr(anchors, "_readiness_source", broken)
    pid = _bank(store, "Alex")
    assert _person(store, pid)["settled"] is True        # the fallback
    assert not store.add_clip(pid, _clip(2.5), SR, "accumulated",
                              added_at=D0 + 3600)


def test_the_calibration_worker_registers_its_verdict(store, monkeypatch):
    assert vc.ready_for("anyone") is None                 # no build yet
    monkeypatch.setattr(vc, "_snapshot", {"readiness": {
        "p-ready": {"ready": True}, "p-not": {"ready": False}}})
    assert vc.ready_for("p-ready") is True
    assert vc.ready_for("p-not") is False
    assert vc.ready_for("p-unknown") is False
    monkeypatch.setattr(vc, "models_state", lambda cfg: "unavailable")
    assert vc.start(ON) is True
    assert anchors._readiness_source is vc.ready_for
    vc.stop()
    assert anchors._readiness_source is None


def test_settle_offer_is_pure():
    """The rule on plain dicts: room, the week, a new day, a better score,
    and protected clips."""
    def c(score, day, source="accumulated", seconds=3.0, **extra):
        return {"file": f"{source}-{score}-{day}", "score": score,
                "seconds": seconds, "source": source,
                "added_at": D0 + day * DAY, **extra}
    full = [c(3.0 + i / 10, i % 2) for i in range(anchors.KEEP_CLIPS)]
    now = D0 + 5 * DAY
    same_day_worse = c(2.0, 1)
    assert anchors.settle_offer(full, same_day_worse, now) == (False, None)
    took, victim = anchors.settle_offer(full, c(9.0, 1), now)
    assert took and victim["score"] == 3.0
    took, victim = anchors.settle_offer(full, c(2.0, 3), now)   # new day
    assert took and victim["score"] == 3.0
    assert anchors.settle_offer(full, c(9.0, 3), now,
                                last_at=now - DAY) == (False, None)
    assert anchors.settle_offer(full, c(9.0, 3), now,
                                last_at=now - 8 * DAY)[0] is True
    assert anchors.settle_offer(full[:9], c(1.0, 1), now) == (True, None)
    guarded = [dict(x, source="introduction") for x in full]
    assert anchors.settle_offer(guarded, c(9.0, 3), now) == (False, None)
    # a moved clip is protected, weakest or not
    moved = [dict(full[0], score=1.0, moved_at=now)] + full[1:]
    took, victim = anchors.settle_offer(moved, c(9.0, 1), now)
    assert took and victim["score"] == 3.1
    # a quarantined clip neither fills a class nor is ever the victim
    aside = full[:9] + [dict(full[9], quarantined=True)]
    assert anchors.settle_offer(aside, c(1.0, 1), now) == (True, None)


# ── 3. drops enter the ledger ──────────────────────────────────────────────

def test_rotation_and_the_settle_rule_send_their_drops_on(store):
    alex = _bank(store, "Alex", days=(0,))         # full, not established
    store.set_membro_slug(alex, alex)
    before = {c["file"]: _sha(store, c["file"]) for c in store.clips_of(alex)}
    assert store.add_clip(alex, _clip(5.0), SR, "accumulated",
                          added_at=D0 + 3600)
    (gone,) = set(before) - {c["file"] for c in store.clips_of(alex)}
    assert _drops(store) == [(alex, before[gone], "rotation")]

    sam = _bank(store, "Sam")                      # full and settled
    store.set_membro_slug(sam, sam)
    weakest = min(store.clips_of(sam), key=lambda c: c["score"])["file"]
    sha = _sha(store, weakest)
    assert store.add_clip(sam, _clip(4.5), SR, "accumulated",
                          added_at=D0 + 3600)
    assert _drops(store)[-1] == (sam, sha, "settled")
    assert all(c["kind"] == "delete" for c in store.pending_corrections())


def test_no_drop_rows_for_a_person_membro_never_held(store):
    pid = _bank(store, "Alex", days=(0,))          # never synced: no slug
    assert store.add_clip(pid, _clip(5.0), SR, "accumulated",
                          added_at=D0 + 3600)
    assert store.pending_corrections() == []


def test_a_clip_moved_in_but_not_yet_synced_is_still_sent_on(store):
    """A pending move into a person with no slug yet names a clip membro
    holds under someone else, so its drop is recorded all the same."""
    sam = store.ensure_person("Sam")
    assert store.add_clip(sam, _clip(2.5), SR, "accumulated")
    store.set_membro_slug(sam, sam)
    alex = _bank(store, "Alex", days=(0,))         # full, no slug
    moved = store.clips_of(sam)[0]["file"]
    sha = _sha(store, moved)
    assert store.move_clip(sam, moved, alex)
    data = store._load()               # the weakest clip of Alex's sitting,
    for c in data["people"][alex]["clips"]:        # and automatic again
        if c["file"] == moved:
            c.pop("moved_at")
            c.update(score=0.1, added_at=D0 + 120)
    store._save(data)
    assert store.add_clip(alex, _clip(5.0), SR, "accumulated",
                          added_at=D0 + 3600)
    assert moved not in {c["file"] for c in store.clips_of(alex)}
    assert sha in [s for _, s, _ in _drops(store)]


def test_a_restore_from_membro_sends_nothing_back(store):
    pid = _bank(store, "Alex", days=(0,))
    store.set_membro_slug(pid, pid)
    assert store.add_clip(pid, _clip(6.0), SR, "accumulated",
                          membro_sha="f" * 64, added_at=D0 + 3600)
    assert len(store.clips_of(pid)) == anchors.KEEP_CLIPS
    assert store.pending_corrections() == []


def test_a_set_aside_clip_is_sent_on_once_and_loses_its_stamp(store):
    pid = store.ensure_person("Alex")
    store.set_membro_slug(pid, pid)
    assert store.add_clip(pid, _clip(2.5), SR, "introduction")
    assert store.add_clip(pid, _clip(2.6), SR, "accumulated",
                          membro_sha="a" * 64)
    pulled = next(c["file"] for c in store.clips_of(pid)
                  if c["source"] == "accumulated")
    assert store.membro_stamps(pid) == {pulled: "a" * 64}
    store.set_hygiene({pid: {pulled: "contaminated"}}, [])
    assert _drops(store) == [(pid, "a" * 64, "set-aside")]
    assert store.membro_stamps(pid) == {}          # the push re-hashes it
    store.set_hygiene({pid: {pulled: "contaminated"}}, [])   # same verdict
    assert len(_drops(store)) == 1
    assert "a" * 64 not in store.kept_shas(pid)
    store.set_hygiene({}, [])                      # reinstated: nothing sent
    assert len(_drops(store)) == 1
    assert _sha(store, pulled) in store.kept_shas(pid)


def test_a_clip_deleted_past_the_quarantine_cap_goes_once(store, monkeypatch):
    monkeypatch.setattr(anchors, "QUARANTINE_MAX", 1)
    pid = store.ensure_person("Alex")
    store.set_membro_slug(pid, pid)
    for secs in (2.5, 2.6):
        assert store.add_clip(pid, _clip(secs), SR, "accumulated")
    files = [c["file"] for c in store.clips_of(pid)]
    store.set_hygiene({pid: {f: "not_speech" for f in files}}, [])
    assert len(_drops(store)) == 2                 # both set aside, once each
    assert len(store.clips_of(pid)) == 1           # one deleted past the cap


def test_a_merge_sends_its_evictions_on_ahead_of_the_merge(store):
    alex = _bank(store, "Alex", days=(0,))         # older: survives
    store.set_membro_slug(alex, alex)
    sam = store.ensure_person("Sam")               # the sitting's weakest
    assert store.add_clip(sam, _clip(2.1), SR, "accumulated",
                          added_at=D0 + 600)
    store.set_membro_slug(sam, sam)
    sam_sha = _sha(store, store.clips_of(sam)[0]["file"])
    assert store.merge_people(alex, sam) == alex
    rows = store.pending_corrections()
    assert [r["kind"] for r in rows] == ["delete", "merge"]
    assert rows[0]["sha"] == sam_sha and rows[0]["why"] == "rotation"
    assert rows[0]["from_slug"] == sam             # still lands in membro


def test_drop_rows_are_bounded_and_the_owners_rows_never_trimmed(
        store, monkeypatch):
    monkeypatch.setattr(anchors, "DROP_ROWS_MAX", 2)
    pid = _bank(store, "Alex", days=(0,))
    store.set_membro_slug(pid, pid)
    owner_file = store.clips_of(pid)[-1]["file"]
    assert store.delete_clip(pid, owner_file)      # the owner's own row
    for i in range(4):
        assert store.add_clip(pid, _clip(6.0 + i / 10), SR, "accumulated",
                              added_at=D0 + 3600 + i)
    rows = store.pending_corrections()
    assert len([r for r in rows if r.get("why")]) == 2
    assert rows[0].get("why") is None and rows[0]["kind"] == "delete"


# ── 4. the manifest ────────────────────────────────────────────────────────

def test_the_manifest_lists_the_active_clips_by_their_address(store):
    pid = store.ensure_person("Alex")
    assert store.kept_shas("nobody") is None
    assert store.kept_shas(pid) == []
    assert store.add_clip(pid, _clip(2.5), SR, "introduction")
    assert store.add_clip(pid, _clip(2.6), SR, "accumulated",
                          membro_sha="b" * 64)
    assert store.add_clip(pid, _clip(2.7), SR, "accumulated")
    intro = next(c["file"] for c in store.clips_of(pid)
                 if c["source"] == "introduction")
    local = _sha(store, intro)
    shas = store.kept_shas(pid)
    assert "b" * 64 in shas and local in shas and len(shas) == 3
    assert shas == sorted(shas)
    third = next(c["file"] for c in store.clips_of(pid)
                 if c["file"] not in store.membro_stamps(pid)
                 and c["source"] == "accumulated")
    store.set_hygiene({pid: [third]}, [])
    assert len(store.kept_shas(pid)) == 2
