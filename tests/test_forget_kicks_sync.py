"""Forget kicks the person sync itself (#334).

Forget on the Voices page deletes a person's audio here at once, and
since workbench#56 the forget rides the correction ledger to membro. The
pass that carries it used to run only after a round, at most every two
minutes, or at startup, so for a round or two the explainer's both-apps
promise was true here only. The route now kicks one forced pass on a
worker thread and answers without waiting for it. Pinned here:

- `kick` runs a forced pass on its own thread and never raises;
- DELETE /api/voice/people/{id} answers before the pass has run, and
  once it has, membro shows the person forgotten with no audio - with
  no round and no second sync call;
- with membro unreachable the route answers ok, and 404s, exactly as
  before: the kick can neither block nor fail the response, and the
  forget stays in the ledger for the next pass.
"""

import logging
import threading

import pytest
from fastapi.testclient import TestClient

from backend import anchors, person_sync
from backend.app import create_app
from backend.config import Settings
from roomkit import _pcm
from tests.test_person_sync import FakeMembro

CLOSED_PORT = "http://127.0.0.1:1"


@pytest.fixture
def membro():
    fake = FakeMembro()
    yield fake
    fake.stop()


def _app(tmp_path, monkeypatch, memory_url):
    """test_person_sync's `app` fixture, pointed where each test needs it:
    the route reads memory_url from the app's settings."""
    monkeypatch.setenv("MEMORY_AUTH_TOKEN", "test-token")
    person_sync._state.update({"last": 0.0, "warned": False})
    person_sync._restore_offered.clear()
    return create_app(Settings(data_dir=str(tmp_path / "data"),
                               memory_url=memory_url))


@pytest.fixture
def app(tmp_path, monkeypatch, membro):
    return _app(tmp_path, monkeypatch, membro.url)


@pytest.fixture
def dead_app(tmp_path, monkeypatch):
    return _app(tmp_path, monkeypatch, CLOSED_PORT)


@pytest.fixture
def kicks(monkeypatch):
    """Every thread the route kicks, so a test can join it: the real kick,
    observed. Teardown joins whatever is left so no pass outlives its
    test's data directory."""
    threads = []
    real = person_sync.kick

    def capturing(memory_url):
        t = real(memory_url)
        threads.append(t)
        return t

    monkeypatch.setattr(person_sync, "kick", capturing)
    yield threads
    for t in threads:
        t.join(10)
    assert not any(t.is_alive() for t in threads)


def test_kick_runs_one_forced_pass_on_its_own_thread(monkeypatch):
    calls = []

    def recorder(memory_url, force=False):
        calls.append((memory_url, force, threading.current_thread()))
        return {"skipped": "recorded"}

    monkeypatch.setattr(person_sync, "sync_once", recorder)
    t = person_sync.kick("http://membro.test")
    assert t.daemon                          # never holds the process open
    t.join(10)
    assert not t.is_alive()
    assert calls == [("http://membro.test", True, t)]
    assert t is not threading.current_thread()


def test_kick_logs_a_failed_pass_and_never_raises(monkeypatch, caplog):
    def boom(memory_url, force=False):
        raise RuntimeError("membro exploded")

    monkeypatch.setattr(person_sync, "sync_once", boom)
    with caplog.at_level(logging.ERROR, logger="crossband.person_sync"):
        t = person_sync.kick("http://membro.test")
        t.join(10)
    assert not t.is_alive()
    assert any("kicked person sync failed" in r.getMessage()
               for r in caplog.records)


def test_forget_route_sends_the_forget_to_membro_at_once(
        app, membro, kicks, monkeypatch):
    """The whole path: a person membro holds is forgotten through the
    route, and membro has forgotten them too once the kicked pass has
    run - no round, no second sync call. The kicked pass is held at a
    gate until the test opens it, which proves the response came back
    before the pass ran rather than after it."""
    store = anchors.store()
    pid = store.ensure_person("Alex")
    assert store.add_clip(pid, _pcm(2.0), 16000, source="introduction")
    assert store.add_clip(pid, _pcm(2.5), 16000, source="accumulated")
    person_sync.sync_once(membro.url, force=True)       # the one push
    assert len(membro.anchors[pid]) == 2

    gate = threading.Event()
    real_sync = person_sync.sync_once
    passes = []

    def gated(memory_url, force=False):
        gate.wait(10)
        passes.append((memory_url, force))
        return real_sync(memory_url, force=force)

    monkeypatch.setattr(person_sync, "sync_once", gated)

    c = TestClient(app, base_url="http://127.0.0.1")
    assert c.delete(f"/api/voice/people/{pid}").json() == {"ok": True}
    assert store.people() == []                         # gone here already
    assert [t.is_alive() for t in kicks] == [True]      # the pass: on its way
    assert passes == []                                 # and not yet run
    assert not membro.persons[pid]["forgotten_at"]

    gate.set()
    kicks[0].join(10)
    assert passes == [(membro.url, True)]               # forced, exactly once
    assert membro.persons[pid]["forgotten_at"]
    assert membro.anchors[pid] == []                    # audio gone there too
    assert store.pending_corrections() == []            # landed, not left over
    forgets = [r for r in membro.requests
               if r[0] == "POST" and r[1].endswith("/forget")]
    assert len(forgets) == 1


def test_forget_route_answers_as_before_when_membro_is_unreachable(
        dead_app, kicks):
    """Membro at a closed port: the kicked pass is the logged no-op it
    always was, the route answers ok, the 404 is unchanged, and the
    forget waits in the ledger for a pass that can land it."""
    store = anchors.store()
    pid = store.ensure_person("Alex")
    assert store.add_clip(pid, _pcm(2.0), 16000, source="introduction")
    store.set_membro_slug(pid, pid)  # membro held them: a forget is recorded

    c = TestClient(dead_app, base_url="http://127.0.0.1")
    assert c.delete(f"/api/voice/people/{pid}").json() == {"ok": True}
    assert store.people() == []
    assert len(kicks) == 1
    kicks[0].join(10)
    assert not kicks[0].is_alive()
    assert [k["kind"] for k in store.pending_corrections()] == ["forget"]

    assert c.delete(f"/api/voice/people/{pid}").status_code == 404
    assert len(kicks) == 1              # nothing forgotten, nothing kicked
