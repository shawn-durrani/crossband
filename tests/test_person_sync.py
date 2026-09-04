"""Crossband's membro person sync (#33 slice 2, design on membro#33).

Against a stateful fake membro (a real local HTTP server speaking the
slice-1 routes), the contract under test:

- the first pass IS the backfill: local people and their clip files are
  pushed (content-addressed - a second pass uploads nothing);
- a person membro holds and we don't is rebuilt locally, clips included;
- a person membro marks forgotten is forgotten here too - the one-press
  forget's step 3;
- a person forgotten HERE is forgotten in membro too (workbench#56): the
  forget rides the correction ledger, and the pull step never rebuilds
  them in the pass that is about to send it;
- a forget settles the corrections still pending on that person (#335):
  a merge they won forgets the loser too, a clip moved into them is
  deleted at its source, and the pull applies membro's forget marks
  before it rebuilds anyone, so a settled merge's loser cannot come back;
- a participant-named entry (#65 guard artefact) is never pushed;
- no token, or membro unreachable, is a clean logged no-op - crossband
  behaves exactly as it did before membro existed.
"""

import base64
import hashlib
import json
import struct
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from backend import anchors, person_sync
from backend.app import create_app
from backend.config import Settings
from backend.diarize import pcm16_wav
from roomkit import _pcm
from tests.conftest import speech_pcm


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORY_AUTH_TOKEN", "test-token")
    person_sync._state.update({"last": 0.0, "warned": False})
    person_sync._restore_offered.clear()
    return create_app(Settings(data_dir=str(tmp_path / "data"),
                               memory_url="http://127.0.0.1:1"))


class FakeMembro:
    """The slice-1 person routes, stateful, on a real local socket."""

    def __init__(self):
        self.persons = {}      # slug -> record
        self.anchors = {}      # slug -> [{id, sha256, source, data}]
        self.requests = []
        self.fail_anchor_lists = False   # 500 every anchors GET when set
        self.fail_forget = False         # 500 every forget POST when set

        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_DELETE(self):
                fake.requests.append(("DELETE", self.path))
                parts = self.path.strip("/").split("/")
                if len(parts) == 5 and parts[3] == "anchors":
                    rows = fake.anchors.get(parts[2], [])
                    row = next((a for a in rows if a["id"] == int(parts[4])),
                               None)
                    if not row:
                        self._json({"error": "no clip"}, 404)
                        return
                    rows.remove(row)
                    self._json({"deleted": True, "file_removed": True})
                else:
                    self._json({"error": "nope"}, 404)

            def _json(self, obj, code=200):
                body = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                fake.requests.append(("GET", self.path))
                parts = self.path.split("?")[0].strip("/").split("/")
                if parts[:2] == ["v1", "persons"] and len(parts) == 2:
                    self._json({"persons": list(fake.persons.values())})
                elif len(parts) == 4 and parts[3] == "anchors":
                    if fake.fail_anchor_lists:
                        self._json({"error": "boom"}, 500)
                        return
                    rows = [{k: a[k] for k in ("id", "sha256", "source")}
                            for a in fake.anchors.get(parts[2], [])]
                    self._json({"anchors": rows})
                elif len(parts) == 6 and parts[5] == "file":
                    for a in fake.anchors.get(parts[2], []):
                        if a["id"] == int(parts[4]):
                            self.send_response(200)
                            self.send_header("Content-Type", "audio/wav")
                            self.end_headers()
                            self.wfile.write(a["data"])
                            return
                    self._json({"error": "no clip"}, 404)
                else:
                    self._json({"error": "nope"}, 404)

            def do_POST(self):
                n = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(n) or b"{}")
                fake.requests.append(("POST", self.path, body))
                parts = self.path.strip("/").split("/")
                if parts == ["v1", "persons"]:
                    slug = body["slug"]
                    fake.persons[slug] = {
                        "slug": slug, "display_name": body["display_name"],
                        "name_owner_set": False, "forgotten_at": None,
                        "updated_at": 100.0, "aliases": [], "clip_count": 0}
                    self._json(fake.persons[slug])
                elif len(parts) == 4 and parts[3] == "anchors":
                    data = base64.b64decode(body["data_b64"])
                    rows = fake.anchors.setdefault(parts[2], [])
                    sha = hashlib.sha256(data).hexdigest()
                    if any(a["sha256"] == sha for a in rows):
                        self._json({"deduped": True})
                        return
                    rows.append({"id": len(rows) + 1, "sha256": sha,
                                 "source": body.get("source", ""),
                                 "data": data})
                    self._json({"deduped": False, "anchor_id": len(rows)})
                elif len(parts) == 6 and parts[5] == "move":
                    rows = fake.anchors.get(parts[2], [])
                    row = next((a for a in rows if a["id"] == int(parts[4])),
                               None)
                    if not row:
                        self._json({"error": "no clip"}, 404)
                        return
                    rows.remove(row)
                    fake.anchors.setdefault(body["to"], []).append(row)
                    self._json({"moved": True, "to": body["to"]})
                elif len(parts) == 4 and parts[3] == "merge":
                    fake.persons[parts[2]]["merged_into"] = body["into"]
                    fake.persons[parts[2]]["updated_at"] = 300.0
                    fake.anchors.setdefault(body["into"], []).extend(
                        fake.anchors.pop(parts[2], []))
                    self._json({"merged": parts[2], "into": body["into"]})
                elif len(parts) == 4 and parts[3] == "forget":
                    person = fake.persons.get(parts[2])
                    if person is None:
                        self._json({"error": "no such person"}, 404)
                        return
                    if person.get("forgotten_at"):
                        self._json({"error": "already forgotten"}, 410)
                        return
                    if fake.fail_forget:
                        self._json({"error": "boom"}, 500)
                        return
                    rows = fake.anchors.get(parts[2], [])
                    n = len(rows)
                    rows.clear()
                    person["forgotten_at"] = 400.0
                    person["updated_at"] = 400.0
                    self._json({"forgotten": parts[2], "clips_deleted": n,
                                "files_removed": n, "facts_held": 0})
                else:
                    self._json({"error": "nope"}, 404)

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever,
                         daemon=True).start()

    def stop(self):
        self.server.shutdown()


