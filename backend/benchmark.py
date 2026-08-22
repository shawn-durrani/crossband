"""Synthetic voice benchmark (#94): identical scripted cases through a chosen
set of seats, stage timings compared side by side.

Why synthetic and minimal: a real turn's latency depends on transcript length,
memory, tools and prompt caches, so two seats never see the same input. The
benchmark strips all of that - one fixed prompt, no history, no tools, no
system prompt beyond a seat's own thinking-control hint. The numbers answer
"how do these seats compare on the same tiny job", not "how fast is my next
real turn", and every result file says so.

Non-interactive by design: no microphone capture, no audio playback. The
speech legs run on fixtures - a fixed spoken-prompt clip for speech-to-text,
a fixed sentence for synthesis - and generated audio is kept on disk for
human listening instead of being scored automatically.

The spoken fixture is generated once through a configured TTS voice and
cached under data/benchmarks/fixtures/ with its provenance beside it. Drop in
your own spoken-prompt.mp3 (plus spoken-prompt.json carrying {"sentence":
...}) to replace it. Fixtures carry no conversation content and results carry
no key values; tests pin both.

Units run strictly sequentially - one call in flight, ever - so a timing
never includes self-inflicted contention. A big selection is therefore slow,
and the UI says so up front.

What a seat's text call sends: the seat's own reasoning_effort and
thinking_control, exactly as a live turn would (they dominate first-word
latency), on the same adapter choice - Responses first for OpenAI-style
seats, with the #144 chat-completions fallback for endpoints that lack the
route. Voice spend from a run appears in the ElevenLabs quota, not on the
per-chat spend page: there is no chat to bill it to, so each unit carries
its own estimated cost instead.
"""

import asyncio
import json
import logging
import os
import re
import shutil
import time
from pathlib import Path

from . import db, providers, voice

log = logging.getLogger("uvicorn.error")

DIMENSIONS = ("text", "stt", "tts", "pipeline")

DIMENSION_LABELS = {
    "text": "Text reply (latency + metadata)",
    "stt": "Speech-to-text (spoken fixture)",
    "tts": "Text-to-speech (fixed sentence, per voice)",
    "pipeline": "Full pipeline (listen, think, speak)",
}

# Scripted text cases. Fixed strings only - never conversation content.
CASES = {
    "echo": {
        "label": "Instruction echo",
        "prompt": "Reply with exactly these three words: benchmark reply received",
        "expect": "benchmark reply received",
    },
    "arithmetic": {
        "label": "Small arithmetic",
        "prompt": "What is 17 multiplied by 23? Reply with the number only.",
        "expect": "391",
    },
    "speakable": {
        "label": "One-sentence explainer",
        "prompt": "In one short sentence, why does the sky look blue in the "
                  "middle of the day?",
        "expect": None,  # judged by a human from the retained reply
    },
}

# The spoken fixture's sentence doubles as a sensible model prompt, so the
# pipeline leg can feed the transcript straight in. Plain words, no digits -
# the match check is then just lowercase-and-strip-punctuation.
FIXTURE_SENTENCE = ("Please tell me in one short sentence why the sky looks "
                    "blue in the middle of the day.")
FIXTURE_FILE = "spoken-prompt.mp3"
FIXTURE_META = "spoken-prompt.json"

# The fixed response fixture the TTS dimension synthesises for every voice.
TTS_SENTENCE = ("Here is the answer you asked for, spoken aloud so you can "
                "judge this voice.")

MAX_TOKENS = 1024        # guard, not a target - every case asks for brevity
CALL_TIMEOUT_S = 180.0   # a cold local model pages in; a wedged one must fail
TTS_MAX_CHARS = 400      # pipeline replies are truncated for synthesis, flagged
REPLY_KEEP_CHARS = 500   # retained reply text per unit, for human judgement
LIST_LIMIT = 30

RUN_ID_RE = re.compile(r"^bench-\d{8}-\d{6}(-\d+)?$")
ARTEFACT_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,80}$")

