"""Tests for the voice rig itself (crossband#416): the scripts and their
validator, the render cache, the mixer's truth, the crossband adapter's
frames and label reading, the isolated instance's environment, the
scoring and the report. Keyless and offline throughout: the renderer gets
a fake fetch, and the end-to-end run uses the rig's own mock stand-ins.
What the app does with real voices is what `python -m eval_voice`
measures."""

import base64
import json
from pathlib import Path

import numpy as np
import pytest

from eval_voice import cast, crossband, generate, instance, mix, report
from eval_voice import runner, scoring
from eval_voice import script as script_mod
from eval_voice.adapter import EventCheck, Heard
from eval_voice.mock import MockAdapter, MockRenderer
from eval_voice.render import RenderError, Renderer, cache_key
from eval_voice.truth import TurnTruth, VoiceSpan

SR = mix.SAMPLE_RATE


def _tone(seconds, hz=150.0, level=0.3, pad=0.2):
    """A line as the renderer returns it: speech-like buzz with silence
    either side."""
    t = np.arange(int(seconds * SR)) / SR
    x = level * (np.sin(2 * np.pi * hz * t) + 0.5 * np.sin(4 * np.pi * hz * t))
    z = np.zeros(int(pad * SR))
    return mix.to_pcm(np.concatenate([z, x, z]))


def _script(**over):
    d = {"id": "t", "cast": ["Alex", "Sam"], "noise": None,
         "turns": [{"speaker": "Alex", "text": "hello there"}]}
    d.update(over)
    return d


# ---------- scripts ----------

def test_builtin_scripts_load_and_cover_every_case():
    scripts = script_mod.load_scripts()
    ids = [s.id for s in scripts]
    assert len(ids) == len(set(ids)) >= 4
    turns = [t for s in scripts for t in s.turns]
    assert any(t.over for t in turns)                       # crosstalk
    assert any(t.line.gain_db <= -6 for t in turns)         # a quiet voice
    assert {s.noise["kind"] for s in scripts if s.noise} == {"cafe", "road"}
    events = [e for t in turns for e in t.events]
    assert {"introduce": "Mateo"} in events
    assert {"room": "on"} in events and {"room": "off"} in events
    # a voice with no bank before the run, so a new voice is tested
    assert any(t.speaker not in cast.DEFAULT_ENROLLED for t in turns)


def test_only_the_synthetic_roster_speaks():
    for s in script_mod.load_scripts():
        assert set(s.cast) <= set(cast.CAST)
        for line in s.lines():
            assert line.speaker in cast.CAST


@pytest.mark.parametrize("bad, why", [
    (_script(cast=["Alex", "Stranger"]), "roster"),
    (_script(turns=[{"speaker": "Dave", "text": "hi"}]), "cast"),
    (_script(turns=[{"speaker": "Alex", "text": ""}]), "text"),
    (_script(turns=[{"speaker": "Alex", "text": "hi", "gain_db": -40}]),
     "outside"),
    (_script(turns=[{"speaker": "Alex", "text": "hi",
                     "events": [{"introduce": "Stranger"}]}]), "roster"),
    (_script(turns=[{"speaker": "Alex", "text": "hi",
                     "events": [{"room": "maybe"}]}]), "on or off"),
    (_script(turns=[{"speaker": "Alex", "text": "hi",
                     "over": {"speaker": "Alex", "text": "me too"}}]),
     "second person"),
    (_script(noise={"kind": "rain", "snr_db": 10}), "noise"),
])
def test_the_validator_refuses_a_bad_script(bad, why):
    with pytest.raises(script_mod.ScriptError, match=why):
        script_mod.from_dict(bad)


def test_a_script_round_trips_through_its_dict():
    for s in script_mod.load_scripts():
        again = script_mod.from_dict(s.to_dict())
        assert again.to_dict() == s.to_dict()


# ---------- the render cache ----------