@pytest.fixture
def membro():
    fake = FakeMembro()
    yield fake
    fake.stop()


def test_first_pass_is_the_backfill_and_reruns_are_noops(app, membro):
    store = anchors.store()
    pid = store.ensure_person("Alex")
    for _ in range(3):
        assert store.add_clip(pid, _pcm(), 16000, source="introduction")

    out = person_sync.sync_once(membro.url, force=True)
    assert out["pushed_people"] == 1
    assert out["pushed_clips"] == len(store.clips_of(pid))
    assert store.membro_slugs() == {pid: pid}
    assert membro.persons[pid]["display_name"] == "Alex"

    again = person_sync.sync_once(membro.url, force=True)
    assert again["pushed_people"] == 0 and again["pushed_clips"] == 0


def test_a_person_membro_holds_is_rebuilt_locally(app, membro):
    wav = pcm16_wav(_pcm(2.5), 16000)
    membro.persons["p-remote1"] = {
        "slug": "p-remote1", "display_name": "Robin",
        "name_owner_set": True, "forgotten_at": None,
        "updated_at": 50.0, "aliases": [], "clip_count": 1}
    membro.anchors["p-remote1"] = [{
        "id": 1, "sha256": hashlib.sha256(wav).hexdigest(),
        "source": "introduction", "data": wav}]

    out = person_sync.sync_once(membro.url, force=True)
    assert out["pulled_people"] == 1 and out["pulled_clips"] == 1
    store = anchors.store()
    robin = store.find_by_name("Robin")
    assert robin is not None
    assert robin["preferred_name"] == "Robin" and robin["owner_set"]
    assert store.clips_of(robin["person_id"])[0]["source"] == "introduction"
    assert store.get_sync_watermark() == 50.0


