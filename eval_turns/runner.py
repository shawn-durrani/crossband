"""CLI for the turn-taking rig.

    .venv/bin/python -m eval_turns --mock
    .venv/bin/python -m eval_turns --env ~/dev/crossband/.env
    .venv/bin/python -m eval_turns --moments pause,interrupt --keep

A measurement only. It starts its own crossband from a copy of this
checkout's code and built page, on its own port and data folder with
memory off, opens the real voice page in a headless Chromium with a
made-up microphone, plays made-up moments into it and times what the page
does. It never touches the running app, its data or membro."""

import argparse
import asyncio
import datetime
import json
import os
import sys
import tempfile
from pathlib import Path

from eval_turns import moments as moments_mod
from eval_turns import timeline

RIG = Path(__file__).resolve().parent
REPO = RIG.parent
DEFAULT_CACHE = REPO / "eval_voice" / "cache"
DEFAULT_PORT = 8921
KEYS = ("ELEVENLABS_API_KEY", "ANTHROPIC_API_KEY", "TYPESAFE_API_KEY")
TTS_PER_CHAR = 110.0 / 1_000_000     # the app's own voice rate card
STT_PER_HOUR = 0.40
OVER_AFTER_S = 2.0      # how far into a reply each moment starts talking
SETTLE_S = 6.0          # after a pause moment, time for the turn to be sent
OBSERVE_S = 6.0         # after talking over a reply, time for the decision
PAUSE_TITLE = "Rig: pauses"
OVER_TITLE = "Rig: talking over a reply"
SHORT_REPLY_CHARS = len("Sure.")


