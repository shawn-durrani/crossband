"""The synthetic voice benchmark (#94), tested without any provider.

The module's promises, pinned: requests are validated against the live
roster; a run is sequential data-in-files with progress written after every
unit; unsupported legs say WHY instead of failing; one seat's failure never
takes the run down; fixtures and results carry provenance and never a key
value; and the artefact route cannot be walked out of its run directory.
"""

import asyncio
import json

import pytest

from backend import benchmark, db


def _seat(slug, voice="", provider="anthropic", env="X_KEY"):
    return {"slug": slug, "name": slug.title(), "provider": provider,
            "model": "test-model-1", "base_url": "", "api_key_env": env,
            "voice_id": voice, "reasoning_effort": "", "thinking_control": "",
            "enabled": 1}


def _plan(seats, dims, cases=("echo", "arithmetic"), eleven=True):
    plan, err = benchmark.build_plan(
        {"models": [s["slug"] for s in seats], "cases": list(cases),
         "dimensions": list(dims)},
        seats, eleven)
    assert err is None, err
    return plan


@pytest.fixture
def bench_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    return tmp_path


@pytest.fixture
def fakes(monkeypatch):
    """Canned provider/voice calls, with call records the tests inspect."""
    calls = {"model": [], "tts": [], "stt": []}

    async def fake_model(seat, prompt, cfg):
        calls["model"].append((seat["slug"], prompt))
        if "three words" in prompt:
            text = "Benchmark reply received."
        elif "17 multiplied" in prompt:
            text = "391"
        else:
            text = "Because the air scatters blue light more than red."
        return {"text": text, "ttfb_s": 0.11, "total_s": 0.52,
                "output_tokens": 9, "leg": "fake"}

    def fake_tts(text, voice_id, cfg):
        calls["tts"].append((voice_id, text))
        return b"ID3-fake-audio-bytes"

    def fake_stt(audio, mime, cfg):
        calls["stt"].append(len(audio))
        return benchmark.FIXTURE_SENTENCE, "scribe_v2"

    monkeypatch.setattr(benchmark, "call_model", fake_model)
    monkeypatch.setattr(benchmark, "tts_call", fake_tts)
    monkeypatch.setattr(benchmark, "stt_call", fake_stt)
    return calls


def _run(plan, cfg=None):
    run_id = benchmark.new_run_id()
    return asyncio.run(benchmark.run_benchmark(
        run_id, plan, cfg or {"tts_model": "eleven_flash_v2_5",
                              "voice_pricing": {"tts_per_1m_chars": 100.0}}))


def test_build_plan_refuses_bad_requests():
    seats = [_seat("claude"), dict(_seat("off"), enabled=0)]
    cases = [
        ({"models": ["claude"], "dimensions": []}, "dimension"),
        ({"models": ["claude"], "dimensions": ["text"], "cases": []}, "case"),
        ({"models": [], "dimensions": ["tts"]}, "one model"),
        ({"models": ["ghost"], "dimensions": ["tts"]}, "ghost"),
        ({"models": ["off"], "dimensions": ["tts"]}, "off"),
    ]
    for body, needle in cases:
        plan, err = benchmark.build_plan(body, seats, True)
        assert plan is None and needle in err, (body, err)


def test_build_plan_normalises_and_counts():
    seats = [_seat("a", voice="v1"), _seat("b")]
    plan, err = benchmark.build_plan(
        {"models": ["a", "b"], "cases": ["echo", "nope"],
         "dimensions": ["text", "text", "tts", "pipeline", "stt"]},
        seats, True)
    assert err is None
    assert plan["dimensions"] == ["text", "tts", "pipeline", "stt"]
    assert plan["cases"] == ["echo"]          # unknown ids dropped
    assert plan["fixture_voice"] == "v1"      # first seat with a voice
    # 2 seats x 1 case + 1 stt + 2 tts + 2 pipeline
    assert benchmark.plan_total(plan) == 7
    # cases are ignored (not required) when text isn't selected
    plan2, err2 = benchmark.build_plan(
        {"models": ["a"], "dimensions": ["tts"]}, seats, True)
    assert err2 is None and plan2["cases"] == []


def test_voice_support_reasons():
    seat = _seat("a", voice="v1")
    assert benchmark.voice_support({"eleven": True}, seat) == ""
    assert "ElevenLabs" in benchmark.voice_support({"eleven": False}, seat)
    assert "no voice" in benchmark.voice_support({"eleven": True}, _seat("b"))