def test_a_pulled_clip_is_never_pushed_back_as_a_variant(app, membro):
    """#310: the local copy of a pulled clip is trimmed, so it stops
    hashing to membro's content address. The push diff must not mint a
    variant anchor on every pass, and an owner delete must still reach
    the durable copy - both ride the membro_sha stamp."""
    padded = (b"\x00\x00" * 8000 + _pcm(2.0) + b"\x00\x00" * 32000)
    wav = pcm16_wav(padded, 16000)
    sha = hashlib.sha256(wav).hexdigest()
    membro.persons["p-remote1"] = {
        "slug": "p-remote1", "display_name": "Robin",
        "name_owner_set": False, "forgotten_at": None,
        "updated_at": 50.0, "aliases": [], "clip_count": 1}
    membro.anchors["p-remote1"] = [{"id": 1, "sha256": sha,
                                    "source": "accumulated", "data": wav}]
    out = person_sync.sync_once(membro.url, force=True)
    assert out["pulled_people"] == 1 and out["pulled_clips"] == 1

    again = person_sync.sync_once(membro.url, force=True)
    assert again["pushed_clips"] == 0
    assert [a["sha256"] for a in membro.anchors["p-remote1"]] == [sha]

    store = anchors.store()
    robin = store.find_by_name("Robin")
    fname = store.clips_of(robin["person_id"])[0]["file"]
    assert store.membro_stamps(robin["person_id"]) == {fname: sha}
    assert store.delete_clip(robin["person_id"], fname)
    out = person_sync.sync_once(membro.url, force=True)
    assert out["replayed"] == 1
    assert membro.anchors["p-remote1"] == []


def _file_gets(membro):
    return [r for r in membro.requests
            if r[0] == "GET" and r[1].endswith("/file")]


def test_a_thinned_bank_is_restored_from_membro(app, membro):
    """#311: anchors the durable home holds and this install lost come
    back through the ordinary acceptance path, stamped with membro's own
    address - so a second pass re-uploads nothing and re-downloads
    nothing."""
    store = anchors.store()
    pid = store.ensure_person("Alex")
    assert store.add_clip(pid, _pcm(2.0), 16000, source="introduction")
    person_sync.sync_once(membro.url, force=True)   # map + push the one clip

    # history rotation left behind: two anchors only membro still holds
    for secs in (2.2, 2.4):
        wav = pcm16_wav(_pcm(secs), 16000)
        rows = membro.anchors[pid]
        rows.append({"id": len(rows) + 1,
                     "sha256": hashlib.sha256(wav).hexdigest(),
                     "source": "accumulated", "data": wav})

    out = person_sync.sync_once(membro.url, force=True)
    assert out["restored_clips"] == 2
    assert len(store.clips_of(pid)) == 3
    assert len(store.membro_stamps(pid)) == 2

    again = person_sync.sync_once(membro.url, force=True)
    assert again["restored_clips"] == 0 and again["pushed_clips"] == 0
    assert len(membro.anchors[pid]) == 3            # no variant anchors


def test_restore_leaves_a_full_sufficient_bank_alone(app, membro):
    """#311: a bank at capacity and past the sufficiency bar is already
    best-of; the archive is not downloaded at it."""
    store = anchors.store()
    pid = store.ensure_person("Alex")
    for secs in (2.1, 2.2, 2.3, 2.4, 2.5, 1.1, 1.2, 1.3):
        assert store.add_clip(pid, _pcm(secs), 16000, source="introduction")
    person_sync.sync_once(membro.url, force=True)

    wav = pcm16_wav(_pcm(3.5), 16000)
    membro.anchors[pid].append({"id": 99,
                                "sha256": hashlib.sha256(wav).hexdigest(),
                                "source": "accumulated", "data": wav})
    before = len(_file_gets(membro))
    out = person_sync.sync_once(membro.url, force=True)
    assert out["restored_clips"] == 0
    assert len(_file_gets(membro)) == before        # not even downloaded


