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
    # every kind of noise, made up and recorded
    assert {s.noise["kind"] for s in scripts if s.noise} == \
        set(script_mod.NOISE_KINDS) == {"cafe", "road", "chatter", "kitchen"}
    events = [e for t in turns for e in t.events]
    assert {"introduce": "Mateo"} in events
    assert {"room": "on"} in events and {"room": "off"} in events
    # a voice with no bank before the run, so a new voice is tested
    assert any(t.speaker not in cast.DEFAULT_ENROLLED for t in turns)
    # and "who's this?" answered out loud both ways, then a name spelt
    assert {"answer": "Mateo"} in events and {"answer": "TV"} in events
    assert {"correct": "Mateo"} in events


def test_only_the_synthetic_roster_speaks():
    for s in script_mod.load_scripts():
        assert set(s.cast) <= set(cast.voices())
        for line in s.lines():
            assert line.speaker in cast.voices()
    # the TV is nobody: never on the roster, never recorded
    assert not set(cast.MEDIA) & set(cast.CAST)
    assert not set(cast.MEDIA) & set(cast.DEFAULT_ENROLLED)


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
    (_script(turns=[{"speaker": "Alex", "text": "hi",
                     "events": [{"introduce": "TV"}]}]), "roster"),
    (_script(turns=[{"speaker": "Alex", "text": "hi",
                     "events": [{"correct": "TV"}]}]), "roster"),
    (_script(turns=[{"speaker": "Alex", "text": "hi",
                     "events": [{"answer": "Stranger"}]}]), "roster"),
    (_script(turns=[{"speaker": "Alex", "text": "hi",
                     "events": [{"depth": "deep"}]}]), "unknown event"),
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


def test_a_bed_is_made_once_then_read_from_the_cache(tmp_path):
    from eval_voice import beds as beds_mod
    calls = []

    def fetch(prompt, seconds):
        calls.append((prompt, seconds))
        return _tone(3.0, level=0.05, pad=0)

    b = beds_mod.Beds(tmp_path, fetch=fetch)
    first = b.bed("kitchen")
    assert b.bed("kitchen") == first and len(calls) == 1
    assert calls[0] == (beds_mod.BEDS["kitchen"], beds_mod.SECONDS)
    assert b.stats()["new_seconds"] == 3.0 and b.stats()["cached"] == 1
    assert beds_mod.Beds(tmp_path).bed("kitchen") == first
    with pytest.raises(beds_mod.BedError, match="no ElevenLabs key"):
        beds_mod.Beds(tmp_path).bed("chatter")
    with pytest.raises(beds_mod.BedError, match="wrong shape"):
        beds_mod.Beds(tmp_path / "x", fetch=lambda *a: b"\x01").bed("chatter")
    # the key covers the description, so a reworded bed is made again
    assert beds_mod.cache_key("a room") != beds_mod.cache_key("a room.")


def test_a_bed_lies_under_a_turn_at_the_scripts_level():
    from eval_voice.mock import MockBeds
    bed = MockBeds().bed("chatter")
    line = script_mod.Line("Sam", "hi")
    noise = {"kind": "chatter", "snr_db": 12}
    clean = mix.to_float(mix.mix_turn("s", 0, line, _tone(2.0)).pcm)
    noisy = mix.mix_turn("s", 0, line, _tone(2.0), noise=noise, bed=bed,
                         enrolled=("Sam",))
    under = mix.to_float(noisy.pcm) - clean
    speech = mix.speech_rms(mix.trim(mix.to_float(_tone(2.0))))
    assert 20 * np.log10(speech / mix.rms(under)) == pytest.approx(12, abs=0.3)
    assert noisy.truth.tags() == ["chatter noise"]
    again = mix.mix_turn("s", 0, line, _tone(2.0), noise=noise, bed=bed)
    other = mix.mix_turn("s", 1, line, _tone(2.0), noise=noise, bed=bed)
    assert again.pcm == noisy.pcm and other.pcm != noisy.pcm
    with pytest.raises(ValueError, match="needs its bed"):
        mix.mix_turn("s", 0, line, _tone(2.0), noise=noise)
    # a bed shorter than the turn loops
    long = mix.bed_stretch(bed, 25 * SR, np.random.default_rng(1))
    assert len(long) == 25 * SR and mix.rms(long) == pytest.approx(1.0)


def test_a_script_whose_bed_cant_be_made_is_left_out(tmp_path,
                                                     monkeypatch):
    from eval_voice import beds as beds_mod
    from eval_voice.mock import MockBeds

    def refuse(self, kind):
        raise beds_mod.BedError("the ElevenLabs key can't make sound "
                                "effects.")

    monkeypatch.setattr(MockBeds, "bed", refuse)
    data = tmp_path / "report.json"
    out = tmp_path / "report.md"
    assert runner.main(["--mock", "--cache", str(tmp_path / "cache"),
                        "--out", str(out), "--json-out", str(data)]) == 0
    got = json.loads(data.read_text())
    assert set(got["skipped"]) == {"cafe-chatter", "kitchen"}
    assert "cafe-chatter" not in got["scripts"] and got["scripts"]
    assert "Left out `kitchen`, because the ElevenLabs key" in \
        out.read_text()


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


