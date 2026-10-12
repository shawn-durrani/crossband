"""The turn-taking rig (eval_turns), keyless and with no browser.

Pins: the moments load, name only the made-up roster and filter by id,
kind or group; a moment's audio has its pause where the truth says, at the
speaking level less its gain, and mixes the same way every time; the
stand-in seat gives the long reply only to the opening question as the
transcriber heard it, streams it as a local model would, and answers 404
on the Responses route; the timeline reads a pause as one turn, an early
send or a missed one, and a talked-over reply as paused, cut, let go, cut
late or never playing; the summary counts short and long pauses apart; a
stand-in run reports every moment right; the app copy carries the built
page and the judge's key only when asked; and every debug line and button
the rig reads still exists in the page, so a rename fails here instead of
silently in a run.
"""

import json
import re
import urllib.error
import urllib.request
from pathlib import Path

import numpy as np
import pytest

from eval_turns import moments as M
from eval_turns import runner, seat, timeline
from eval_turns.page import END_LABEL, INIT_JS, TALK
from eval_voice import instance

REPO = Path(__file__).resolve().parents[1]


class ToneRenderer:
    """A tone for each word, so clips have known lengths with no key."""

    def line(self, who, text):
        n = int(M.RATE * 0.3 * len(text.split()))
        t = np.arange(n) / M.RATE
        return (0.05 * np.sin(2 * np.pi * 200 * t) * 32767).astype("<i2").tobytes()


# ---------- the moments ----------

def test_the_moments_load_and_cover_every_kind_and_group():
    plan = M.load()
    assert {m.kind for m in plan.moments} == set(M.KINDS)
    assert {m.group for m in plan.moments if m.kind == "over"} == set(M.GROUPS)
    assert plan.opening and len(plan.reply) > 150
    assert [m.id for m in M.load(only="interrupt").moments] == [
        "over_stop", "over_name", "over_question"]
    assert {m.kind for m in M.load(only="pause").moments} == {"pause"}
    with pytest.raises(ValueError):
        M.load(only="nothing_like_this")


@pytest.mark.parametrize("bad, why", [
    ({"who": "Priya"}, "roster"),
    ({"pause_s": 9}, "pause_s"),
    ({"gain_db": -40}, "gain_db"),
    ({"noise": "jet"}, "noise"),
])
def test_the_validator_refuses_a_bad_moment(tmp_path, bad, why):
    raw = json.loads(M.MOMENTS.read_text())
    raw["moments"][0].update(bad)
    path = tmp_path / "moments.json"
    path.write_text(json.dumps(raw))
    with pytest.raises(ValueError, match=why):
        M.load(path)


def test_the_moments_name_only_the_made_up_roster():
    text = M.MOMENTS.read_text()
    for who in re.findall(r'"who": "([^"]+)"', text):
        assert who in {"Alex", "Sam", "Dave", "Mateo", "TV"}


# ---------- the audio ----------

def _speech_dbfs(clip, start, end):
    x = np.frombuffer(clip.pcm, dtype="<i2").astype(np.float64) / 32768
    seg = x[int(start * M.RATE):int(end * M.RATE)]
    return 20 * np.log10(np.sqrt(np.mean(seg ** 2)))


def test_a_pause_sits_where_the_truth_says_at_the_speaking_level():
    m = next(m for m in M.load().moments if m.id == "pause_one")
    clip = M.clip_for(m, ToneRenderer())
    assert clip.onset_s == M.LEAD_S
    assert clip.pause[1] - clip.pause[0] == pytest.approx(m.pause_s, abs=0.002)
    assert clip.end_s > clip.pause[1] and clip.seconds == pytest.approx(clip.end_s + M.TAIL_S, abs=0.01)
    assert _speech_dbfs(clip, clip.onset_s, clip.pause[0]) == pytest.approx(M.SPEECH_DBFS, abs=1)
    x = np.frombuffer(clip.pcm, dtype="<i2")
    gap = x[int((clip.pause[0] + 0.05) * M.RATE):int((clip.pause[1] - 0.05) * M.RATE)]
    assert np.abs(gap).max() == 0


def test_a_quieter_moment_is_quieter_and_noise_mixes_the_same_way():
    plan = M.load()
    tv = next(m for m in plan.moments if m.id == "over_tv")
    clip = M.clip_for(tv, ToneRenderer())
    assert _speech_dbfs(clip, clip.onset_s, clip.end_s) == pytest.approx(
        M.SPEECH_DBFS + tv.gain_db, abs=1)
    cafe = next(m for m in plan.moments if m.noise)
    assert M.clip_for(cafe, ToneRenderer()).pcm == M.clip_for(cafe, ToneRenderer()).pcm


# ---------- the stand-in seat ----------