def test_a_refused_anchor_is_not_downloaded_again(app, membro):
    """#311: acceptance is the keep policy's call, but a refused anchor
    must not be re-fetched every pass - the offer is remembered."""
    import random
    import struct as _struct
    store = anchors.store()
    pid = store.ensure_person("Alex")
    assert store.add_clip(pid, _pcm(2.0), 16000, source="introduction")
    person_sync.sync_once(membro.url, force=True)

    rng = random.Random(9)
    noise = b"".join(_struct.pack("<h", rng.randint(-12000, 12000))
                     for _ in range(2 * 16000))
    wav = pcm16_wav(noise, 16000)
    membro.anchors[pid].append({"id": 7,
                                "sha256": hashlib.sha256(wav).hexdigest(),
                                "source": "accumulated", "data": wav})

    out = person_sync.sync_once(membro.url, force=True)
    fetched = len(_file_gets(membro))
    assert out["restored_clips"] == 0               # the gate refused it
    assert fetched >= 1

    again = person_sync.sync_once(membro.url, force=True)
    assert again["restored_clips"] == 0
    assert len(_file_gets(membro)) == fetched       # remembered, not refetched


def test_a_forgotten_person_is_forgotten_here_too(app, membro):
    store = anchors.store()
    pid = store.ensure_person("Sam")
    assert store.add_clip(pid, _pcm(), 16000, source="introduction")
    person_sync.sync_once(membro.url, force=True)
    assert pid in store.membro_slugs()

    membro.persons[pid]["forgotten_at"] = 200.0
    membro.persons[pid]["updated_at"] = 200.0
    out = person_sync.sync_once(membro.url, force=True)
    assert out["forgotten"] == 1
    assert store.find_by_name("Sam") is None          # audio and entry gone
    assert store.people() == []


def test_a_local_forget_reaches_membro_and_cannot_resurrect(app, membro):
    """workbench#56: the owner's Forget here must be membro's forget too,
    or the both-apps promise in the explainer is false. Two passes on
    purpose: a pull that rebuilds the person from membro's living copy
    in the same pass that sends the forget would pass a one-pass test
    and still bring the audio back on the next one."""
    store = anchors.store()
    pid = store.ensure_person("Alex")
    assert store.add_clip(pid, _pcm(2.0), 16000, source="introduction")
    assert store.add_clip(pid, _pcm(2.5), 16000, source="accumulated")
    person_sync.sync_once(membro.url, force=True)
    assert len(membro.anchors[pid]) == 2

    assert store.forget(pid)
    assert [c["kind"] for c in store.pending_corrections()] == ["forget"]
    assert store.pending_corrections()[0]["slug"] == pid

    out = person_sync.sync_once(membro.url, force=True)
    assert out["replayed"] == 1 and store.pending_corrections() == []
    assert out["pulled_people"] == 0                # not rebuilt on the way
    assert membro.persons[pid]["forgotten_at"]
    assert membro.anchors[pid] == []                # audio gone there too
    assert store.people() == []

    again = person_sync.sync_once(membro.url, force=True)
    assert again["pulled_people"] == 0 and again["restored_clips"] == 0
    assert store.people() == []                     # and it stays gone
    forgets = [r for r in membro.requests
               if r[0] == "POST" and r[1].endswith("/forget")]
    assert len(forgets) == 1                        # sent once, not retried


def test_a_forget_membro_already_made_is_convergence(app, membro):
    """410 from membro means it already forgot them: consume, never retry."""
    store = anchors.store()
    pid = store.ensure_person("Alex")
    assert store.add_clip(pid, _pcm(), 16000, source="introduction")
    person_sync.sync_once(membro.url, force=True)

    assert store.forget(pid)
    membro.persons[pid]["forgotten_at"] = 200.0    # forgotten there first
    membro.persons[pid]["updated_at"] = 200.0
    out = person_sync.sync_once(membro.url, force=True)
    assert out["replayed"] == 1 and store.pending_corrections() == []
    assert store.people() == []


