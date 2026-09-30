"""CLI for the voice rig.

    .venv/bin/python -m eval_voice --mock
    .venv/bin/python -m eval_voice --mix-only
    .venv/bin/python -m eval_voice --env ~/dev/crossband/.env
    .venv/bin/python -m eval_voice --scripts two-voices,introductions --keep
    .venv/bin/python -m eval_voice --generate 2 --with crosstalk,room

A measurement only. It starts its own crossband from a copy of this
checkout's code, on its own port and data folder with memory off, plays
made-up conversations into it, and scores what it wrote. It never
touches the running app, its data or membro."""

import argparse
import asyncio
import datetime
import json
import os
import sys
from pathlib import Path

from eval_voice import cast as cast_mod
from eval_voice import mix as mix_mod
from eval_voice import scoring
from eval_voice import script as script_mod
from eval_voice.beds import BedError
from eval_voice.report import render_markdown

RIG = Path(__file__).resolve().parent
REPO = RIG.parent
DEFAULT_CACHE = RIG / "cache"
TTS_PER_CHAR = 110.0 / 1_000_000     # the app's own voice rate card
STT_PER_HOUR = 0.40
# The second app's voice sessions end after this much quiet, where yours
# wait ten minutes, so each conversation's end-of-session pass is scored.
# Long enough that no pause inside a conversation ends a session.
SESSION_IDLE_S = 45.0
END_PASS_MARGIN_S = 60.0


