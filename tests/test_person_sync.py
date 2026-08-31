"""Crossband's membro person sync (#33 slice 2, design on membro#33).

Against a stateful fake membro (a real local HTTP server speaking the
slice-1 routes), the contract under test:

- the first pass IS the backfill: local people and their clip files are
  pushed (content-addressed - a second pass uploads nothing);
- a person membro holds and we don't is rebuilt locally, clips included;
- a person membro marks forgotten is forgotten here too - the one-press
  forget's step 3;
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
    return create_app(Settings(data_dir=str(tmp_path / "data"),
                               memory_url="http://127.0.0.1:1"))


class FakeMembro:
    """The slice-1 person routes, stateful, on a real local socket."""

    def __init__(self):
        self.persons = {}      # slug -> record
        self.anchors = {}      # slug -> [{id, sha256, source, data}]
        self.requests = []
        self.fail_anchor_lists = False   # 500 every anchors GET when set

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