# The one active run, keyed by run id, holding the LIVE results dict that
# progress polling reads. One entry at most: overlapping runs would time each
# other's contention, so the router refuses a second start with a 409.
_active: dict = {}


# ---------- paths ----------

def runs_root() -> Path:
    return Path(db.DATA_DIR) / "benchmarks" / "runs"


def fixtures_root() -> Path:
    return Path(db.DATA_DIR) / "benchmarks" / "fixtures"


def new_run_id() -> str:
    base = time.strftime("bench-%Y%m%d-%H%M%S")
    run_id, n = base, 2
    while (runs_root() / run_id).exists():
        run_id = f"{base}-{n}"
        n += 1
    return run_id


# ---------- pure helpers ----------

def normalise(text: str) -> str:
    """Lowercase, punctuation stripped, whitespace collapsed - the comparison
    space for 'did the reply/transcript say the expected words'."""
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def seat_public(row: dict) -> dict:
    """The seat fields a result file may carry: configuration identity only.
    api_key_env is the NAME of an environment variable (the UI already treats
    it as name-only); key VALUES never appear anywhere in a result."""
    return {k: row.get(k) or "" for k in (
        "slug", "name", "provider", "model", "base_url", "api_key_env",
        "voice_id", "reasoning_effort", "thinking_control")}


def build_plan(body: dict, participants: list, eleven_on: bool):
    """Validate a run request against the live roster. Returns (plan, error) -
    exactly one is None. Pure: the router passes the roster and key state in."""
    dims = [d for d in (body.get("dimensions") or []) if d in DIMENSIONS]
    if not dims:
        return None, "pick at least one test dimension"
    if sorted(set(dims)) != sorted(dims):
        dims = list(dict.fromkeys(dims))
    cases = [c for c in (body.get("cases") or []) if c in CASES]
    if "text" in dims and not cases:
        return None, "the text dimension needs at least one case"
    if "text" not in dims:
        cases = []
    wanted = [s for s in (body.get("models") or []) if isinstance(s, str)]
    by_slug = {p["slug"]: p for p in participants if p.get("enabled")}
    missing = [s for s in wanted if s not in by_slug]
    if missing:
        return None, f"unknown or disabled seat: {', '.join(missing)}"
    seats = [seat_public(by_slug[s]) for s in wanted]
    if not seats:
        return None, "pick at least one model"
    fixture_voice = next((s["voice_id"] for s in seats if s["voice_id"]), "")
    return {"models": seats, "cases": cases, "dimensions": dims,
            "eleven": bool(eleven_on), "fixture_voice": fixture_voice}, None


def plan_total(plan: dict) -> int:
    dims, n = plan["dimensions"], len(plan["models"])
    total = 0
    if "text" in dims:
        total += n * len(plan["cases"])
    if "stt" in dims:
        total += 1  # speech-to-text has no model axis; it runs once
    if "tts" in dims:
        total += n
    if "pipeline" in dims:
        total += n
    return total


def voice_support(plan: dict, seat: dict) -> str:
    """Why this seat cannot run a synthesis leg, or '' when it can."""
    if not plan["eleven"]:
        return "ElevenLabs is not configured"
    if not seat["voice_id"]:
        return "no voice configured for this seat"
    return ""


# ---------- effectful call points (tests monkeypatch these) ----------

def stt_call(audio: bytes, mime: str, cfg: dict):
    return voice.transcribe(audio, mime, cfg)


def tts_call(text: str, voice_id: str, cfg: dict) -> bytes:
    return voice.synthesize(text, voice_id, cfg)


async def call_model(seat: dict, prompt: str, cfg: dict) -> dict:
    """One minimal streaming completion against this seat: a single user turn,
    bounded output, the seat's own reasoning/thinking settings. Returns text,
    first-visible-word and total seconds, token count when the provider sends
    one, and which adapter leg answered. Raises on failure - the unit wrapper
    turns that into a 'failed' result with the provider's own words."""
    if seat["provider"] == "anthropic":
        return await _call_anthropic(seat, prompt)
    base_url = (seat.get("base_url") or "").strip()
    client = providers._openai_client(seat)
    if not (base_url and base_url in providers._chat_completions_only):
        try:
            return await _call_openai_responses(client, seat, prompt)
        except Exception:
            # Mirror #144 cheaply: an endpoint with a base_url that fails the
            # Responses route gets one chat-completions retry. A genuinely
            # broken seat fails there too, and THAT error is the one reported.
            if not base_url:
                raise
    return await _call_openai_chat(client, seat, prompt)