def test_each_line_is_rendered_once_then_read_from_the_cache(tmp_path):
    calls = []

    def fetch(voice_id, model, text):
        calls.append((voice_id, model, text))
        return _tone(1.0)

    r = Renderer(tmp_path, fetch=fetch)
    first = r.line("Sam", "hello there")
    again = r.line("Sam", "hello there")
    assert first == again and len(calls) == 1
    assert r.stats()["new_chars"] == len("hello there")
    assert r.stats()["cached_chars"] == len("hello there")
    # a rerun with no key at all speaks from the cache
    keyless = Renderer(tmp_path, fetch=None)
    assert keyless.line("Sam", "hello there") == first
    assert keyless.stats()["new_chars"] == 0
    with pytest.raises(RenderError, match="no ElevenLabs key"):
        keyless.line("Sam", "a line nobody rendered")


def test_the_cache_key_covers_voice_model_and_text():
    base = cache_key("v1", "m1", "hi")
    assert base != cache_key("v2", "m1", "hi")
    assert base != cache_key("v1", "m2", "hi")
    assert base != cache_key("v1", "m1", "hi.")


def test_bad_audio_from_the_renderer_is_refused(tmp_path):
    r = Renderer(tmp_path, fetch=lambda *a: b"\x01")
    with pytest.raises(RenderError, match="wrong shape"):
        r.line("Alex", "hello")
    assert not list((tmp_path / "lines").rglob("*.pcm"))


# ---------- the mixer and the truth ----------

def test_a_crosstalk_turn_records_who_spoke_when():
    main = script_mod.Line("Sam", "one two three four five six")
    over = script_mod.Over(line=script_mod.Line("Dave", "seven eight nine"),
                           at=0.5)
    mt = mix.mix_turn("s", 2, main, _tone(4.0, 200), over, _tone(2.0, 120),
                      enrolled=("Sam", "Dave"))
    t = mt.truth
    sam, dave = t.voices
    assert sam.start == pytest.approx(mix.LEAD_S, abs=0.03)
    assert sam.seconds == pytest.approx(4.0, abs=0.05)
    assert dave.start == pytest.approx(sam.start + 2.0, abs=0.05)
    assert dave.seconds == pytest.approx(2.0, abs=0.05)
    assert t.overlap_s() == pytest.approx(2.0, abs=0.06)
    assert t.alone_s("Sam") == pytest.approx(2.0, abs=0.06)
    assert t.alone_s("Dave") == pytest.approx(0.0, abs=0.06)
    assert t.main == "Sam" and t.crosstalk and "crosstalk" in t.tags()
    assert t.seconds == pytest.approx(len(mt.pcm) / 2 / SR, abs=0.001)
    assert t.seconds == pytest.approx(dave.end + mix.TAIL_S, abs=0.03)


def test_the_voice_heard_most_alone_is_the_main_one():
    t = TurnTruth("s", 0, [VoiceSpan("Sam", 0.3, 1.3),
                           VoiceSpan("Dave", 1.0, 4.0)], 4.5)
    assert t.main == "Dave"
    tie = TurnTruth("s", 0, [VoiceSpan("Sam", 0.0, 2.0),
                             VoiceSpan("Dave", 2.0, 4.0)], 4.0)
    assert tie.main == "Sam"


def test_a_short_interjection_over_a_turn_is_not_crosstalk():
    t = TurnTruth("s", 0, [VoiceSpan("Sam", 0.3, 4.0),
                           VoiceSpan("Dave", 2.0, 2.6)], 4.5)
    assert not t.crosstalk


def test_gain_and_noise_land_where_the_script_puts_them():
    quiet = script_mod.Line("Sam", "hi", gain_db=-12)
    loud = script_mod.Line("Sam", "hi")
    a = mix.to_float(mix.mix_turn("s", 0, loud, _tone(2.0)).pcm)
    b = mix.to_float(mix.mix_turn("s", 0, quiet, _tone(2.0)).pcm)
    assert 20 * np.log10(mix.rms(b) / mix.rms(a)) == pytest.approx(-12, abs=0.5)
    noisy = mix.mix_turn("s", 0, loud, _tone(2.0),
                         noise={"kind": "road", "snr_db": 10},
                         enrolled=("Sam",))
    bed = mix.to_float(noisy.pcm) - a   # the same turn with no noise
    speech = mix.speech_rms(mix.trim(mix.to_float(_tone(2.0))))
    assert 20 * np.log10(speech / mix.rms(bed)) == pytest.approx(10, abs=0.3)
    assert noisy.truth.tags() == ["road noise"]