def say(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def build_arg_parser():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--moments", help="comma-separated moment ids, kinds (pause, "
                   "over) or groups (interrupt, backchannel, aside, background)")
    p.add_argument("--env", help="a .env file to take the keys from; default "
                   "the environment, then this checkout's .env")
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p.add_argument("--cache", default=str(DEFAULT_CACHE),
                   help="where rendered lines are kept, shared with the voice rig")
    p.add_argument("--pause", type=float, default=0.0,
                   help="the app's pause setting in seconds; default the app's own")
    p.add_argument("--level", type=float, default=moments_mod.SPEECH_DBFS,
                   help="how loud the words reach the page, dB of full scale "
                   f"measured on the speech; default {moments_mod.SPEECH_DBFS:g}")
    p.add_argument("--over-after", type=float, default=OVER_AFTER_S,
                   help="seconds into a reply each moment starts talking")
    p.add_argument("--reply-tts-model", default="",
                   help="the voice model the stand-in seat speaks with; "
                   "default this checkout's tts_model setting")
    p.add_argument("--repeat", type=int, default=1,
                   help="play every moment this many times, to see how steady "
                   "a borderline one is")
    p.add_argument("--keep", action="store_true",
                   help="keep the second app's data folder after the run")
    p.add_argument("--mock", action="store_true",
                   help="keyless stand-ins; for checking the rig, never numbers")
    p.add_argument("--format", choices=("markdown", "json"), default="markdown")
    p.add_argument("--out")
    p.add_argument("--json-out")
    return p


def read_keys(path=None, environ=None) -> dict:
    environ = os.environ if environ is None else environ
    if path:
        from dotenv import dotenv_values
        p = Path(os.path.expanduser(path))
        if not p.is_file():
            raise SystemExit(f"--env file not found: {p}")
        vals = dotenv_values(p)
        return {k: vals.get(k) or "" for k in KEYS}
    keys = {k: environ.get(k, "") for k in KEYS}
    if not keys["ELEVENLABS_API_KEY"] and (REPO / ".env").is_file():
        return read_keys(REPO / ".env")
    return keys


def default_reply_tts_model() -> str:
    try:
        from backend.config import load_settings
        return load_settings().tts_model or ""
    except Exception:
        return ""


# ---------- the run ----------

def _api(base):
    import httpx
    return httpx.Client(base_url=base, timeout=30.0, trust_env=False)


def set_up(client, seat_url, tts_model) -> dict:
    """The stand-in seat in Claude's place, two remembered people, and the
    two chats."""
    seats = client.get("/api/state").json().get("participants", [])
    claude = next((s for s in seats if s.get("slug") == "claude"), None)
    if claude is None:
        raise RuntimeError("the second app has no Claude seat to stand in for")
    patch = {"provider": "openai", "model": "rig-seat", "base_url": seat_url,
             "lifecycle": "onboarded"}
    if tts_model:
        patch["tts_model"] = tts_model
    r = client.patch(f"/api/participants/{claude['id']}", json=patch)
    r.raise_for_status()
    for name in ("Sam", "Dave"):
        client.post("/api/voice/people", json={"name": name})
    pauses = client.post("/api/chats", json={"title": PAUSE_TITLE,
                                              "participant_ids": []}).json()
    over = client.post("/api/chats", json={"title": OVER_TITLE,
                                            "participant_ids": [claude["id"]]}).json()
    return {"pauses": pauses["id"], "over": over["id"], "seat": claude["id"]}


def _tries(plan, kind, repeat):
    return [(m, k) for k in range(1, repeat + 1)
            for m in plan.moments if m.kind == kind]


def _tag(result, k, repeat):
    if repeat > 1:
        result["try"] = k
    return result


async def play_pauses(page, plan, clips, rate, repeat=1) -> list:
    out = []
    for m, k in _tries(plan, "pause", repeat):
        await page.wait_quiet()
        clip = clips[m.id]
        say(f"  {m.id}: {m.pause_s:g}s pause")
        started = await page.play(clip.pcm, rate)
        await asyncio.sleep(clip.seconds + SETTLE_S)
        until = started + (clip.seconds + SETTLE_S) * 1000
        out.append(_tag(timeline.pause(m, clip, started, await page.events(), until),
                        k, repeat))
    return out


async def play_overs(page, plan, clips, opening, rate, over_after, repeat=1) -> list:
    out = []
    for m, k in _tries(plan, "over", repeat):
        if not await page.wait_quiet(timeout=60):
            say(f"  {m.id}: the page didn't settle; going on anyway")
        clip = clips[m.id]
        say(f"  {m.id}: {m.group}")
        opened = await page.play(opening.pcm, rate)
        playing = await page.wait_playing(after=opened + opening.end_s * 1000)
        if playing is None:
            out.append(_tag({"id": m.id, "kind": "over", "group": m.group,
                             "should_cut": m.should_cut, "no_reply": True}, k, repeat))
            continue
        await page.sleep_until(playing + over_after * 1000
                               - clip.onset_s * 1000 - 50)
        over_at = await page.play(clip.pcm, rate)
        await asyncio.sleep(clip.seconds + OBSERVE_S)
        until = over_at + (clip.seconds + OBSERVE_S) * 1000
        out.append(_tag(timeline.over(m, clip, opening, opened, over_at,
                                      await page.events(), until), k, repeat))
    return out


async def run_live(args, plan, clips, opening, keys) -> tuple:
    from eval_turns.page import Page
    from eval_turns.seat import Seat
    from eval_voice.instance import KEYS as RIG_KEYS
    from eval_voice.instance import Instance

    run_dir = Path(tempfile.mkdtemp(prefix="run-", dir=Path(args.cache)))
    seat = Seat(plan.opening, plan.reply).start()
    inst = Instance(run_dir, port=args.port, keys=keys, owner=plan.opening_who,
                    calibrated=False, models_dir=Path(args.cache) / "voice_models",
                    frontend=True, allowed_keys=RIG_KEYS + ("TYPESAFE_API_KEY",))
    page = None
    results = []
    try:
        say(f"starting the second app on port {args.port}")
        inst.start()
        with _api(inst.base) as client:
            ids = set_up(client, seat.base_url,
                         args.reply_tts_model or default_reply_tts_model())
        page = await Page(inst.base, silence_s=args.pause).open()
        rate = moments_mod.RATE
        if any(m.kind == "pause" for m in plan.moments):
            say("pauses")
            await page.open_chat(PAUSE_TITLE)
            await page.start_voice()
            results += await play_pauses(page, plan, clips, rate, args.repeat)
            await page.stop_voice()
        if any(m.kind == "over" for m in plan.moments):
            say("talking over a reply")
            await page.open_chat(OVER_TITLE)
            await page.start_voice()
            results += await play_overs(page, plan, clips, opening, rate,
                                        args.over_after, args.repeat)
            await page.wait_quiet(timeout=60)
            await page.stop_voice()
        events = await page.events()
        if args.keep:
            (run_dir / "events.json").write_text(json.dumps(events))
    finally:
        if page is not None:
            await page.close()
        seat.stop()
        inst.stop()
        if args.keep:
            say(f"kept the second app's data in {inst.data_dir}")
        else:
            inst.remove(log_too=True)
    spent = {
        "reply_chars": sum(len(plan.reply) if r["long"] else SHORT_REPLY_CHARS
                           for r in seat.requests),
        "judge_calls": sum(1 for f in events["fetch"]
                           if f.get("path", "").endswith("/api/voice/judge")),
        "judge_on": bool(keys.get("TYPESAFE_API_KEY")),
        "ids": ids,
    }
    return results, spent


# ---------- the stand-in run ----------

def mock_events(plan, clips, opening) -> list:
    """What an ideal app would do with every moment, for checking the rig."""
    out = []
    for m in plan.moments:
        clip = clips[m.id]
        if m.kind == "pause":
            end = 1000 + clip.end_s * 1000
            ev = {"fetch": [{"t": end + 1650, "path": "/api/chats/1/send"}],
                  "log": [{"t": end + 1500, "tag": "vad:finalize", "data": {}}],
                  "media": []}
            out.append(timeline.pause(m, clip, 1000.0, ev, end + 6000))
            continue
        said = 1000 + opening.end_s * 1000
        playing = said + 2400
        over_at = playing + 2000 - clip.onset_s * 1000
        onset = over_at + clip.onset_s * 1000
        log = [{"t": onset + 520, "tag": "vad:hold", "data": {}}]
        if m.should_cut:
            log.append({"t": onset + 900, "tag": "vad:bargeIn", "data": {}})
        else:
            log.append({"t": onset + 1600, "tag": "vad:letGo", "data": {}})
        ev = {"fetch": [{"t": said + 1650, "path": "/api/chats/1/send"}],
              "log": log, "media": [{"t": playing, "ev": "playing", "dur": -1}]}
        out.append(timeline.over(m, clip, opening, 1000.0, over_at, ev,
                                 onset + 8000))
    return out


class _MockRenderer:
    """Speech-shaped tones instead of voices, one second a word or so."""
    def __init__(self):
        self.new_chars = self.cached_chars = 0

    def line(self, who, text):
        import numpy as np
        n = int(moments_mod.RATE * max(0.4, 0.28 * len(text.split())))
        t = np.arange(n) / moments_mod.RATE
        x = 0.3 * np.sin(2 * np.pi * 180 * t) * (0.6 + 0.4 * np.sin(2 * np.pi * 3 * t))
        return (x * 32767).astype("<i2").tobytes()

    def stats(self):
        return {"new_chars": 0, "cached_chars": 0, "new_lines": 0, "cached_lines": 0}


# ---------- the report ----------

def _ms(v):
    return "" if v is None else f"{v / 1000:.2f}s"


def _yes(v):
    return "yes" if v else "no"


def _name(r):
    return f"{r['id']} ({r['try']})" if r.get("try") else r["id"]


def cost(renderer_stats, spent, clips, plan) -> dict:
    """Each moment's audio is transcribed once, and each moment that talks
    over a reply asks the opening question first."""
    overs = sum(1 for m in plan.moments if m.kind == "over")
    stt_s = sum(clips[m.id].seconds for m in plan.moments) \
        + overs * clips["_opening"].seconds
    return {
        "lines_usd": round(renderer_stats.get("new_chars", 0) * TTS_PER_CHAR, 4),
        "replies_usd": round(spent.get("reply_chars", 0) * TTS_PER_CHAR, 4),
        "stt_usd": round(stt_s / 3600 * STT_PER_HOUR, 4),
        "reply_chars": spent.get("reply_chars", 0),
        "new_line_chars": renderer_stats.get("new_chars", 0),
    }


def render(report: dict) -> str:
    s = report["summary"]
    lines = ["# Turn-taking rig", ""]
    if report.get("mock"):
        lines += ["Practice run with stand-ins: the numbers are made up.", ""]
    lines += [f"Run {report['when']}, the app's pause setting "
              f"{report['pause_setting_s']:g}s, talking over each reply "
              f"{report['over_after_s']:g}s in, words at {report['level_dbfs']:g} dBFS.", ""]
    lines += ["## Pauses", "",
              f"Took a mid-sentence pause as the end of the turn in "
              f"{s['jumped_in_short']} of {s['short']} pauses shorter than the "
              f"setting, and {s['jumped_in_long']} of {s['long']} longer ones.",
              f"From your last word to the turn being sent: {_ms(s['send_ms'])} "
              "at the median.", "",
              "| moment | pause | noise | turns sent | ended early | to send |",
              "|---|---|---|---|---|---|"]
    for r in report["results"]:
        if r["kind"] != "pause":
            continue
        early = (f"yes, {_ms(r.get('cut_in_ms'))} into the pause"
                 if r["jumped_in"] else "no")
        lines.append(f"| {_name(r)} | {r['pause_s']:g}s | {r['noise'] or 'none'} "
                     f"| {r['turns']} | {early} | {_ms(r.get('send_ms'))} |")
    lines += ["", "## Talking over a reply", "",
              f"Right on {s['over_right']} of {s['over']}. From your last word "
              f"to the reply's first sound: {_ms(s['reply_ms'])} at the median, "
              "with a stand-in seat that answers in 0.3s.", "",
              "| kind | moments | right | paused | cut | to pause | to cut |",
              "|---|---|---|---|---|---|---|"]
    for g in ("interrupt", "backchannel", "aside", "background"):
        st = s["groups"].get(g)
        if st:
            lines.append(f"| {g} | {st['moments']} | {st['right']} | {st['held']} "
                         f"| {st['cut']} | {_ms(st['hold_ms'])} | {_ms(st['cut_ms'])} |")
    late = sum(bool(r.get("late_cut")) for r in report["results"])
    if late:
        lines += ["", f"{late} {'cut' if late == 1 else 'cuts'} came late: the "
                  "reply was let go, then the words arrived and cut it. The cut "
                  "time is when that happened."]
    lines += ["", "| moment | kind | should cut | paused | cut | resumed | words | judge | right |",
              "|---|---|---|---|---|---|---|---|---|"]
    for r in report["results"]:
        if r["kind"] != "over":
            continue
        if r.get("no_reply"):
            lines.append(f"| {_name(r)} | {r['group']} | {_yes(r['should_cut'])} "
                         "| no reply was playing | | | | | |")
            continue
        lines.append(
            f"| {_name(r)} | {r['group']} | {_yes(r['should_cut'])} "
            f"| {_ms(r.get('hold_ms')) if r['held'] else 'no'} "
            f"| {_ms(r.get('cut_ms')) if r['cut'] else 'no'} "
            f"| {_ms(r.get('resume_ms')) if r['resumed'] else 'no'} "
            f"| {r['words'] or ''} | {r['judge'] or ''} | {_yes(r['right'])} |")
    c = report["cost"]
    lines += ["", "## What it cost", "",
              f"- New lines spoken for the moments: {c['new_line_chars']} characters, "
              f"${c['lines_usd']:.2f}. They're cached after the first run.",
              f"- The stand-in seat's replies, spoken by the app: {c['reply_chars']} "
              f"characters, ${c['replies_usd']:.2f}.",
              f"- Transcribing the moments: about ${c['stt_usd']:.2f}.",
              f"- The judge was {'on' if report['judge_on'] else 'off, with no TYPESAFE_API_KEY'}"
              f", asked {report['judge_calls']} times."]
    return "\n".join(lines) + "\n"


def build_report(results, plan, clips, renderer_stats, spent, args, mock=False) -> dict:
    pause_setting = args.pause or 1.5
    summary = timeline.summarise(results, pause_setting)
    return {
        "when": datetime.datetime.now().isoformat(timespec="seconds"),
        "mock": mock, "pause_setting_s": pause_setting,
        "over_after_s": args.over_after, "level_dbfs": args.level,
        "summary": summary, "results": results,
        "judge_on": spent.get("judge_on", False),
        "judge_calls": spent.get("judge_calls", 0),
        "cost": cost(renderer_stats, spent, clips, plan),
    }


def main(argv=None) -> int:
    args = build_arg_parser().parse_args(argv)
    plan = moments_mod.load(only=args.moments)
    if args.mock:
        renderer = _MockRenderer()
    else:
        from eval_voice.render import Renderer, elevenlabs_fetch
        keys = read_keys(args.env)
        if not keys["ELEVENLABS_API_KEY"]:
            raise SystemExit("ELEVENLABS_API_KEY is needed to speak the lines "
                             "and run the voice page")
        Path(args.cache).mkdir(parents=True, exist_ok=True)
        renderer = Renderer(args.cache, elevenlabs_fetch(keys["ELEVENLABS_API_KEY"]))
    say(f"{len(plan.moments)} moments; speaking any new lines")
    clips = {m.id: moments_mod.clip_for(m, renderer, args.level) for m in plan.moments}
    opening = moments_mod.opening_clip(plan, renderer, args.level)
    clips["_opening"] = opening
    if args.mock:
        results = mock_events(plan, clips, opening)
        spent = {"reply_chars": 0, "judge_calls": 0, "judge_on": False}
    else:
        results, spent = asyncio.run(run_live(args, plan, clips, opening, keys))
    report = build_report(results, plan, clips, renderer.stats(), spent, args,
                          mock=args.mock)
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(report, indent=2))
    out = json.dumps(report, indent=2) if args.format == "json" else render(report)
    if args.out:
        Path(args.out).write_text(out)
        say(f"wrote {args.out}")
    else:
        sys.stdout.write(out)
    return 0
