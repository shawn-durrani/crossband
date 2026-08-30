"""#304 evidence capture: the parked-label breadcrumbs, the per-chat
decision history, their /api/voice/health surfacing, and the one-tap dump.

What these tests pin:

1. Every park/claim/expiry of a parked label leaves a content-free
   breadcrumb - turn id, outcome word, milliseconds waited - so "did a
   stalled /send leave a valid label to expire unclaimed?" is answerable
   after the fact. The label's payload (a person's name) never rides.
2. The decision history keeps the affected turn's decision, with its turn
   id, after later turns overwrite the single freshest record. Bounded.
3. GET /api/voice/health carries both with a chat id, still content-free.
4. The dump endpoint holds every cap: entry count, tag and data lengths,
   malformed entries dropped, bounded file retention. The written file
   bundles the client ring with the server's own correlated state.
"""

import json

import pytest
from fastapi.testclient import TestClient

from backend import db, diarize
from backend.app import create_app
from backend.config import Settings
from backend.routers import voice as voice_router


@pytest.fixture
def app(tmp_path):
    settings = Settings(data_dir=str(tmp_path / "data"),
                        memory_url="http://127.0.0.1:1", user_name="Alex")
    return create_app(settings)


@pytest.fixture(autouse=True)
def _clean_stores():
    diarize._PENDING_LABELS.clear()
    diarize._LABEL_EVENTS.clear()
    diarize._LAST_DECISION.clear()
    diarize._DECISION_HISTORY.clear()
    yield
    diarize._PENDING_LABELS.clear()
    diarize._LABEL_EVENTS.clear()
    diarize._LAST_DECISION.clear()
    diarize._DECISION_HISTORY.clear()


# ── label-flow breadcrumbs ──────────────────────────────────────────────────

def test_park_then_claim_leaves_breadcrumbs_without_the_payload():
    diarize.park_label("turn-a", {"name": "Sam", "kind": "user"})
    assert diarize.claim_label("turn-a") == {"name": "Sam", "kind": "user"}
    flow = diarize.label_flow()
    assert [e["event"] for e in flow] == [diarize.LABEL_CLAIMED,
                                          diarize.LABEL_PARKED]
    assert all(e["turn_id"] == "turn-a" for e in flow)
    assert flow[0]["wait_ms"] >= 0
    assert all(e["age_s"] >= 0 for e in flow)
    # The payload never rides: breadcrumbs are turn ids, outcome words
    # and milliseconds only.
    assert "Sam" not in json.dumps(flow)


def test_unclaimed_label_expires_with_a_breadcrumb(monkeypatch):
    t = [1000.0]
    monkeypatch.setattr(diarize.time, "monotonic", lambda: t[0])
    diarize.park_label("turn-old", {"name": "Sam"})
    t[0] += diarize.PENDING_LABEL_TTL_S + 1
    # The sweep runs on the next park - exactly how expiry happens live.
    diarize.park_label("turn-new", {"name": "Sam"})
    expired = [e for e in diarize.label_flow()
               if e["turn_id"] == "turn-old"
               and e["event"] == diarize.LABEL_EXPIRED_UNCLAIMED]
    assert len(expired) == 1
    assert expired[0]["wait_ms"] == pytest.approx(
        (diarize.PENDING_LABEL_TTL_S + 1) * 1000, abs=1)
    assert diarize.claim_label("turn-old") is None


def test_stale_claim_records_expired_at_claim(monkeypatch):
    t = [1000.0]
    monkeypatch.setattr(diarize.time, "monotonic", lambda: t[0])
    diarize.park_label("turn-a", {"name": "Sam"})
    t[0] += diarize.PENDING_LABEL_TTL_S + 5
    assert diarize.claim_label("turn-a") is None
    assert diarize.label_flow()[0]["event"] == diarize.LABEL_EXPIRED_AT_CLAIM


def test_label_events_stay_bounded():
    for i in range(diarize._LABEL_EVENTS_MAX * 2):
        diarize.park_label(f"turn-{i}", {"x": 1})
    assert len(diarize._LABEL_EVENTS) <= diarize._LABEL_EVENTS_MAX


# ── decision history ────────────────────────────────────────────────────────

def test_decision_history_keeps_the_stalled_turns_decision():
    diarize.record_decision(1, "unresolved", 90.0, "below_threshold",
                            turn_id="turn-stalled")
    diarize.record_decision(1, "local", 50.0, turn_id="turn-later")
    # The single freshest record has moved on ...
    assert diarize.last_decision(1)["path"] == "local"
    # ... but the stalled turn's decision survives, newest first.
    hist = diarize.decision_history(1)
    assert [h["turn_id"] for h in hist] == ["turn-later", "turn-stalled"]
    assert hist[1]["reason"] == "below_threshold"
    assert all(h["age_s"] >= 0 for h in hist)