def test_a_failed_forget_stays_pending_and_never_resurrects(app, membro):
    """A 500 (or a stale token's 401) is not convergence: the forget waits
    for the next pass, and the pull guard holds while it waits."""
    store = anchors.store()
    pid = store.ensure_person("Alex")
    assert store.add_clip(pid, _pcm(), 16000, source="introduction")
    person_sync.sync_once(membro.url, force=True)
    assert store.forget(pid)

    membro.fail_forget = True
    out = person_sync.sync_once(membro.url, force=True)
    assert out["replayed"] == 0
    assert len(store.pending_corrections()) == 1    # kept, not eaten
    assert store.people() == []                     # not rebuilt meanwhile
    assert not membro.persons[pid]["forgotten_at"]

    membro.fail_forget = False
    out = person_sync.sync_once(membro.url, force=True)
    assert out["replayed"] == 1 and store.pending_corrections() == []
    assert membro.persons[pid]["forgotten_at"]
    assert membro.anchors[pid] == []


def test_forgetting_a_person_membro_never_knew_records_nothing(app):
    """No membro slug means nothing durable to fix: the ledger stays empty
    and the forget is exactly the local delete it always was."""
    store = anchors.store()
    pid = store.ensure_person("Alex")
    assert store.add_clip(pid, _pcm(), 16000, source="introduction")
    assert store.forget(pid)
    assert store.pending_corrections() == []
    assert store.people() == []


def test_forgetting_a_merge_winner_cannot_bring_the_loser_back(app, membro):
    """#335, the worst case: a merge whose winner is then forgotten. The
    merge row named the winner by a local id that no longer resolved, so
    it stayed pending forever, and the loser, still a living person in
    membro, was rebuilt here under the other name, audio and all. The
    owner forgot a human and the human came back. Two passes on purpose,
    as for the plain forget."""
    store = anchors.store()
    sam = store.ensure_person("Sam")          # older: merge_people keeps it
    sammy = store.ensure_person("Sammy")
    assert store.add_clip(sam, _pcm(2.0), 16000, source="introduction")
    assert store.add_clip(sammy, _pcm(2.5), 16000, source="accumulated")
    person_sync.sync_once(membro.url, force=True)
    assert len(membro.anchors[sam]) == 1 and len(membro.anchors[sammy]) == 1

    assert store.merge_people(sam, sammy) == sam
    assert store.forget(sam)
    assert [(c["kind"], c["slug"]) for c in store.pending_corrections()] == [
        ("forget", sammy), ("forget", sam)]

    out = person_sync.sync_once(membro.url, force=True)
    assert out["replayed"] == 2 and store.pending_corrections() == []
    assert out["pulled_people"] == 0                # nobody rebuilt on the way
    for slug in (sam, sammy):
        assert membro.persons[slug]["forgotten_at"]
        assert membro.anchors[slug] == []           # audio gone there too
    assert store.people() == []

    again = person_sync.sync_once(membro.url, force=True)
    assert again["pulled_people"] == 0 and again["restored_clips"] == 0
    assert store.people() == []                     # and the loser stays gone


def test_a_forget_membro_made_settles_a_pending_merge_before_any_rebuild(
        app, membro):
    """The membro-first half, older than the local forget: membro forgets
    the winner of a merge that has not landed yet. Mirroring that forget
    here settles the merge into a forget of the loser, and the pull must
    see that row before it rebuilds anyone. A guard computed before the
    mirror pulled the loser back, audio and all, for one pass."""
    store = anchors.store()
    sam = store.ensure_person("Sam")
    sammy = store.ensure_person("Sammy")
    assert store.add_clip(sam, _pcm(2.0), 16000, source="introduction")
    assert store.add_clip(sammy, _pcm(2.5), 16000, source="accumulated")
    person_sync.sync_once(membro.url, force=True)
    assert store.merge_people(sam, sammy) == sam
    assert [c["kind"] for c in store.pending_corrections()] == ["merge"]

    membro.persons[sam]["forgotten_at"] = 200.0     # forgotten there first
    membro.persons[sam]["updated_at"] = 200.0
    membro.anchors[sam] = []
    out = person_sync.sync_once(membro.url, force=True)
    assert out["forgotten"] == 1                    # the mirror
    assert out["pulled_people"] == 0                # the loser never rebuilt
    assert out["replayed"] == 1 and store.pending_corrections() == []
    assert membro.persons[sammy]["forgotten_at"]
    assert membro.anchors[sammy] == []
    assert store.people() == []
    forgets = [r[1] for r in membro.requests
               if r[0] == "POST" and r[1].endswith("/forget")]
    assert forgets == [f"/v1/persons/{sammy}/forget"]   # nothing back for Sam

    again = person_sync.sync_once(membro.url, force=True)
    assert again["pulled_people"] == 0 and store.people() == []