def test_only_the_rigs_instance_ends_a_voice_session_early(tmp_path):
    short = instance.instance_env(8920, tmp_path, {}, owner="Alex",
                                  session_idle_s=45, environ={})
    assert short["CROSSBAND_VOICE_SESSION_IDLE_S"] == "45"
    plain = instance.instance_env(8920, tmp_path, {}, owner="Alex",
                                  environ={})
    assert "CROSSBAND_VOICE_SESSION_IDLE_S" not in plain
    # the rig's own setting never leaks in from the parent's environment
    leaky = instance.instance_env(
        8920, tmp_path, {}, owner="Alex",
        environ={"CROSSBAND_VOICE_SESSION_IDLE_S": "5"})
    assert "CROSSBAND_VOICE_SESSION_IDLE_S" not in leaky


class _Reply:
    def __init__(self, data):
        self.data = data

    def json(self):
        return self.data


class _SessionsApp:
    """The second app's /api/voice/sessions as the adapter polls it: the
    turn rows first, then the feed gone, then the end row."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = 0

    async def get(self, path, params=None):
        assert path == "/api/voice/sessions" and params["rows"] == "true"
        self.calls += 1
        return _Reply(self.replies[min(self.calls, len(self.replies)) - 1])


def test_the_adapter_waits_for_each_sessions_end_row(monkeypatch):
    import asyncio

    async def no_sleep(_):
        return None

    monkeypatch.setattr(crossband.asyncio, "sleep", no_sleep)
    turn = {"session": "s1", "turn_id": "rig-1", "method": "calibrated"}
    live = {"feeds": 1, "open_sessions": 1}
    end = {"session": "s1", "end": "idle", "filled": 2, "renamed": [1]}
    app = _SessionsApp([{"rows": [turn], "status": live}] * 3
                       + [{"rows": [end, turn], "status": {}}])
    a = crossband.CrossbandAdapter("http://127.0.0.1:8920",
                                   diariser="http://127.0.0.1:8910",
                                   end_pass_wait_s=100)
    got = asyncio.run(a._wait_end_pass(app, 7))
    assert app.calls == 4
    assert got["sessions"] == got["ended"] == 1
    assert got["relabelled"] == 2 and got["voices_renamed"] == 1
    assert got["why"] == {"idle": 1}
    # a session the app never ends isn't waited on forever
    none = _SessionsApp([{"rows": [turn], "status": live}])
    a.end_pass_wait_s = 0
    assert asyncio.run(a._wait_end_pass(none, 7))["ended"] == 0


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


def test_a_tv_turn_is_right_with_no_name_on_it():
    tv = TurnTruth("s", 0, [VoiceSpan("TV", 0.3, 5.0)], 5.5, enrolled=False,
                   media=True)
    assert tv.tags() == ["tv"]
    for reason in ("new_voice", "media"):
        row = scoring.judge(tv, Heard(0, labelled=True, reason=reason,
                                      placeholders=1))
        assert row["verdict"] == "right" and row["reason"] == reason
    assert scoring.judge(tv, Heard(0, labelled=True, names=["Dave"]))[
        "verdict"] == "wrong"
    assert scoring.judge(tv, Heard(0))["verdict"] == "no label"


def _ask_rows(who, media, labels):
    """Rows for a script where Alex answers "who's this?" on turn 3 and
    `who` spoke turns 1, 2 and 4, each read as `labels[i]` at the end."""
    rows = []
    for i, spoke in enumerate(["Alex", who, who, "Alex", who]):
        t = TurnTruth("s", i, [VoiceSpan(spoke, 0.3, 4.0)], 4.5,
                      enrolled=spoke == "Alex", media=media and spoke == who)
        rows.append(scoring.judge(t, labels[i]))
    return rows


def test_a_spoken_answer_is_scored_on_the_voices_turns():
    named = [Heard(0, labelled=True, names=["Alex"]),
             Heard(1, labelled=True, names=["Matteo"]),
             Heard(2, labelled=True, reason="new_voice", placeholders=1),
             Heard(3, labelled=True, names=["Alex"]),
             Heard(4, labelled=True, names=["Mateo"])]
    rows = _ask_rows("Mateo", False, named)
    answer = EventCheck(3, "answer", "Mateo", "heard", script="s",
                        detail={"ask_turn": 1, "clips": 2,
                                "people_new": ["Mateo"], "open_asks": 0})
    got, = scoring.asks(rows, [answer])
    assert got["asked_right"] and got["named"] and not got["media"]
    assert got["before"] == [1, 2] and got["after"] == [1, 1]
    assert got["clips"] == 2 and got["open_asks"] == 0
    # the pass relabelling the missed turn counts, read after the pass
    rows[2]["after_end"] = {"verdict": "right", "named": ["Mateo"],
                            "reason": ""}
    assert scoring.asks(rows, [answer])[0]["before"] == [2, 2]
    tv = [Heard(0, labelled=True, names=["Alex"]),
          Heard(1, labelled=True, reason="media", placeholders=1),
          Heard(2, labelled=True, reason="new_voice", placeholders=1),
          Heard(3, labelled=True, names=["Alex"]),
          Heard(4, labelled=True, names=["Dave"])]
    rows = _ask_rows("TV", True, tv)
    said = EventCheck(3, "answer", "TV", "heard", script="s",
                      detail={"ask_turn": 1, "clips": None,
                              "people_new": [], "open_asks": 1})
    got, = scoring.asks(rows, [said])
    assert got["media"] and got["named"]
    assert got["before"] == [1, 2] and got["after"] == [0, 1]
    assert rows[4]["verdict"] == "wrong"
    nothing = EventCheck(3, "answer", "TV", "no ask", script="s")
    assert scoring.asks(rows, [nothing])[0]["ask_turn"] is None
    text = "\n".join(report.ask_lines(scoring.asks(rows, [said, nothing])))
    assert "the TV" in text and "nothing asked" in text


def test_the_adapter_hears_an_answer_on_the_asked_turn(monkeypatch):
    import asyncio

    async def no_sleep(_):
        return None

    monkeypatch.setattr(crossband.asyncio, "sleep", no_sleep)

    class Chat:
        def __init__(self, labels):
            self.labels = list(labels)

        async def get(self, path, params=None):
            assert path == "/api/chats/5"
            label = self.labels.pop(0) if len(self.labels) > 1 \
                else self.labels[0]
            return _Reply({"messages": [{"id": 41, "voice_labels": label}]})

    a = crossband.CrossbandAdapter("http://127.0.0.1:8920")
    new = {"labels": [], "unresolved": "new_voice"}
    got = asyncio.run(a._check_answer(
        Chat([new, {"labels": ["Matteo"], "source": "introduction"}]), 5, 4,
        "Mateo", 41))
    assert got.result == "heard" and got.seen == "Matteo"
    assert got.detail == {"ask_message": 41}
    got = asyncio.run(a._check_answer(
        Chat([{"labels": [], "unresolved": "media"}]), 5, 4, "TV", 41))
    assert got.result == "heard" and got.seen == "media"
    assert asyncio.run(a._check_answer(Chat([new]), 5, 4, "TV", "")).result \
        == "no ask"
    a.event_wait_s = 0
    monkeypatch.setattr(crossband, "ANSWER_WAIT_S", 0)
    missed = asyncio.run(a._check_answer(Chat([new]), 5, 4, "Mateo", 41))
    assert missed.result == "missed" and missed.seen == "new_voice"


def test_a_person_is_found_by_either_of_their_names():
    people = [{"person_id": "p1", "name": "Sam", "preferred_name": "Sam"},
              {"person_id": "p2", "name": "Matteo",
               "preferred_name": "Mateo", "owner_set": True}]
    assert crossband._person(people, "Mateo")["person_id"] == "p2"
    assert crossband._person(people, "Dave") is None


def test_the_end_pass_is_scored_beside_the_names_at_the_time():
    rows = []
    for i, (who, then, later) in enumerate([("Sam", [], ["Sam"]),
                                            ("Sam", ["Sam"], ["Sam"]),
                                            ("Dave", ["Dave"], ["Sam"])]):
        t = TurnTruth("s", i, [VoiceSpan(who, 0.3, 3.0)], 3.5)
        row = scoring.judge(t, Heard(i, labelled=True, names=then,
                                     placeholders=int(not then)))
        again = scoring.judge(t, Heard(i, labelled=True, names=later))
        row["after_end"] = {k: again[k] for k in ("verdict", "named",
                                                  "reason")}
        rows.append(row)
    got = scoring.end_pass(rows)
    assert got["then"]["right"] == 2 and got["then"]["unnamed"] == 1
    assert got["after"]["right"] == 2 and got["after"]["wrong"] == 1
    assert got["wrong_per_100"] == {"then": 0.0, "after": 33.3}
    assert [(c["index"], c["verdict_after"]) for c in got["changed"]] == [
        (0, "right"), (2, "wrong")]
    assert scoring.end_pass([{k: v for k, v in r.items() if k != "after_end"}
                             for r in rows]) is None


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
    # the mock's end-of-session pass names the turns it left listening
    assert got["end_pass"]["changed"]
    assert "## After the end-of-session pass" in text


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