def test_the_same_script_always_mixes_to_the_same_audio():
    line = script_mod.Line("Alex", "hi")
    noise = {"kind": "cafe", "snr_db": 15}
    one = mix.mix_turn("s", 3, line, _tone(1.5), noise=noise)
    two = mix.mix_turn("s", 3, line, _tone(1.5), noise=noise)
    other = mix.mix_turn("s", 4, line, _tone(1.5), noise=noise)
    assert one.pcm == two.pcm and one.pcm != other.pcm


def test_the_mix_never_clips():
    line = script_mod.Line("Alex", "hi", gain_db=6)
    over = script_mod.Over(script_mod.Line("Sam", "hi", gain_db=6), at=0.1)
    x = mix.to_float(mix.mix_turn("s", 0, line, _tone(2, level=0.9), over,
                                  _tone(2, level=0.9)).pcm)
    assert np.max(np.abs(x)) <= mix.PEAK + 1e-3


def test_a_whole_script_mixes_with_introductions_tracked():
    s = next(s for s in script_mod.load_scripts() if s.id == "introductions")
    turns = mix.mix_script(s, MockRenderer(), enrolled=cast.DEFAULT_ENROLLED)
    assert len(turns) == len(s.turns)
    mateo = [mt.truth for mt in turns if mt.truth.main == "Mateo"]
    assert mateo and all(not t.enrolled and t.introduced for t in mateo)
    assert all("introduced voice" in t.tags() for t in mateo)
    pcm, timeline = mix.conversation(turns)
    assert len(pcm) / 2 / SR == pytest.approx(
        sum(mt.seconds + mt.gap_s for mt in turns), abs=0.01)
    assert [e["turn"] for e in timeline] == list(range(len(turns)))


# ---------- the crossband adapter's frames and labels ----------

def test_a_turn_goes_as_the_browser_sends_it():
    pcm = _tone(1.0)
    frames = crossband.turn_frames(pcm, "turn-1")
    audio = [f for f in frames if "audio" in f]
    assert b"".join(base64.b64decode(f["audio"]) for f in audio) == pcm
    assert all(len(base64.b64decode(f["audio"])) <= crossband.CHUNK_SAMPLES * 2
               for f in audio)
    assert frames[-1] == {"sample_rate": SR, "commit": True,
                          "turn_id": "turn-1"}
    assert all(f["sample_rate"] == SR for f in frames)


def test_labels_are_read_the_way_the_app_writes_them():
    named = crossband.heard_from_label(json.dumps(
        {"clusters": ["session"], "labels": ["Sam"], "uncertain": [],
         "source": "session", "score": 0.97}), 0)
    assert named.labelled and named.names == ["Sam"] and named.score == 0.97
    owner = crossband.heard_from_label(
        {"labels": ["Alex"], "uncertain": [], "owner": True}, 1)
    assert owner.owner and owner.names == ["Alex"]
    listening = crossband.heard_from_label(
        {"labels": [], "uncertain": [], "unresolved": "listening"}, 2)
    assert listening.labelled and not listening.names
    assert listening.reason == "listening"
    learning = crossband.heard_from_label(
        {"labels": ["Mateo"], "uncertain": ["Mateo"], "learning": True}, 3)
    assert learning.learning and learning.unsure == ["Mateo"]
    xt = crossband.heard_from_label({
        "labels": ["Sam", "Voice 2"], "uncertain": ["Voice 2"],
        "crosstalk": True, "overlap": True,
        "segments": [{"label": "Sam", "text": "are we going",
                      "uncertain": False},
                     {"label": "Voice 2", "text": "yes soon",
                      "uncertain": True}]}, 4)
    assert xt.crosstalk and xt.names == ["Sam"] and xt.placeholders == 1
    assert xt.segments[1]["name"] is None
    only_voices = crossband.heard_from_label(
        {"labels": ["Voice 1", "Voice 2"], "crosstalk": True}, 5)
    assert only_voices.reason == "voices still listening"
    assert not crossband.heard_from_label("", 6).labelled
    assert crossband.heard_from_label("{not json", 7).note