def test_the_long_reply_goes_only_to_the_opening_question_as_heard():
    opening = "How do I make really good meatballs?"
    heard = [{"role": "system", "content": "rules"},
             {"role": "user", "content": "[Alex] how do i make really good meat balls"},
             {"role": "user", "content": "context note"}]
    assert seat.reply_for(heard, opening, "LONG") == "LONG"
    later = heard + [{"role": "assistant", "content": "LONG"},
                     {"role": "user", "content": "[Alex] wait stop"},
                     {"role": "user", "content": "context note"}]
    assert seat.reply_for(later, opening, "LONG") == seat.SHORT_REPLY
    parts = [{"role": "user", "content": [{"type": "text", "text": opening}]}]
    assert seat.reply_for(parts, opening, "LONG") == "LONG"


def _post(url, body):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    return urllib.request.urlopen(req, timeout=10)


def test_the_seat_streams_like_a_local_model_and_has_no_responses_route():
    s = seat.Seat("How do I make meatballs?", "Soak the breadcrumbs.",
                  first_token_s=0).start()
    try:
        with pytest.raises(urllib.error.HTTPError) as e:
            _post(s.base_url + "/responses", {"input": "hi"})
        assert e.value.code == 404
        with urllib.request.urlopen(s.base_url + "/models", timeout=10) as r:
            assert json.loads(r.read())["data"][0]["id"] == seat.MODEL
        body = {"stream": True, "messages": [
            {"role": "user", "content": "how do I make meatballs"}]}
        with _post(s.base_url + "/chat/completions", body) as r:
            raw = r.read().decode()
        chunks = [json.loads(line[6:]) for line in raw.splitlines()
                  if line.startswith("data: {")]
        text = "".join(c["choices"][0]["delta"].get("content", "") for c in chunks)
        assert text == "Soak the breadcrumbs." and raw.rstrip().endswith("[DONE]")
        with _post(s.base_url + "/chat/completions",
                   {"messages": [{"role": "user", "content": "yeah"}]}) as r:
            done = json.loads(r.read())
        assert done["choices"][0]["message"]["content"] == seat.SHORT_REPLY
        assert [q["long"] for q in s.requests] == [True, False]
    finally:
        s.stop()


# ---------- the timeline ----------

PAUSE = M.Moment("p", "pause", "Alex", parts=("a", "b"), pause_s=1.2)
PCLIP = M.Clip(b"", onset_s=0.3, end_s=4.0, pause=(1.5, 2.7))


def _send(t):
    return {"t": t, "path": "/api/chats/1/send"}


def test_a_pause_is_one_turn_an_early_send_or_a_missed_one():
    ok = timeline.pause(PAUSE, PCLIP, 1000.0, {"fetch": [_send(6900)],
                        "log": [{"t": 6500, "tag": "vad:finalize"}]}, 12000)
    assert (ok["turns"], ok["jumped_in"], ok["send_ms"], ok["finalize_ms"]) == (1, False, 1900, 1500)
    early = timeline.pause(PAUSE, PCLIP, 1000.0, {"fetch": [_send(4300), _send(6900)]}, 12000)
    assert early["jumped_in"] and early["turns"] == 2 and early["cut_in_ms"] == 1800
    assert "send_ms" not in early
    assert timeline.pause(PAUSE, PCLIP, 1000.0, {}, 12000)["missed"]


OPENING = M.Clip(b"", onset_s=0.3, end_s=2.0)
STOP = M.Moment("s", "over", "Alex", text="Wait, stop.", group="interrupt")
YEAH = M.Moment("y", "over", "Alex", text="Mm-hm.", group="backchannel")
OCLIP = M.Clip(b"", onset_s=0.3, end_s=1.0)


def _over(moment, log, media=None, fetch=None):
    # The question ends at 3000; the reply plays at 6000; the moment's clip
    # starts at 7700, so its first word is at 8000.
    media = media if media is not None else [{"t": 6000, "ev": "playing"}]
    events = {"log": log, "media": media, "fetch": [_send(4900)] + (fetch or [])}
    return timeline.over(moment, OCLIP, OPENING, 1000.0, 7700.0, events, 16000)


def test_an_interruption_pauses_then_cuts():
    r = _over(STOP, [{"t": 8550, "tag": "vad:hold"}, {"t": 9100, "tag": "vad:bargeIn"},
                     {"t": 8900, "tag": "vad:words", "data": {"verdict": "cut"}}])
    assert (r["held"], r["cut"], r["resumed"], r["right"]) == (True, True, False, True)
    assert (r["hold_ms"], r["cut_ms"], r["words"]) == (550, 1100, "cut")
    assert (r["send_ms"], r["reply_ms"]) == (1900, 3000)