def test_forgetting_a_person_deletes_the_clip_moved_into_them_in_membro(
        app, membro):
    """A clip the owner moved into a person who is then forgotten is that
    person's audio. The pending move becomes a delete at its source, so
    membro's copy goes too instead of staying under the person it was
    moved from, where a restore would hand it straight back."""
    store = anchors.store()
    blair = store.ensure_person("Blair")
    casey = store.ensure_person("Casey")
    assert store.add_clip(blair, _pcm(2.0), 16000, source="introduction")
    assert store.add_clip(blair, _pcm(2.5), 16000, source="accumulated")
    person_sync.sync_once(membro.url, force=True)
    assert len(membro.anchors[blair]) == 2

    moved = store.clips_of(blair)[0]["file"]
    moved_sha = hashlib.sha256(
        (store.root / moved).read_bytes()).hexdigest()
    assert store.move_clip(blair, moved, casey)
    assert store.forget(casey)
    assert [c["kind"] for c in store.pending_corrections()] == [
        "delete", "forget"]

    out = person_sync.sync_once(membro.url, force=True)
    assert out["replayed"] == 2 and store.pending_corrections() == []
    assert moved_sha not in {x["sha256"] for x in membro.anchors[blair]}
    assert len(membro.anchors[blair]) == 1          # the other clip untouched
    assert membro.persons[casey]["forgotten_at"]
    assert membro.anchors.get(casey, []) == []

    again = person_sync.sync_once(membro.url, force=True)
    assert again["pulled_people"] == 0 and again["restored_clips"] == 0
    assert [p["person_id"] for p in store.people()] == [blair]
    assert moved_sha not in {x["sha256"] for x in membro.anchors[blair]}


def test_a_participant_named_entry_is_never_pushed(app, membro):
    store = anchors.store()
    pid = store.ensure_person("Claude")               # the #65 shape
    store.add_clip(pid, _pcm(), 16000, source="accumulated")
    out = person_sync.sync_once(membro.url, force=True)
    assert out["pushed_people"] == 0
    assert "Claude" not in {p.get("display_name")
                            for p in membro.persons.values()}
    assert pid not in store.membro_slugs()


def test_no_token_or_dead_membro_is_a_clean_noop(app, membro, monkeypatch):
    monkeypatch.delenv("MEMORY_AUTH_TOKEN")
    assert person_sync.sync_once(membro.url,
                                 force=True)["skipped"] == "no token"
    monkeypatch.setenv("MEMORY_AUTH_TOKEN", "test-token")
    out = person_sync.sync_once("http://127.0.0.1:1", force=True)
    assert out["skipped"].startswith("unreachable")


def test_a_local_move_is_replayed_and_cannot_resurrect(app, membro):
    store = anchors.store()
    a = store.ensure_person("Blair")
    b = store.ensure_person("Casey")
    # two DIFFERENT clips (identical bytes would content-address to one)
    assert store.add_clip(a, _pcm(2.0), 16000, source="introduction")
    assert store.add_clip(a, _pcm(2.5), 16000, source="accumulated")
    person_sync.sync_once(membro.url, force=True)
    assert len(membro.anchors[a]) == 2

    moved = store.clips_of(a)[0]["file"]
    moved_sha = hashlib.sha256(
        (store.root / moved).read_bytes()).hexdigest()
    assert store.move_clip(a, moved, b)
    out = person_sync.sync_once(membro.url, force=True)
    assert out["replayed"] == 1
    assert store.pending_corrections() == []
    # membro reflects the correction: the clip now lives under Casey
    assert moved_sha not in {x["sha256"] for x in membro.anchors[a]}
    assert moved_sha in {x["sha256"] for x in membro.anchors.get(b, [])}


