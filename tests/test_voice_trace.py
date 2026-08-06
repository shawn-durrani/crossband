"""Per-turn voice latency instrumentation.

Guardrails pinned here:
  - the PRIVACY FLOOR: sanitize accepts only allowlisted stage names + numeric
    durations + bounded labels, and NEVER any free-text/transcript field;
  - aggregation math (p50/p95, per-model / per-TTS-provider segmentation);
  - the ingest + summary HTTP endpoints, incl. that a hostile payload trying to
    smuggle content is dropped, not stored;
  - retention pruning bounds the table.
"""
import pytest

from backend import db, voice_trace
from backend.app import create_app
from backend.config import Settings


# ---------- sanitize (the privacy + integrity gate) ----------

def test_sanitize_stage_accepts_allowlisted_numeric():
    out = voice_trace.sanitize_stage(
        {"stage": "final_to_first_token", "ms": 812.5,
         "provider": "openai", "model": "gpt-5.1"})
    assert out["stage"] == "final_to_first_token"
    assert out["ms"] == 812.5
    assert out["provider"] == "openai" and out["model"] == "gpt-5.1"


def test_sanitize_stage_drops_unknown_stage():
    assert voice_trace.sanitize_stage({"stage": "transcript_text", "ms": 1}) is None


@pytest.mark.parametrize("dead", [
    "final_to_dispatch", "dispatch_to_first_token", "stt",
    "tts_request_to_first_audio",
])
def test_sanitize_drops_stages_we_no_longer_emit(dead):
    """Renamed/removed stages must not sneak back into the table — the summary
    endpoint would otherwise advertise columns the client never fills."""
    assert dead not in voice_trace.ALLOWED_STAGES
    assert voice_trace.sanitize_stage({"stage": dead, "ms": 100}) is None


@pytest.mark.parametrize("ms", [-1, float("nan"), 10**9, "5", True, None])
def test_sanitize_stage_rejects_bad_durations(ms):
    assert voice_trace.sanitize_stage({"stage": "end_of_speech_to_final", "ms": ms}) is None


def test_sanitize_never_persists_free_text_fields():
    """A caller can't smuggle a transcript in: unknown keys are ignored and the
    output dict has ONLY the fixed content-free schema."""
    out = voice_trace.sanitize_stage(
        {"stage": "end_of_speech_to_final", "ms": 100, "transcript": "my bank pin is 1234",
         "text": "secret", "model": "x"})
    assert set(out) == {"stage", "ms", "provider", "model", "tts_provider", "speaker"}
    assert "1234" not in str(out) and "secret" not in str(out)


def test_sanitize_bounds_identifier_length():
    out = voice_trace.sanitize_stage(
        {"stage": "end_of_speech_to_final", "ms": 1, "model": "m" * 500})
    assert len(out["model"]) <= 64


def test_sanitize_turn_requires_turn_id():
    tid, cid, stages = voice_trace.sanitize_turn(
        {"stages": [{"stage": "end_of_speech_to_final", "ms": 1}]})
    assert tid is None and stages == []


def test_sanitize_turn_keeps_good_drops_bad():
    tid, cid, stages = voice_trace.sanitize_turn({
        "turn_id": "abc", "chat_id": 7,
        "stages": [
            {"stage": "end_of_speech_to_final", "ms": 120},
            {"stage": "not_a_stage", "ms": 5},      # dropped
            {"stage": "end_to_end_first_audio", "ms": 2200},
        ],
    })
    assert tid == "abc" and cid == 7
    assert [s["stage"] for s in stages] == ["end_of_speech_to_final", "end_to_end_first_audio"]


# ---------- aggregation ----------

def _row(stage, ms, model="", tts=""):
    return {"turn_id": "t", "stage": stage, "ms": ms, "model": model, "tts_provider": tts}


def test_aggregate_percentiles_and_segments():
    rows = [_row("final_to_first_token", v, model="gpt-5.1")
            for v in (100, 200, 300, 400, 500)]
    rows += [_row("final_to_first_token", v, model="claude-opus-4-8")
             for v in (600, 700)]
    rows += [_row("first_token_to_first_audio", 50, tts="elevenlabs")]
    agg = voice_trace.aggregate(rows)
    disp = agg["stages"]["final_to_first_token"]
    assert disp["count"] == 7
    assert disp["p50"] == 400  # median of the 7 values
    assert set(disp["by_model"]) == {"gpt-5.1", "claude-opus-4-8"}
    assert disp["by_model"]["gpt-5.1"]["count"] == 5
    assert agg["stages"]["first_token_to_first_audio"]["by_tts_provider"]["elevenlabs"]["count"] == 1