def test_normalise_is_a_fair_comparison_space():
    assert benchmark.normalise("  Benchmark, reply RECEIVED!  ") == \
        "benchmark reply received"
    assert benchmark.normalise("") == ""


def test_full_run_writes_labelled_results(bench_dir, fakes, monkeypatch):
    monkeypatch.setenv("X_KEY", "sk-supersecret-000")
    seats = [_seat("voiced", voice="v1"), _seat("mute")]
    results = _run(_plan(seats, ("text", "stt", "tts", "pipeline")))
    assert results["state"] == "done"
    assert results["progress"]["done"] == results["progress"]["total"] == 9
    assert benchmark._active == {}
    # the honesty labels ride every file
    assert results["synthetic"] is True and "not live-turn" in results["note"]
    # text: both cases judged, echo and arithmetic both match
    for slug in ("voiced", "mute"):
        for case in ("echo", "arithmetic"):
            unit = results["text"][slug][case]
            assert unit["status"] == "ok" and unit["matches_expected"] is True
            assert unit["first_word_s"] == 0.11
    # stt ran once, against the generated fixture, and matched it
    assert len(fakes["stt"]) >= 1
    assert results["stt"]["matches_fixture"] is True
    fixture_meta = json.loads(
        (benchmark.fixtures_root() / benchmark.FIXTURE_META).read_text())
    assert fixture_meta["synthetic"] is True
    assert fixture_meta["sentence"] == benchmark.FIXTURE_SENTENCE
    # tts/pipeline: the voiced seat produced artefacts, the mute seat is
    # unsupported with the reason, not failed
    run_dir = benchmark.runs_root() / results["run_id"]
    assert results["tts"]["voiced"]["status"] == "ok"
    assert (run_dir / results["tts"]["voiced"]["artefact"]).is_file()
    assert results["tts"]["voiced"]["est_cost"] is not None
    assert results["tts"]["mute"] == {"status": "unsupported",
                                      "reason": "no voice configured for this seat"}
    pipe = results["pipeline"]["voiced"]
    assert pipe["status"] == "ok" and set(pipe["stages"]) == {"stt", "model", "tts"}
    assert (run_dir / pipe["artefact"]).is_file()
    assert results["pipeline"]["mute"]["status"] == "unsupported"
    # a key VALUE must never appear in a result file (the env NAME may)
    on_disk = (run_dir / "results.json").read_text()
    assert "sk-supersecret-000" not in on_disk
    assert "X_KEY" in on_disk


def test_one_seats_failure_never_takes_the_run_down(bench_dir, fakes,
                                                    monkeypatch):
    async def flaky(seat, prompt, cfg):
        if seat["slug"] == "broken":
            raise RuntimeError("X_KEY is not set - Broken cannot reply.")
        return {"text": "391", "ttfb_s": 0.1, "total_s": 0.2,
                "output_tokens": 2, "leg": "fake"}

    monkeypatch.setattr(benchmark, "call_model", flaky)
    results = _run(_plan([_seat("ok"), _seat("broken")], ("text",),
                         cases=("arithmetic",)))
    assert results["state"] == "done"
    assert results["text"]["ok"]["arithmetic"]["status"] == "ok"
    unit = results["text"]["broken"]["arithmetic"]
    assert unit["status"] == "failed" and "X_KEY is not set" in unit["error"]


def test_eleven_off_is_unsupported_everywhere(bench_dir, fakes):
    seats = [_seat("a", voice="v1")]
    results = _run(_plan(seats, ("stt", "tts", "pipeline"), eleven=False))
    assert results["state"] == "done"
    for unit in (results["stt"], results["tts"]["a"], results["pipeline"]["a"]):
        assert unit["status"] == "unsupported"
        assert "ElevenLabs" in unit["reason"]
    assert not (benchmark.fixtures_root() / benchmark.FIXTURE_FILE).exists()
    assert fakes["tts"] == [] and fakes["stt"] == []


def test_fixture_generated_once_then_reused(bench_dir, fakes):
    plan = _plan([_seat("a", voice="v1")], ("stt",))
    _run(plan)
    generated = [t for t in fakes["tts"] if t[1] == benchmark.FIXTURE_SENTENCE]
    assert len(generated) == 1
    _run(plan)
    generated = [t for t in fakes["tts"] if t[1] == benchmark.FIXTURE_SENTENCE]
    assert len(generated) == 1  # second run read the cached clip