def test_a_local_delete_is_replayed(app, membro):
    store = anchors.store()
    a = store.ensure_person("Blair")
    assert store.add_clip(a, _pcm(), 16000, source="introduction")
    person_sync.sync_once(membro.url, force=True)
    gone = store.clips_of(a)[0]["file"]
    gone_sha = hashlib.sha256((store.root / gone).read_bytes()).hexdigest()
    assert store.delete_clip(a, gone)
    out = person_sync.sync_once(membro.url, force=True)
    assert out["replayed"] == 1 and store.pending_corrections() == []
    assert gone_sha not in {x["sha256"] for x in membro.anchors[a]}
    # and the push step does NOT re-upload what the owner deleted
    assert membro.anchors[a] == []


def test_a_local_merge_is_replayed(app, membro):
    store = anchors.store()
    a = store.ensure_person("Sam")
    store.add_clip(a, _pcm(), 16000, source="introduction")
    b = store.ensure_person("Sammy")
    store.add_clip(b, _pcm(1.5), 16000, source="accumulated")
    person_sync.sync_once(membro.url, force=True)

    survivor = store.merge_people(a, b)
    gone_slug = b if survivor == a else a
    out = person_sync.sync_once(membro.url, force=True)
    assert out["replayed"] == 1 and store.pending_corrections() == []
    assert membro.persons[gone_slug]["merged_into"] == survivor


def test_an_unreadable_anchor_list_keeps_the_correction_pending(app, membro):
    """A 401/500 on the clip list is NOT convergence. _find_anchor used to
    return None for any non-200, and the replay consumed that None as
    "already converged" - so a stale token or a flaky 500 permanently ate
    the owner's delete, the exact silent drop the replay promises never to
    make. An unreadable list now leaves the correction pending and the next
    pass retries it."""
    store = anchors.store()
    a = store.ensure_person("Blair")
    assert store.add_clip(a, _pcm(), 16000, source="introduction")
    person_sync.sync_once(membro.url, force=True)
    gone = store.clips_of(a)[0]["file"]
    gone_sha = hashlib.sha256((store.root / gone).read_bytes()).hexdigest()
    assert store.delete_clip(a, gone)

    membro.fail_anchor_lists = True
    person_sync.sync_once(membro.url, force=True)
    assert len(store.pending_corrections()) == 1   # kept, not eaten

    membro.fail_anchor_lists = False
    out = person_sync.sync_once(membro.url, force=True)
    assert out["replayed"] == 1 and store.pending_corrections() == []
    assert gone_sha not in {x["sha256"] for x in membro.anchors[a]}


def test_watermark_advances_to_the_newest_remote_change(app, membro):
    """The delta pull is real: the recorded watermark moves to the newest
    updated_at membro reported, so the next pass asks membro only for what
    changed since. (Membro-side: /v1/persons must project updated_at -
    without it every pull re-reads everyone forever.)"""
    store = anchors.store()
    membro.persons["p-remote1"] = {
        "slug": "p-remote1", "display_name": "Robin",
        "name_owner_set": False, "forgotten_at": None,
        "updated_at": 250.0, "aliases": [], "clip_count": 0}
    assert store.get_sync_watermark() == 0
    person_sync.sync_once(membro.url, force=True)
    assert store.get_sync_watermark() == 250.0
    person_sync.sync_once(membro.url, force=True)
    since = [path for verb, *rest in membro.requests
             for path in rest[:1]
             if verb == "GET" and path.startswith("/v1/persons?")][-1]
    assert "since=250.0" in since


def test_corrections_survive_membro_being_down(app, membro):
    store = anchors.store()
    a = store.ensure_person("Blair")
    b = store.ensure_person("Casey")
    assert store.add_clip(a, _pcm(), 16000, source="introduction")
    person_sync.sync_once(membro.url, force=True)
    assert store.move_clip(a, store.clips_of(a)[0]["file"], b)
    # membro unreachable: the correction stays pending
    person_sync.sync_once("http://127.0.0.1:1", force=True)
    assert len(store.pending_corrections()) == 1
    # membro back: it lands and clears
    out = person_sync.sync_once(membro.url, force=True)
    assert out["replayed"] == 1 and store.pending_corrections() == []