def test_decision_history_is_bounded_per_chat_and_across_chats():
    for i in range(diarize._DECISION_HISTORY_MAX * 2):
        diarize.record_decision(1, "local", i, turn_id=f"t{i}")
    assert len(diarize.decision_history(1)) == diarize._DECISION_HISTORY_MAX
    for chat in range(2, diarize._DECISION_MAX_CHATS + 3):
        diarize.record_decision(chat, "local", 1.0)
    assert len(diarize._DECISION_HISTORY) <= diarize._DECISION_MAX_CHATS


def test_decision_history_empty_for_unknown_chat():
    assert diarize.decision_history(999) == []


# ── the health surfacing ────────────────────────────────────────────────────

def test_health_surfaces_history_and_label_flow_content_free(app):
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={"participant_ids": []}).json()
        diarize.record_decision(chat["id"], "unresolved", 80.0, "ambiguous",
                                turn_id="turn-x")
        diarize.park_label("turn-x", {"name": "Sam"})
        r = c.get(f"/api/voice/health?chat_id={chat['id']}")
        h = r.json()
        assert h["identity_history"][0]["turn_id"] == "turn-x"
        assert h["identity_history"][0]["reason"] == "ambiguous"
        assert h["label_flow"][0]["event"] == diarize.LABEL_PARKED
        assert "Sam" not in r.text
        # Without a chat id the keys stay absent - same shape as before.
        h = c.get("/api/voice/health").json()
        assert "identity_history" not in h
        assert "label_flow" not in h


# ── the dump endpoint ───────────────────────────────────────────────────────

def test_sanitize_debug_entries_holds_every_cap():
    good = {"t": 12.34, "tag": "state", "data": "x"}
    out = voice_router.sanitize_debug_entries([
        good,
        {"t": "not-a-number", "tag": "state"},   # dropped
        {"t": True, "tag": "state"},             # bool is not a timestamp
        {"t": 1.0, "tag": ""},                   # dropped
        {"t": 1.0},                              # dropped
        "not-a-dict",                            # dropped
        {"t": 2.0, "tag": "x" * 500, "data": "y" * 5000},
        {"t": 3.0, "tag": "no-data", "data": {"nested": "object"}},
    ])
    assert out[0] == {"t": 12.3, "tag": "state", "data": "x"}
    assert len(out) == 3
    assert len(out[1]["tag"]) == voice_router.DEBUG_DUMP_TAG_CHARS
    assert len(out[1]["data"]) == voice_router.DEBUG_DUMP_DATA_CHARS
    assert out[2]["data"] is None  # non-string data never rides


def test_sanitize_debug_entries_keeps_only_the_newest():
    entries = [{"t": float(i), "tag": "e"} for i in range(1000)]
    out = voice_router.sanitize_debug_entries(entries)
    assert len(out) == voice_router.DEBUG_DUMP_MAX_ENTRIES
    assert out[-1]["t"] == 999.0


def test_debug_dump_writes_one_bundled_file(app):
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={"participant_ids": []}).json()
        diarize.record_decision(chat["id"], "local", 42.0, turn_id="turn-y")
        r = c.post("/api/voice/debug-dump", json={
            "chat_id": chat["id"],
            "entries": [{"t": 1.0, "tag": "state", "data": "{}"}],
        }).json()
        assert r["ok"] is True and r["entries"] == 1
        path = db.DATA_DIR / "voice_debug" / r["file"]
        bundle = json.loads(path.read_text())
        assert bundle["chat_id"] == chat["id"]
        assert bundle["client_entries"] == [
            {"t": 1.0, "tag": "state", "data": "{}"}]
        assert bundle["captures"] == []
        assert bundle["identity"]["history"][0]["turn_id"] == "turn-y"
        assert "label_flow" in bundle["identity"]
        assert "trace_summary" in bundle


def test_debug_dump_prunes_old_files(app):
    with TestClient(app, base_url="http://127.0.0.1") as c:
        folder = db.DATA_DIR / "voice_debug"
        folder.mkdir(parents=True, exist_ok=True)
        for i in range(voice_router.DEBUG_DUMP_KEEP_FILES + 5):
            (folder / f"voice_debug_20200101_{i:06d}_aaaaaa.json").write_text("{}")
        r = c.post("/api/voice/debug-dump", json={"entries": []}).json()
        assert r["ok"] is True
        files = list(folder.glob("voice_debug_*.json"))
        assert len(files) == voice_router.DEBUG_DUMP_KEEP_FILES
        # The newest dump - the one just written - survived the prune.
        assert any(f.name == r["file"] for f in files)