async def _call_anthropic(seat: dict, prompt: str) -> dict:
    client = providers._anthropic_client(seat)
    kwargs = dict(model=seat["model"], max_tokens=MAX_TOKENS, stream=True,
                  messages=[{"role": "user", "content": prompt}],
                  timeout=CALL_TIMEOUT_S)
    effort = providers._anthropic_effort(seat)
    if effort:
        kwargs["output_config"] = {"effort": effort}
    thinking = providers._anthropic_thinking(seat)
    if thinking:
        kwargs["thinking"] = thinking
    t0 = time.monotonic()
    ttfb, parts, out_tokens = None, [], 0
    stream = await client.messages.create(**kwargs)
    async for ev in stream:
        if ev.type == "content_block_delta":
            text = getattr(ev.delta, "text", None)
            if text:
                if ttfb is None:
                    ttfb = time.monotonic() - t0
                parts.append(text)
        elif ev.type == "message_delta":
            out_tokens = getattr(ev.usage, "output_tokens", 0) or out_tokens
    return {"text": "".join(parts), "ttfb_s": ttfb,
            "total_s": time.monotonic() - t0,
            "output_tokens": out_tokens, "leg": "anthropic"}


async def _call_openai_responses(client, seat: dict, prompt: str) -> dict:
    kwargs = dict(model=seat["model"], stream=True, store=False,
                  input=[{"role": "user", "content": prompt}],
                  max_output_tokens=MAX_TOKENS, timeout=CALL_TIMEOUT_S)
    effort = providers._openai_effort(seat)
    if effort:
        kwargs["reasoning"] = {"effort": effort}
    t0 = time.monotonic()
    ttfb, parts, out_tokens = None, [], 0
    stream = await client.responses.create(**kwargs)
    async for ev in stream:
        if ev.type == "response.output_text.delta" and ev.delta:
            if ttfb is None:
                ttfb = time.monotonic() - t0
            parts.append(ev.delta)
        elif ev.type == "response.completed":
            usage = getattr(ev.response, "usage", None)
            out_tokens = getattr(usage, "output_tokens", 0) or 0
    return {"text": "".join(parts), "ttfb_s": ttfb,
            "total_s": time.monotonic() - t0,
            "output_tokens": out_tokens, "leg": "responses"}


async def _call_openai_chat(client, seat: dict, prompt: str) -> dict:
    messages = [{"role": "user", "content": prompt}]
    hint = providers.thinking_prompt_hint(seat)
    if hint:
        messages.insert(0, {"role": "system", "content": hint})
    kwargs = dict(model=seat["model"], messages=messages, stream=True,
                  max_tokens=MAX_TOKENS, timeout=CALL_TIMEOUT_S)
    extra = providers.thinking_extra_body(seat)
    if extra:
        kwargs["extra_body"] = extra
    t0 = time.monotonic()
    ttfb, parts, out_tokens = None, [], 0
    stream = await client.chat.completions.create(**kwargs)
    async for chunk in stream:
        choices = getattr(chunk, "choices", None) or []
        delta = choices[0].delta.content if choices else None
        if delta:
            if ttfb is None:
                ttfb = time.monotonic() - t0
            parts.append(delta)
        usage = getattr(chunk, "usage", None)  # final chunk, when sent (#144)
        if usage:
            out_tokens = getattr(usage, "completion_tokens", 0) or out_tokens
    return {"text": "".join(parts), "ttfb_s": ttfb,
            "total_s": time.monotonic() - t0,
            "output_tokens": out_tokens, "leg": "chat_completions"}