def test_hand_supplied_fixture_without_meta_still_runs(bench_dir, fakes):
    root = benchmark.fixtures_root()
    root.mkdir(parents=True)
    (root / benchmark.FIXTURE_FILE).write_bytes(b"owner-recorded-audio")
    results = _run(_plan([_seat("a", voice="v1")], ("stt",)))
    assert results["stt"]["status"] == "ok"
    # no reference sentence: honestly unjudged, never claimed as a match
    assert results["stt"]["matches_fixture"] is None
    assert results["fixture"]["supplied"] is True


def test_artefact_path_refuses_everything_unsafe(bench_dir):
    run_id = "bench-20260821-101500"
    run_dir = benchmark.runs_root() / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "tts-a.mp3").write_bytes(b"x")
    assert benchmark.artefact_path(run_id, "tts-a.mp3") is not None
    for bad_run, bad_name in (
            ("../fixtures", "tts-a.mp3"),      # run id shape
            ("bench-20260821-1015", "tts-a.mp3"),
            (run_id, "../results.json"),       # traversal
            (run_id, ".hidden"),               # dot-leading
            (run_id, "absent.mp3")):           # missing file
        assert benchmark.artefact_path(bad_run, bad_name) is None


def test_interrupted_run_reads_as_interrupted(bench_dir):
    run_id = "bench-20260821-090000"
    run_dir = benchmark.runs_root() / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "results.json").write_text(json.dumps(
        {"run_id": run_id, "state": "running", "models": [],
         "config": {"dimensions": ["text"]}, "created_at": "x",
         "progress": {"done": 1, "total": 4}}))
    assert benchmark.get_run(run_id)["state"] == "interrupted"
    (row,) = benchmark.list_runs()
    assert row["state"] == "interrupted" and row["run_id"] == run_id


def test_delete_run_is_guarded(bench_dir):
    run_id = "bench-20260821-110000"
    (benchmark.runs_root() / run_id).mkdir(parents=True)
    assert "no such run" in benchmark.delete_run("bench-20260101-000000")
    assert "not a benchmark" in benchmark.delete_run("../../chat.db")
    benchmark._active[run_id] = {}
    try:
        assert "still going" in benchmark.delete_run(run_id)
    finally:
        benchmark._active.clear()
    assert benchmark.delete_run(run_id) == ""
    assert not (benchmark.runs_root() / run_id).exists()


# ---------- the HTTP surface ----------

def test_catalogue_names_everything_the_panel_needs(client_factory):
    c = client_factory()
    body = c.get("/api/benchmark").json()
    assert {x["id"] for x in body["dimensions"]} == set(benchmark.DIMENSIONS)
    assert {x["id"] for x in body["cases"]} == set(benchmark.CASES)
    assert isinstance(body["seats"], list) and body["active_run_id"] is None
    assert body["eleven"] in (True, False)
    for seat in body["seats"]:  # names only, never key material
        assert "api_key_env" in seat and "system_prompt" not in seat


def test_run_endpoints_validate_and_guard(client_factory, monkeypatch):
    c = client_factory()
    r = c.post("/api/benchmark/runs",
               json={"models": [], "cases": ["echo"], "dimensions": ["text"]})
    assert r.status_code == 400
    # one at a time: an active run refuses a second start
    benchmark._active["bench-20260821-120000"] = {}
    try:
        r = c.post("/api/benchmark/runs",
                   json={"models": ["x"], "cases": ["echo"],
                         "dimensions": ["text"]})
        assert r.status_code == 409
    finally:
        benchmark._active.clear()
    assert c.get("/api/benchmark/runs/bench-20260101-000000").status_code == 404
    assert c.delete("/api/benchmark/runs/bench-20260101-000000").status_code == 404


def test_start_run_spawns_and_reports(client_factory, monkeypatch):
    seen = {}

    async def fake_run(run_id, plan, cfg, results=None):
        seen["run_id"], seen["plan"] = run_id, plan
        benchmark._active.pop(run_id, None)

    monkeypatch.setattr(benchmark, "run_benchmark", fake_run)
    c = client_factory()
    seats = c.get("/api/benchmark").json()["seats"]
    if not seats:  # a data dir with no seats can't exercise this path
        return
    try:
        r = c.post("/api/benchmark/runs",
                   json={"models": [seats[0]["slug"]], "cases": ["echo"],
                         "dimensions": ["text"]})
        assert r.status_code == 202
        assert benchmark.RUN_ID_RE.match(r.json()["run_id"])
    finally:
        benchmark._active.clear()  # the stub may not have run before teardown
