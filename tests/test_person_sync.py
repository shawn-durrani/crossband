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


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORY_AUTH_TOKEN", "test-token")
    person_sync._state.update({"last": 0.0, "warned": False})
    return create_app(Settings(data_dir=str(tmp_path / "data"),
                               memory_url="http://127.0.0.1:1"))


def _pcm(seconds=2.0, rate=16000, amp=6000):
    n = int(seconds * rate)
    return struct.pack(f"<{n}h", *([amp, -amp] * (n // 2)))


class FakeMembro:
    """The slice-1 person routes, stateful, on a real local socket."""

    def __init__(self):
        self.persons = {}      # slug -> record
        self.anchors = {}      # slug -> [{id, sha256, source, data}]
        self.requests = []

        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

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