# ---------- fixtures ----------

async def ensure_fixture(plan: dict, cfg: dict):
    """The spoken-prompt clip for the listening legs. Returns (fixture, why) -
    fixture is {file, sentence, ...provenance} or None with a plain reason.
    An existing clip is reused as-is; one supplied by hand without a meta file
    still runs, with no reference sentence to match against."""
    root = fixtures_root()
    clip, meta = root / FIXTURE_FILE, root / FIXTURE_META
    if clip.exists():
        info = {}
        try:
            info = json.loads(meta.read_text())
        except (OSError, json.JSONDecodeError):
            info = {"supplied": True}
        info.setdefault("sentence", "")
        info["file"] = FIXTURE_FILE
        return info, ""
    if not plan["eleven"]:
        return None, "ElevenLabs is not configured"
    if not plan["fixture_voice"]:
        return None, ("no seat in this run has a voice, so there is nothing "
                      "to generate the spoken fixture with")
    try:
        audio = await asyncio.to_thread(
            tts_call, FIXTURE_SENTENCE, plan["fixture_voice"], cfg)
    except Exception as e:
        return None, f"could not generate the spoken fixture: {e}"
    root.mkdir(parents=True, exist_ok=True)
    clip.write_bytes(audio)
    info = {"file": FIXTURE_FILE, "sentence": FIXTURE_SENTENCE,
            "synthetic": True, "voice_id": plan["fixture_voice"],
            "tts_model": cfg.get("tts_model") or "",
            "created_at": _iso_now()}
    meta.write_text(json.dumps(info, indent=1))
    return info, ""


# ---------- the run ----------

def _iso_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _write(run_dir: Path, results: dict):
    tmp = run_dir / "results.json.tmp"
    tmp.write_text(json.dumps(results, indent=1))
    os.replace(tmp, run_dir / "results.json")


def start_run(plan: dict, cfg: dict) -> str:
    """Register and spawn one run. Caller (the router) has already refused a
    start while _active is non-empty; this just wires the pieces."""
    from . import engine
    run_id = new_run_id()
    results = _fresh_results(run_id, plan, cfg)
    _active[run_id] = results
    engine.spawn(run_benchmark(run_id, plan, cfg, results))
    return run_id


def _fresh_results(run_id: str, plan: dict, cfg: dict) -> dict:
    return {
        "run_id": run_id,
        "created_at": _iso_now(),
        "created_at_unix": time.time(),
        "synthetic": True,
        "note": ("Synthetic benchmark: minimal scripted calls outside any "
                 "chat, not live-turn measurements."),
        "state": "running",
        "progress": {"done": 0, "total": plan_total(plan)},
        "config": {
            "dimensions": plan["dimensions"],
            "cases": plan["cases"],
            "tts_model": cfg.get("tts_model") or "",
            "stt_model": cfg.get("stt_model") or "scribe_v2",
            "tts_sentence": TTS_SENTENCE,
        },
        "models": plan["models"],
        "case_details": {c: {"label": CASES[c]["label"],
                             "prompt": CASES[c]["prompt"],
                             "expect": CASES[c]["expect"]}
                         for c in plan["cases"]},
        "fixture": None,
        "text": {}, "stt": None, "tts": {}, "pipeline": {},
    }