def test_aggregate_segments_multi_speaker_queue_wait():
    """A 3-model round posts a queue_wait per follower; the summary aggregates
    them per model, answering the multi-agent-serialisation question."""
    rows = [_row("playback_queue_wait", 2500, model="claude-opus-4-8"),
            _row("playback_queue_wait", 1800, model="gpt-5.1"),
            _row("playback_queue_wait", 3200, model="claude-opus-4-8")]
    qw = voice_trace.aggregate(rows)["stages"]["playback_queue_wait"]
    assert qw["count"] == 3
    assert qw["by_model"]["claude-opus-4-8"]["count"] == 2


def test_aggregate_counts_distinct_turns():
    rows = [{"turn_id": "a", "stage": "end_of_speech_to_final", "ms": 1},
            {"turn_id": "a", "stage": "end_of_speech_to_final", "ms": 2},
            {"turn_id": "b", "stage": "end_of_speech_to_final", "ms": 3}]
    assert voice_trace.aggregate(rows)["turns"] == 2


def test_percentile_single_sample_is_honest():
    assert voice_trace._percentile([42.0], 95) == 42.0


# ---------- DB persistence + retention ----------

@pytest.fixture
def app(tmp_path):
    return create_app(Settings(data_dir=str(tmp_path / "data"),
                               memory_url="http://127.0.0.1:1"))


def test_insert_and_query_roundtrip(app):
    con = db.connect()
    try:
        con.execute("INSERT INTO chats(title, created_at, updated_at) VALUES('t',0,0)")
        cid = con.execute("SELECT id FROM chats").fetchone()["id"]
        db.insert_voice_trace(con, "turn1", cid, "end_of_speech_to_final", 130.0,
                              model="gpt-5.1")
        con.commit()
        rows = db.get_voice_traces(con)
        assert len(rows) == 1
        assert rows[0]["stage"] == "end_of_speech_to_final" and rows[0]["ms"] == 130.0
    finally:
        con.close()


def test_record_server_stage_writes_recognized_stage(app):
    """Server-measured stages go straight through record_server_stage
    (no client-input sanitize gate — this IS the trusted server path), but
    are still checked against SERVER_STAGES so a typo/refactor can't write an
    unrecognized stage name."""
    con = db.connect()
    try:
        con.execute("INSERT INTO chats(title, created_at, updated_at) VALUES('t',0,0)")
        cid = con.execute("SELECT id FROM chats").fetchone()["id"]
        voice_trace.record_server_stage(con, "turnS", cid, "server_context_assembly",
                                        123.4, provider="anthropic", model="claude-opus-4-8")
        con.commit()
        rows = db.get_voice_traces(con)
        assert len(rows) == 1
        assert rows[0]["stage"] == "server_context_assembly"
        assert rows[0]["ms"] == 123.4
    finally:
        con.close()


def test_record_server_stage_drops_unrecognized_stage_and_bad_values(app):
    con = db.connect()
    try:
        voice_trace.record_server_stage(con, "turnBad", None, "not_a_real_stage", 10)
        voice_trace.record_server_stage(con, "", None, "server_context_assembly", 10)
        voice_trace.record_server_stage(con, "turnBad", None, "server_context_assembly", -5)
        voice_trace.record_server_stage(con, "turnBad", None, "server_context_assembly", float("nan"))
        con.commit()
        assert db.get_voice_traces(con) == []
    finally:
        con.close()


def test_server_stages_are_never_client_ingestible():
    """The public POST /api/voice/trace path must keep rejecting server-only
    stage names — a client can't fabricate server-side attribution just
    because the name is now recognized by aggregate()."""
    for stage in voice_trace.SERVER_STAGES:
        assert voice_trace.sanitize_stage({"stage": stage, "ms": 1}) is None
        assert stage not in voice_trace.ALLOWED_STAGES
        assert stage in voice_trace.ALL_STAGES  # but aggregate() still reports it


def test_aggregate_includes_server_measured_stages():
    rows = [{"turn_id": "t", "stage": "server_context_assembly", "ms": 300, "model": "claude-opus-4-8"},
            {"turn_id": "t", "stage": "server_provider_first_token", "ms": 4100, "model": "claude-opus-4-8"}]
    agg = voice_trace.aggregate(rows)
    assert agg["stages"]["server_context_assembly"]["p50"] == 300
    assert agg["stages"]["server_provider_first_token"]["p50"] == 4100