def test_a_yeah_is_let_go_and_a_late_cut_is_counted():
    let_go = _over(YEAH, [{"t": 8400, "tag": "vad:hold"}, {"t": 9500, "tag": "vad:letGo"}])
    assert (let_go["cut"], let_go["resumed"], let_go["right"], let_go["resume_ms"]) == \
        (False, True, True, 1500)
    late = _over(YEAH, [{"t": 8400, "tag": "vad:hold"}, {"t": 9500, "tag": "vad:letGo"},
                        {"t": 9700, "tag": "judge:verdict", "data": {"verdict": "interrupt"}}],
                 fetch=[{"t": 10200, "path": "/api/chats/1/round/abort"}])
    assert late["cut"] and late["late_cut"] and late["cut_ms"] == 2200
    assert late["judge"] == "interrupt" and not late["right"]


def test_a_reply_that_ended_first_is_not_scored():
    ended = _over(STOP, [], media=[{"t": 6000, "ev": "playing"}, {"t": 6800, "ev": "ended"}])
    assert ended["no_reply"] and "right" not in ended
    assert _over(STOP, [], media=[])["no_reply"]


def test_the_summary_counts_short_and_long_pauses_apart():
    results = [
        {"kind": "pause", "pause_s": 0.8, "jumped_in": False, "missed": False, "send_ms": 1800},
        {"kind": "pause", "pause_s": 2.0, "jumped_in": True, "missed": False},
        {"kind": "pause", "pause_s": 1.2, "jumped_in": False, "missed": True},
        _over(STOP, [{"t": 8550, "tag": "vad:hold"}, {"t": 9100, "tag": "vad:bargeIn"}]),
    ]
    s = timeline.summarise(results, 1.5)
    assert (s["short"], s["jumped_in_short"], s["long"], s["jumped_in_long"]) == (1, 0, 1, 1)
    assert s["missed_turns"] == 1 and (s["over"], s["over_right"]) == (1, 1)
    assert s["groups"]["interrupt"]["cut_ms"] == 550 + 550


def test_a_stand_in_run_reports_every_moment_right(tmp_path, capsys):
    out = tmp_path / "report.json"
    assert runner.main(["--mock", "--json-out", str(out)]) == 0
    report = json.loads(out.read_text())
    s = report["summary"]
    assert report["mock"] and s["over_right"] == s["over"] == 9
    assert s["jumped_in_short"] == 0 and s["missed_turns"] == 0
    md = capsys.readouterr().out
    assert md.startswith("# Turn-taking rig") and "Practice run" in md


# ---------- the app copy ----------

def test_the_copy_carries_the_built_page_only_when_asked(tmp_path):
    repo = tmp_path / "repo"
    (repo / "backend").mkdir(parents=True)
    (repo / "backend" / "app.py").write_text("")
    (repo / "config.json").write_text("{}")
    with pytest.raises(instance.InstanceError, match="isn't built"):
        instance.snapshot_code(tmp_path / "a", repo, frontend=True)
    (repo / "frontend" / "dist").mkdir(parents=True)
    (repo / "frontend" / "dist" / "index.html").write_text("<html>")
    code = instance.snapshot_code(tmp_path / "b", repo, frontend=True)
    assert (code / "frontend" / "dist" / "index.html").is_file()
    assert not (instance.snapshot_code(tmp_path / "c", repo) / "frontend").exists()


def test_the_judges_key_goes_only_where_it_is_allowed(tmp_path):
    keys = {"ELEVENLABS_API_KEY": "e", "ANTHROPIC_API_KEY": "a", "TYPESAFE_API_KEY": "t"}
    plain = instance.instance_env(8921, tmp_path, keys, owner="Alex", environ={})
    assert "TYPESAFE_API_KEY" not in plain
    turns = instance.instance_env(8921, tmp_path, keys, owner="Alex", environ={},
                                  allowed_keys=instance.KEYS + ("TYPESAFE_API_KEY",))
    assert turns["TYPESAFE_API_KEY"] == "t"


# ---------- what the rig reads from the page ----------

def test_every_line_and_button_the_rig_reads_still_exists():
    voice = (REPO / "frontend" / "src" / "voice.js").read_text()
    for tag in ("state", "vad:hold", "vad:bargeIn", "vad:letGo", "vad:words",
                "vad:finalize", "judge:verdict", "judge:none"):
        assert f"_vlog('{tag}'" in voice, tag
    assert "console.debug(`[voice] ${t}ms ${tag}`" in voice
    for route in ("/send", "/round/abort", "/api/voice/judge"):
        assert route.split("/")[-1] in INIT_JS
    header = (REPO / "frontend" / "src" / "components" / "ChatHeader.jsx").read_text()
    assert f"<Headphones size={{13}} /> {TALK}" in header
    assert f'aria-label="{END_LABEL}"' in "".join(
        p.read_text() for p in (REPO / "frontend" / "src").rglob("*.jsx"))