async def run_benchmark(run_id: str, plan: dict, cfg: dict, results=None):
    """Execute one run sequentially, writing results.json after every unit so
    an interrupted run leaves everything it measured."""
    run_dir = runs_root() / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    if results is None:  # direct callers (tests); start_run passes the live dict
        results = _active.setdefault(run_id, _fresh_results(run_id, plan, cfg))
    dims = plan["dimensions"]
    try:
        fixture, no_fixture = None, ""
        if "stt" in dims or "pipeline" in dims:
            fixture, no_fixture = await ensure_fixture(plan, cfg)
            results["fixture"] = fixture or {"missing": no_fixture}
            _write(run_dir, results)
        if "stt" in dims:
            results["stt"] = await _stt_unit(fixture, no_fixture, cfg)
            _bump(results, run_dir)
        for seat in plan["models"]:
            slug = seat["slug"]
            if "text" in dims:
                per_case = results["text"].setdefault(slug, {})
                for case_id in plan["cases"]:
                    per_case[case_id] = await _text_unit(seat, case_id, cfg)
                    _bump(results, run_dir)
            if "tts" in dims:
                results["tts"][slug] = await _tts_unit(seat, plan, cfg, run_dir)
                _bump(results, run_dir)
            if "pipeline" in dims:
                results["pipeline"][slug] = await _pipeline_unit(
                    seat, plan, fixture, no_fixture, cfg, run_dir)
                _bump(results, run_dir)
        results["state"] = "done"
    except Exception as e:  # the runner itself must never strand "running"
        log.exception("benchmark run %s crashed", run_id)
        results["state"] = "failed"
        results["error"] = str(e)[:500]
    finally:
        results["finished_at"] = _iso_now()
        _write(run_dir, results)
        _active.pop(run_id, None)
    return results


def _bump(results: dict, run_dir: Path):
    results["progress"]["done"] += 1
    _write(run_dir, results)


async def _text_unit(seat: dict, case_id: str, cfg: dict) -> dict:
    case = CASES[case_id]
    try:
        r = await call_model(seat, case["prompt"], cfg)
    except Exception as e:
        return {"status": "failed", "error": str(e)[:300]}
    expect = case["expect"]
    return {
        "status": "ok",
        "seconds": round(r["total_s"], 3),
        "first_word_s": round(r["ttfb_s"], 3) if r["ttfb_s"] is not None else None,
        "output_tokens": r["output_tokens"] or None,
        "chars": len(r["text"]),
        "leg": r["leg"],
        "reply": r["text"][:REPLY_KEEP_CHARS],
        "matches_expected": (normalise(r["text"]) == normalise(expect)
                             if expect else None),
    }


async def _stt_unit(fixture, no_fixture: str, cfg: dict) -> dict:
    if fixture is None:
        return {"status": "unsupported", "reason": no_fixture}
    try:
        audio = (fixtures_root() / fixture["file"]).read_bytes()
        t0 = time.monotonic()
        transcript, model_id = await asyncio.to_thread(
            stt_call, audio, "audio/mpeg", cfg)
        dt = time.monotonic() - t0
    except Exception as e:
        return {"status": "failed", "error": str(e)[:300]}
    sentence = fixture.get("sentence") or ""
    return {
        "status": "ok",
        "seconds": round(dt, 3),
        "transcript": transcript[:REPLY_KEEP_CHARS],
        "matches_fixture": (normalise(transcript) == normalise(sentence)
                            if sentence else None),
        "stt_model": model_id,
    }


async def _tts_unit(seat: dict, plan: dict, cfg: dict, run_dir: Path) -> dict:
    why = voice_support(plan, seat)
    if why:
        return {"status": "unsupported", "reason": why}
    try:
        t0 = time.monotonic()
        audio = await asyncio.to_thread(
            tts_call, TTS_SENTENCE, seat["voice_id"], cfg)
        dt = time.monotonic() - t0
    except Exception as e:
        return {"status": "failed", "error": str(e)[:300]}
    artefact = f"tts-{seat['slug']}.mp3"
    (run_dir / artefact).write_bytes(audio)
    return {
        "status": "ok",
        "seconds": round(dt, 3),
        "bytes": len(audio),
        "chars": len(TTS_SENTENCE),
        "est_cost": voice.voice_cost("tts", len(TTS_SENTENCE), cfg),
        "artefact": artefact,
    }