# ---------- the isolated instance ----------

def test_the_instance_environment_is_built_from_nothing(tmp_path):
    parent = {"PATH": "/bin", "HOME": "/home/you",
              "MEMORY_AUTH_TOKEN": "secret", "OPENAI_API_KEY": "sk-x",
              "GITHUB_TOKEN": "gh", "CROSSBAND_MEMORY_URL": "http://127.0.0.1:8901",
              "CROSSBAND_MCP_SERVERS": '{"spend": {}}'}
    env = instance.instance_env(
        8920, tmp_path / "data",
        {"ELEVENLABS_API_KEY": "el", "ANTHROPIC_API_KEY": "an",
         "OPENAI_API_KEY": "never"},
        owner="Alex", diariser="http://127.0.0.1:8910", environ=parent)
    assert env["CROSSBAND_MEMORY_URL"] == instance.NOWHERE
    assert env["CROSSBAND_DATA_DIR"] == str(tmp_path / "data")
    assert env["CROSSBAND_PORT"] == "8920"
    assert env["CROSSBAND_SIBLING_APPS"] == "{}"
    assert env["CROSSBAND_DIARIZE_SHADOW_URL"] == "http://127.0.0.1:8910"
    assert env["ELEVENLABS_API_KEY"] == "el" and env["ANTHROPIC_API_KEY"] == "an"
    for leaked in ("MEMORY_AUTH_TOKEN", "OPENAI_API_KEY", "GITHUB_TOKEN",
                   "CROSSBAND_MCP_SERVERS"):
        assert leaked not in env
    assert env["PATH"] == "/bin"


def test_the_instance_refuses_the_fleets_ports():
    for port in (8901, 8902, 8903, 8904, 8910):
        with pytest.raises(instance.InstanceError, match="fleet"):
            instance.check_port(port)


def test_the_code_copy_carries_no_local_settings_or_keys(tmp_path):
    repo = tmp_path / "repo"
    (repo / "backend" / "__pycache__").mkdir(parents=True)
    (repo / "backend" / "app.py").write_text("x = 1\n")
    (repo / "backend" / "__pycache__" / "app.pyc").write_bytes(b"\0")
    (repo / "config.json").write_text("{}")
    (repo / "config.local.json").write_text('{"mcp_servers": {"s": {}}}')
    (repo / ".env").write_text("MEMORY_AUTH_TOKEN=secret\n")
    code = instance.snapshot_code(tmp_path / "run", repo)
    copied = sorted(str(p.relative_to(code)) for p in code.rglob("*"))
    assert copied == ["backend", "backend/app.py", "config.json"]


def test_the_rig_never_starts_on_the_apps_own_data_folder(tmp_path):
    repo = tmp_path / "repo"
    (repo / "data").mkdir(parents=True)
    inst = instance.Instance(repo, port=18999, repo=repo)
    with pytest.raises(instance.InstanceError, match="app's own"):
        inst.start()


def test_the_rig_reads_only_the_diariser_from_local_config(tmp_path):
    (tmp_path / "config.local.json").write_text(json.dumps(
        {"diarize_shadow_url": "http://127.0.0.1:8910",
         "mcp_servers": {"s": {}}}))
    assert runner.default_diariser(tmp_path) == "http://127.0.0.1:8910"
    assert runner.default_diariser(tmp_path / "nowhere") == ""