def say(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--scripts", help="comma-separated script ids; default all")
    p.add_argument("--scripts-dir", action="append", default=[],
                   help="more scripts, such as the ones --generate wrote")
    p.add_argument("--no-builtin-scripts", action="store_true")
    p.add_argument("--enrol", help="who has a voice bank before the run, "
                   f"default {','.join(cast_mod.DEFAULT_ENROLLED)}")
    p.add_argument("--env", help="a .env file to take ELEVENLABS_API_KEY and "
                   "ANTHROPIC_API_KEY from; default this checkout's .env")
    p.add_argument("--port", type=int, default=8920)
    p.add_argument("--diariser", help="the diariser URL; default the "
                   "diarize_shadow_url in this checkout's config.local.json")
    p.add_argument("--no-diariser", action="store_true",
                   help="name each turn on its own, with no tracking session")
    p.add_argument("--no-calibrated", action="store_true",
                   help="leave the calibrated scorer off")
    p.add_argument("--speed", type=float, default=1.0,
                   help="how fast to play the audio; 1 is real time")
    p.add_argument("--session-idle", type=float, default=SESSION_IDLE_S,
                   help="seconds of quiet that end the second app's voice "
                   "session and run its end-of-session pass; 0 keeps the "
                   "app's ten minutes and leaves the pass unscored")
    p.add_argument("--tts-model", default="eleven_multilingual_v2")
    p.add_argument("--cache", default=str(DEFAULT_CACHE))
    p.add_argument("--keep", action="store_true",
                   help="keep the instance's data folder after the run")
    p.add_argument("--mix-only", action="store_true",
                   help="render and mix, write the audio and truth, no app")
    p.add_argument("--mock", action="store_true",
                   help="keyless stand-ins; for checking the rig, never numbers")
    p.add_argument("--generate", type=int, metavar="N",
                   help="write N new scripts with the utility model and stop")
    p.add_argument("--people", default="Alex,Sam,Dave",
                   help="who is in a generated script")
    p.add_argument("--with", dest="features", default="crosstalk,quiet",
                   help="what a generated script includes")
    p.add_argument("--turns", type=int, default=8)
    p.add_argument("--show-words", action="store_true",
                   help="put what the transcriber heard in the report")
    p.add_argument("--format", choices=("markdown", "json"), default="markdown")
    p.add_argument("--out")
    p.add_argument("--json-out", help="also write the whole report as JSON here")
    return p


def read_keys(path=None) -> dict:
    """The two keys the rig needs, from a .env file or the environment.
    Nothing else in the file is read into this process."""
    from dotenv import dotenv_values
    if path and not Path(os.path.expanduser(path)).is_file():
        raise SystemExit(f"--env file not found: {path}")
    path = Path(os.path.expanduser(path)) if path else REPO / ".env"
    found = dotenv_values(path) if path.is_file() else {}
    return {k: found.get(k) or os.environ.get(k) or ""
            for k in ("ELEVENLABS_API_KEY", "ANTHROPIC_API_KEY")}


def default_diariser(repo: Path = REPO) -> str:
    """The one setting the rig reads from this checkout's local config: where
    the diariser is. Nothing else in the file is used."""
    try:
        local = json.loads((repo / "config.local.json").read_text())
    except (OSError, ValueError):
        return ""
    return str(local.get("diarize_shadow_url") or "")


def write_mix(cache: Path, script_id: str, turns: list) -> Path:
    pcm, timeline = mix_mod.conversation(turns)
    out = cache / "mixes"
    out.mkdir(parents=True, exist_ok=True)
    wav = out / f"{script_id}.wav"
    wav.write_bytes(mix_mod.wav_bytes(pcm))
    (out / f"{script_id}.truth.json").write_text(json.dumps(
        {"script": script_id, "timeline": timeline,
         "turns": [mt.truth.to_dict() for mt in turns]}, indent=2))
    return wav


def score_run(results: dict, mixed: dict) -> tuple:
    rows, events = [], []
    diag = {"methods": {}, "errors": {}, "sessions": 0, "sessions_closed": 0,
            "sessions_ended": 0, "relay_errors": 0, "people_met": 0,
            "forgotten": 0,
            "end_pass": {"sessions": 0, "ended": 0, "relabelled": 0,
                         "voices_renamed": 0, "errors": 0, "why": {}}}
    for sid, result in results.items():
        after = result.after_end or [None] * len(result.heard)
        for mt, heard, later in zip(mixed[sid], result.heard, after):
            row = scoring.judge(mt.truth, heard)
            row["method"] = (result.diagnostics.get("per_turn_method") or {}) \
                .get(mt.truth.index, "")
            row["transcript"] = heard.transcript
            if later is not None:
                again = scoring.judge(mt.truth, later)
                row["after_end"] = {k: again[k] for k in
                                    ("verdict", "named", "reason")}
            rows.append(row)
        for e in result.events:
            e.script = e.script or sid
        events += result.events
        d = result.diagnostics
        for key in ("methods", "errors"):
            for k, v in (d.get(key) or {}).items():
                diag[key][k] = diag[key].get(k, 0) + v
        for key in ("sessions", "sessions_closed", "sessions_ended",
                    "relay_errors", "people_met", "forgotten"):
            diag[key] += d.get(key) or 0
        ep = d.get("end_pass") or {}
        for key in ("sessions", "ended", "relabelled", "voices_renamed",
                    "errors"):
            diag["end_pass"][key] += ep.get(key) or 0
        for k, v in (ep.get("why") or {}).items():
            diag["end_pass"]["why"][k] = diag["end_pass"]["why"].get(k, 0) + v
    return rows, events, diag


def generate_scripts(args, cache: Path) -> int:
    from backend.config import Settings
    from backend.llm_util import price_utility_call, utility_complete_with_usage
    from eval_voice import generate as gen
    keys = read_keys(args.env)
    if keys["ANTHROPIC_API_KEY"]:
        os.environ["ANTHROPIC_API_KEY"] = keys["ANTHROPIC_API_KEY"]
    cfg = Settings().as_cfg()
    model = cfg.get("utility_model") or "claude-haiku-4-5"
    people = [p.strip() for p in args.people.split(",") if p.strip()]
    features = [f.strip() for f in args.features.split(",") if f.strip()]
    spent = [0.0]

    async def caller(prompt):
        done = await utility_complete_with_usage(prompt, cfg, max_tokens=2500,
                                                 model=model, timeout=60)
        if done.text is None:
            raise SystemExit("no reply from the utility model; is "
                             "ANTHROPIC_API_KEY set?")
        spent[0] += price_utility_call(model, done, cfg)[0] or 0.0
        return done.text

    out = cache / "scripts"
    out.mkdir(parents=True, exist_ok=True)
    for _ in range(args.generate):
        s = asyncio.run(gen.generate(caller, people, args.turns, features))
        path = out / f"{s.id}.json"
        path.write_text(json.dumps(s.to_dict(), indent=2) + "\n")
        say(f"wrote {path}")
    say(f"about ${spent[0]:.3f} on the utility model; run them with "
        f"--scripts-dir {out}")
    return 0


def main(argv=None) -> int:
    args = build_arg_parser().parse_args(argv)
    cache = Path(args.cache)
    if args.generate:
        return generate_scripts(args, cache)
    scripts = script_mod.load_scripts(
        dirs=args.scripts_dir, include_builtin=not args.no_builtin_scripts,
        only=[s.strip() for s in args.scripts.split(",")] if args.scripts else None)
    enrolled = ([n.strip() for n in args.enrol.split(",") if n.strip()]
                if args.enrol else list(cast_mod.DEFAULT_ENROLLED))
    unknown = [n for n in enrolled if n not in cast_mod.CAST]
    if unknown:
        raise SystemExit(f"{unknown} aren't on the synthetic roster")

    keys = {} if args.mock else read_keys(args.env)
    if args.mock:
        from eval_voice.mock import MockBeds, MockRenderer
        renderer, beds = MockRenderer(), MockBeds()
    else:
        from eval_voice import beds as beds_mod
        from eval_voice.render import Renderer, elevenlabs_fetch
        key = keys.get("ELEVENLABS_API_KEY")
        renderer = Renderer(cache, fetch=elevenlabs_fetch(key) if key else None,
                            model=args.tts_model)
        beds = beds_mod.Beds(cache, fetch=beds_mod.elevenlabs_fetch(key)
                             if key else None)

    say(f"mixing {len(scripts)} scripts")
    mixed, skipped = {}, {}
    for s in scripts:
        try:
            mixed[s.id] = mix_mod.mix_script(s, renderer, enrolled, beds)
        except BedError as e:
            # A bed nobody can make leaves its script out, and says so,
            # so the rest of the run still counts.
            skipped[s.id] = str(e)
            say(f"skipping {s.id}: {e}")
    scripts = [s for s in scripts if s.id in mixed]
    if not scripts:
        raise SystemExit("no script could be mixed")
    voices = {n: mix_mod.enrol_audio(renderer, n, cast_mod.ENROL_PASSAGES[n])
              for n in enrolled}
    if not args.mock:
        for sid, turns in mixed.items():
            write_mix(cache, sid, turns)
    if args.mix_only:
        say(f"wrote the mixes and their truth under {cache / 'mixes'}; "
            f"{renderer.new_chars} characters rendered new, "
            f"{beds.stats()['new_seconds']:g} seconds of bed made new")
        return 0

    instance = None
    diariser = "" if args.no_diariser else (args.diariser or default_diariser())
    if args.mock:
        from eval_voice.mock import MockAdapter
        adapter = MockAdapter()
        described = ""
    else:
        from eval_voice.crossband import CrossbandAdapter
        from eval_voice.instance import Instance
        if not keys.get("ELEVENLABS_API_KEY"):
            raise SystemExit("the app needs ELEVENLABS_API_KEY to transcribe; "
                             "pass --env")
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        idle = max(0.0, args.session_idle or 0.0)
        instance = Instance(cache / "runs" / stamp, port=args.port, keys=keys,
                            owner=cast_mod.OWNER, diariser=diariser,
                            calibrated=not args.no_calibrated,
                            session_idle_s=idle,
                            models_dir=cache / "voice_models")
        say(f"starting an isolated crossband on port {args.port}")
        instance.start()
        adapter = CrossbandAdapter(
            instance.base, diariser=diariser,
            calibrated=not args.no_calibrated, speed=args.speed,
            end_pass_wait_s=idle + END_PASS_MARGIN_S if idle else 0.0,
            say=say)
        pace = ("in real time" if args.speed == 1
                else f"at {args.speed:g} times real time")
        ending = (f"voice sessions ending after {idle:g} quiet seconds"
                  if idle and diariser else "no end-of-session pass scored")
        described = (f"Calibrated scorer {'off' if args.no_calibrated else 'on'}, "
                     f"diariser {'on' if diariser else 'off'}, played {pace}, "
                     f"{ending}, {cast_mod.OWNER} as the owner, "
                     f"{', '.join(enrolled)} recorded before the run.")
    results, closing, finished = {}, {}, False
    try:
        say("recording each person's voice")
        enrol_notes = adapter.enrol(voices)
        for s in scripts:
            say(f"playing {s.id} ({len(mixed[s.id])} turns)")
            results[s.id] = adapter.converse(s.id, mixed[s.id])
        closing = adapter.close()
        finished = True
    finally:
        if instance is not None:
            instance.stop()
            if finished and not args.keep:
                instance.remove(log_too=True)
            else:
                say(f"kept the run folder {instance.run_dir}, with the "
                    f"second app's log in {instance.log_path.name}")

    rows, events, diag = score_run(results, mixed)
    tts = renderer.stats()
    tts["usd"] = round(tts["new_chars"] * TTS_PER_CHAR, 4)
    stt_s = getattr(adapter, "stt_seconds", None)
    cost = {"tts": tts, "beds": beds.stats(), "stt_seconds": stt_s,
            "stt_usd": round((stt_s or 0) / 3600 * STT_PER_HOUR, 4),
            "app": closing}
    report = scoring.aggregate(rows, events, cost)
    report["skipped"] = skipped
    report["setup"] = {"adapter": adapter.name, "enrol": enrol_notes,
                       "described": described, "diariser": bool(diariser)}
    report["diagnostics"] = diag
    if not args.show_words:
        for r in report["rows"]:
            r.pop("transcript", None)
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(report, indent=2, default=str))
    if args.format == "json":
        out = json.dumps(report, indent=2, default=str)
    else:
        out = render_markdown(report, args.mock, args.show_words)
    if args.out:
        Path(args.out).write_text(out)
        say(f"wrote {args.out}")
    else:
        sys.stdout.write(out)
    return 0