async def _pipeline_unit(seat, plan, fixture, no_fixture, cfg, run_dir) -> dict:
    why = voice_support(plan, seat) or (no_fixture if fixture is None else "")
    if why:
        return {"status": "unsupported", "reason": why}
    stages = {}
    try:
        audio = (fixtures_root() / fixture["file"]).read_bytes()
        t0 = time.monotonic()
        transcript, _ = await asyncio.to_thread(stt_call, audio, "audio/mpeg", cfg)
        stages["stt"] = {"seconds": round(time.monotonic() - t0, 3),
                         "transcript": transcript[:REPLY_KEEP_CHARS]}
    except Exception as e:
        return {"status": "failed", "failed_stage": "stt",
                "error": str(e)[:300], "stages": stages}
    try:
        r = await call_model(seat, transcript, cfg)
        stages["model"] = {
            "seconds": round(r["total_s"], 3),
            "first_word_s": (round(r["ttfb_s"], 3)
                             if r["ttfb_s"] is not None else None),
            "leg": r["leg"],
            "reply": r["text"][:REPLY_KEEP_CHARS],
        }
    except Exception as e:
        return {"status": "failed", "failed_stage": "model",
                "error": str(e)[:300], "stages": stages}
    speak = r["text"][:TTS_MAX_CHARS]
    try:
        t0 = time.monotonic()
        audio_out = await asyncio.to_thread(
            tts_call, speak or "The model returned an empty reply.",
            seat["voice_id"], cfg)
        stages["tts"] = {"seconds": round(time.monotonic() - t0, 3),
                         "bytes": len(audio_out), "chars": len(speak),
                         "est_cost": voice.voice_cost("tts", len(speak), cfg)}
    except Exception as e:
        return {"status": "failed", "failed_stage": "tts",
                "error": str(e)[:300], "stages": stages}
    artefact = f"pipeline-{seat['slug']}.mp3"
    (run_dir / artefact).write_bytes(audio_out)
    return {
        "status": "ok",
        "total_s": round(sum(s["seconds"] for s in stages.values()), 3),
        "stages": stages,
        "artefact": artefact,
        "reply_truncated_for_tts": len(r["text"]) > TTS_MAX_CHARS,
    }


# ---------- stored runs ----------

def _load(run_id: str):
    """One stored run's results, live dict preferred. A run marked running on
    disk with no live entry died with the server; say so instead of showing a
    progress bar that will never move."""
    if run_id in _active:
        return _active[run_id]
    path = runs_root() / run_id / "results.json"
    try:
        results = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if results.get("state") == "running":
        results["state"] = "interrupted"
    return results


def get_run(run_id: str):
    if not RUN_ID_RE.match(run_id or ""):
        return None
    return _load(run_id)


def list_runs() -> list:
    root = runs_root()
    if not root.is_dir():
        return []
    out = []
    for d in sorted(root.iterdir(), reverse=True):
        if not RUN_ID_RE.match(d.name):
            continue
        r = _load(d.name)
        if not r:
            continue
        out.append({"run_id": r.get("run_id") or d.name,
                    "created_at": r.get("created_at") or "",
                    "state": r.get("state") or "unknown",
                    "progress": r.get("progress") or {},
                    "dimensions": (r.get("config") or {}).get("dimensions") or [],
                    "models": [m.get("slug") for m in r.get("models") or []]})
        if len(out) >= LIST_LIMIT:
            break
    return out


def artefact_path(run_id: str, name: str):
    """A run artefact's on-disk path, or None for anything unsafe or absent.
    Both segments are allow-listed shapes, and the resolved result must still
    sit inside the run directory - a symlinked or dotted name gets a None,
    never a read."""
    if not RUN_ID_RE.match(run_id or "") or not ARTEFACT_RE.match(name or ""):
        return None
    run_dir = (runs_root() / run_id).resolve()
    path = (run_dir / name).resolve()
    if not path.is_file() or run_dir not in path.parents:
        return None
    return path


def delete_run(run_id: str) -> str:
    """Remove one stored run and its audio - the retention control. Returns ''
    or a plain refusal."""
    if not RUN_ID_RE.match(run_id or ""):
        return "not a benchmark run id"
    if run_id in _active:
        return "that run is still going - wait for it to finish"
    run_dir = runs_root() / run_id
    if not run_dir.is_dir():
        return "no such run"
    shutil.rmtree(run_dir)
    return ""