def test_keys_come_from_the_env_file_two_by_name(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    env = tmp_path / "app.env"
    env.write_text("ELEVENLABS_API_KEY=el\nMEMORY_AUTH_TOKEN=secret\n")
    assert runner.read_keys(str(env)) == {"ELEVENLABS_API_KEY": "el",
                                          "ANTHROPIC_API_KEY": ""}
    with pytest.raises(SystemExit, match="not found"):
        runner.read_keys(str(tmp_path / "missing.env"))


# ---------- scoring ----------

def _truth(*voices, enrolled=True):
    return TurnTruth("s", 0, [VoiceSpan(*v) for v in voices], 5.0,
                     enrolled=enrolled)


def test_each_verdict():
    t = _truth(("Sam", 0.3, 3.0, 0.0, "hi there"))
    assert scoring.judge(t, Heard(0, labelled=True, names=["Sam"]))["verdict"] \
        == "right"
    assert scoring.judge(t, Heard(0, labelled=True, names=["Dave"]))["verdict"] \
        == "wrong"
    row = scoring.judge(t, Heard(0, labelled=True, reason="listening",
                                 placeholders=1))
    assert row["verdict"] == "unnamed" and row["has_reason"]
    assert scoring.judge(t, Heard(0))["verdict"] == "no label"


def test_a_misspelt_name_is_matched_and_a_stranger_is_wrong():
    assert scoring.canonical("Matteo") == "Mateo"
    assert scoring.canonical("sam") == "Sam"
    assert scoring.canonical("Stranger") == "Stranger"
    t = _truth(("Mateo", 0.3, 3.0))
    assert scoring.judge(t, Heard(0, labelled=True, names=["Matteo"]))[
        "verdict"] == "right"
    assert scoring.judge(t, Heard(0, labelled=True, names=["Stranger"]))[
        "verdict"] == "wrong"


def test_naming_the_other_voice_in_crosstalk_is_not_wrong():
    t = _truth(("Sam", 0.3, 4.0, 0.0, "are we going to the shops"),
               ("Dave", 2.0, 3.5, 0.0, "yes soon"))
    row = scoring.judge(t, Heard(0, labelled=True, names=["Dave"],
                                 placeholders=1, crosstalk=True))
    assert row["verdict"] == "unnamed" and not row["crosstalk"]["both_named"]


def test_the_crosstalk_split_is_scored_by_words():
    t = _truth(("Sam", 0.3, 4.0, 0.0, "are we going to the shops"),
               ("Dave", 2.0, 3.5, 0.0, "yes soon"))
    good = Heard(0, labelled=True, names=["Sam", "Dave"], crosstalk=True,
                 segments=[{"name": "Sam", "text": "are we going to the shops"},
                           {"name": "Dave", "text": "yes soon"}])
    row = scoring.judge(t, good)
    assert row["verdict"] == "right"
    assert row["crosstalk"]["share_right"] == 1.0
    assert row["crosstalk"]["split_right"]
    swapped = Heard(0, labelled=True, names=["Sam", "Dave"], crosstalk=True,
                    segments=[{"name": "Sam", "text": "are we going yes soon"},
                              {"name": "Dave", "text": "to the shops"}])
    row = scoring.judge(t, swapped)
    assert row["crosstalk"]["words_wrong"] == 5
    assert not row["crosstalk"]["split_right"]
    unmarked = scoring.judge(t, Heard(0, labelled=True, names=["Sam"]))
    assert unmarked["verdict"] == "right"
    assert not unmarked["crosstalk"]["marked"]
    assert not unmarked["crosstalk"]["split_right"]


def test_aggregate_and_the_redesign_targets():
    rows = []
    for i, (who, names) in enumerate([("Sam", ["Sam"]), ("Sam", ["Sam"]),
                                      ("Sam", []), ("Dave", ["Sam"])]):
        t = TurnTruth("s", i, [VoiceSpan(who, 0.3, 3.0)], 3.5)
        rows.append(scoring.judge(t, Heard(i, labelled=True, names=names,
                                           in_time=i != 1)))
    events = [EventCheck(0, "introduce", "Mateo", "heard"),
              EventCheck(1, "room", "on", "missed"),
              EventCheck(2, "room", "off", "already")]
    got = scoring.aggregate(rows, events)
    assert got["summary"]["right"]["turns"] == 2
    assert got["summary"]["wrong"]["turns"] == 1
    assert got["wrong"][0]["truth"] == "Dave"
    assert got["targets"]["wrong_per_100"] == 25.0
    # Sam's third turn is the only one after his first two, and unnamed
    assert got["targets"]["unnamed_per_100_after_two"] == 100.0
    assert got["targets"]["first_named"] == 1
    assert got["targets"]["first_turns"] == 2
    assert got["targets"]["in_time"] == 3
    assert got["events"] == {"introduce": {"heard": 1},
                             "room on": {"missed": 1},
                             "room off": {"already": 1}}


# ---------- the script generator ----------

def test_a_generated_script_is_checked_and_named_apart():
    reply = "```json\n" + json.dumps({
        "id": "Weekend Plans", "about": "x", "cast": ["Alex", "Sam"],
        "noise": None,
        "turns": [{"speaker": "Alex", "text": "shall we go"},
                  {"speaker": "Sam", "text": "yes let's",
                   "over": {"speaker": "Alex", "text": "great", "at": 0.5}}]}) \
        + "\n```"
    s = generate.parse_reply(reply, ["Alex", "Sam"])
    assert s.id.startswith("gen-weekend-plans-") and s.source == "generated"
    with pytest.raises(generate.GenerateError):
        generate.parse_reply('{"id": "x", "cast": ["Alex"], "turns": '
                             '[{"speaker": "Stranger", "text": "hi"}]}', ["Alex"])
    with pytest.raises(generate.GenerateError, match="no JSON"):
        generate.parse_reply("sorry, I can't", ["Alex"])


def test_the_generator_retries_a_bad_reply_once():
    import asyncio
    replies = iter(["not json", json.dumps(
        {"id": "ok", "cast": ["Alex", "Sam"],
         "turns": [{"speaker": "Alex", "text": "hello"}]})])

    async def caller(prompt):
        assert "Alex, Sam" in prompt and "over" in prompt
        return next(replies)

    s = asyncio.run(generate.generate(caller, ["Alex", "Sam"], 4,
                                      ["crosstalk"]))
    assert s.turns[0].speaker == "Alex"
    with pytest.raises(generate.GenerateError, match="unknown features"):
        generate.build_prompt(["Alex"], features=["fireworks"])


# ---------- the whole rig, keyless ----------

def test_a_mock_run_end_to_end(tmp_path, capsys):
    out = tmp_path / "report.md"
    data = tmp_path / "report.json"
    assert runner.main(["--mock", "--cache", str(tmp_path / "cache"),
                        "--out", str(out), "--json-out", str(data)]) == 0
    text = out.read_text()
    got = json.loads(data.read_text())
    assert got["turns"] == sum(len(s.turns) for s in script_mod.load_scripts())
    assert "Mock run" in text and "## Every turn" in text
    assert got["summary"]["wrong"]["turns"] >= 1          # the mock's slip
    assert got["crosstalk"]["turns"] >= 2
    # content-free: no line of any script reaches the report
    for s in script_mod.load_scripts():
        for line in s.lines():
            assert line.text not in text
    assert all("transcript" not in r for r in got["rows"])
    assert not (tmp_path / "cache" / "mixes").exists()   # the mock writes nothing


def test_the_markdown_report_renders_every_section():
    s = next(s for s in script_mod.load_scripts() if s.id == "crosstalk-cafe")
    turns = mix.mix_script(s, MockRenderer(), enrolled=cast.DEFAULT_ENROLLED)
    result = MockAdapter().converse(s.id, turns)
    rows = [scoring.judge(mt.truth, h) for mt, h in zip(turns, result.heard)]
    text = report.render_markdown(scoring.aggregate(rows, result.events),
                                  mock=True)
    for heading in ("## Naming", "## Against the redesign's targets",
                    "## By condition", "## Crosstalk",
                    "## Introductions and instructions", "## Every turn",
                    "## Setup and cost"):
        assert heading in text


def test_the_rig_cache_is_ignored_by_git():
    ignore = (Path(__file__).resolve().parent.parent / ".gitignore").read_text()
    assert "eval_voice/cache/" in ignore.splitlines()