def test_prune_caps_by_count(app):
    con = db.connect()
    try:
        for i in range(20):
            db.insert_voice_trace(con, f"t{i}", None, "end_of_speech_to_final", float(i))
        con.commit()
        db.prune_voice_traces(con, keep_days=30, keep_max=5)
        con.commit()
        assert len(db.get_voice_traces(con)) == 5
    finally:
        con.close()


# ---------- HTTP endpoints ----------

def test_trace_ingest_and_summary_endpoint(app):
    from fastapi.testclient import TestClient
    with TestClient(app, base_url="http://127.0.0.1") as c:
        r = c.post("/api/voice/trace", json={
            "turn_id": "turnX", "chat_id": None,
            "stages": [
                {"stage": "end_to_end_first_audio", "ms": 1800,
                 "model": "gpt-5.1", "tts_provider": "elevenlabs"},
                {"stage": "bogus", "ms": 1, "transcript": "leak me"},  # dropped
            ],
        })
        assert r.status_code == 200 and r.json()["stored"] == 1
        s = c.get("/api/voice/trace/summary?window_hours=24").json()
        assert s["turns"] == 1
        assert s["stages"]["end_to_end_first_audio"]["count"] == 1
        # nothing sensitive can have been stored
        assert "leak" not in str(s)


def test_trace_ingest_rejects_turnless_payload(app):
    from fastapi.testclient import TestClient
    with TestClient(app, base_url="http://127.0.0.1") as c:
        r = c.post("/api/voice/trace",
                   json={"stages": [{"stage": "end_of_speech_to_final", "ms": 1}]})
        assert r.status_code == 200 and r.json()["stored"] == 0


def test_full_multi_speaker_turn_roundtrips(app):
    """The whole-turn payload the DEFERRED flush now produces (the lifecycle
    fix): a 2-speaker round where the follower's playback/queue marks are only
    recorded AFTER round-done, once the playChain drains. All of it — both
    speakers' TTS/playback and the follower's queue wait — must ingest and
    surface in the summary, per model/TTS provider."""
    from fastapi.testclient import TestClient
    with TestClient(app, base_url="http://127.0.0.1") as c:
        r = c.post("/api/voice/trace", json={
            "turn_id": "turnMS", "chat_id": None,
            "stages": [
                {"stage": "end_of_speech_to_final", "ms": 400},
                {"stage": "final_to_first_token", "ms": 600, "model": "gpt-5.1"},
                # lead speaker (played before round-done)
                {"stage": "first_token_to_first_audio", "ms": 200,
                 "model": "gpt-5.1", "tts_provider": "elevenlabs", "speaker": "gpt"},
                {"stage": "first_audio_to_playback", "ms": 50,
                 "model": "gpt-5.1", "tts_provider": "elevenlabs", "speaker": "gpt"},
                # follower (marks recorded AFTER round-done, once playChain reached it)
                {"stage": "first_token_to_first_audio", "ms": 250,
                 "model": "claude-opus-4-8", "tts_provider": "elevenlabs", "speaker": "claude"},
                {"stage": "first_audio_to_playback", "ms": 2550,
                 "model": "claude-opus-4-8", "tts_provider": "elevenlabs", "speaker": "claude"},
                {"stage": "playback_queue_wait", "ms": 2500,
                 "model": "claude-opus-4-8", "tts_provider": "elevenlabs", "speaker": "claude"},
                {"stage": "end_to_end_first_audio", "ms": 1250,
                 "model": "gpt-5.1", "tts_provider": "elevenlabs", "speaker": "gpt"},
            ],
        })
        assert r.json()["stored"] == 8
        s = c.get("/api/voice/trace/summary?window_hours=24").json()
        st = s["stages"]
        # both speakers' TTS samples survived (not just the lead)
        assert st["first_token_to_first_audio"]["count"] == 2
        assert set(st["first_token_to_first_audio"]["by_model"]) == {"gpt-5.1", "claude-opus-4-8"}
        # the follower's post-round-done queue wait made it in
        assert st["playback_queue_wait"]["count"] == 1
        assert st["playback_queue_wait"]["by_model"]["claude-opus-4-8"]["p50"] == 2500
