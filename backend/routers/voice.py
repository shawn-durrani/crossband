"""Voice endpoints: ElevenLabs proxying with the key held server-side.

- batch STT POST (+ per-chat usage metering, and the identity check for a
  turn the realtime relay never heard, #461)
- realtime STT websocket relay to Scribe v2 Realtime (fallback semantics: the
  batch POST remains the default path; this relay is the opt-in parallel one)
- TTS websocket relay with the init message + adaptive chunk scheduling
"""

import asyncio
import base64
import contextlib
import json
import os
import time
import uuid
from urllib.parse import urlparse

import websockets

def _ws_local(ws) -> bool:
    """HTTP middleware doesn't cover websocket scope, so ALL of its checks live
    here: the Host allowlist (loopback + configured trusted_hosts, e.g. a
    Tailscale name), a cross-origin rejection, and - once an owner password is
    enrolled (#25) - the session gate.

    Host alone is not enough. A browser sets Host to whatever it is connecting
    to, and websockets are exempt from CORS, so any page a user visits could
    open ws://127.0.0.1:8902/api/voice/tts, pass a Host check, and drive the
    metered ElevenLabs relays on the operator's key. Origin is set by the
    browser and cannot be forged from page JS, so an Origin whose host is not
    itself allowed is refused. Non-browser clients send no Origin and are
    allowed, matching the HTTP middleware's Sec-Fetch-Site posture.

    The session check mirrors the HTTP middleware's enrolment-activated gate:
    cookies ride the websocket handshake, so the browser that unlocked the
    page authenticates here for free, and an anonymous socket is refused the
    moment a password exists. Before enrolment the gate still has two answers,
    not one: loopback is open and a trusted non-loopback host is not. Both
    guards read auth.GATE_LOOPBACK_HOSTS so they cannot drift apart again."""
    from .. import auth as auth_mod
    from .. import funnel
    app = ws.app
    # #363: while Funnel has the port on the public internet nothing is
    # served, sockets included; and a trusted-host socket without the
    # identity Tailscale adds for tailnet users came in through Funnel.
    if getattr(app.state, "funnel_exposed", None):
        return False
    allowed = getattr(app.state, "allowed_hosts", auth_mod.GATE_LOOPBACK_HOSTS)
    if (ws.url.hostname or "").lower() not in allowed:
        return False
    settings = getattr(app.state, "settings", None)
    if getattr(settings, "tailscale_identity_required", True) \
            and funnel.outside_the_tailnet(ws, auth_mod.GATE_LOOPBACK_HOSTS):
        return False
    origin = ws.headers.get("origin")
    if origin is not None and (urlparse(origin).hostname or "").lower() not in allowed:
        return False
    if auth_mod.session_ok(app, ws.cookies.get(auth_mod.SESSION_COOKIE)):
        return True
    if getattr(app.state, "auth_enrolled", False):
        return False
    # Pre-enrolment the two postures differ, exactly as they do over HTTP.
    # Loopback keeps its historical open posture. A trusted non-loopback host
    # sees the lock screen until an owner password exists, so a tailnet client
    # refused on every /api route cannot open the metered relays either.
    return (ws.url.hostname or "").lower() in auth_mod.GATE_LOOPBACK_HOSTS
import logging

from fastapi import APIRouter, Body, File, Form, HTTPException, Request, UploadFile, WebSocket
from fastapi import WebSocketDisconnect
from pydantic import BaseModel

from .. import (config, db, diagnostics, diarize, engine, room_state,
                seat_trace, tts_models, voice, voice_trace)

router = APIRouter(tags=["voice"])

log = logging.getLogger("crossband.voice")

PREFERRED_VOICES = ["Adam", "Rachel", "Antoni", "Bella", "Josh", "Domi", "Elli", "Sam"]

# The two mic capture profiles a client may report (#28 phase 4, the
# crosstalk capture experiment - see frontend/src/captureProfile.js).
# Allowlisted so the log line stays content-free by construction: anything
# else a client sends is simply not logged.
CAPTURE_PROFILES = {"solo-tuned", "room-open"}


def capture_profile(msg) -> str:
    """The client-reported capture profile out of an init or control frame,
    or '' when absent/unrecognised."""
    p = (msg or {}).get("capture_profile")
    return p if p in CAPTURE_PROFILES else ""


@router.get("/api/voice/status")
def voice_status(request: Request):
    cfg = request.app.state.settings.as_cfg()
    if voice.provider_for(cfg) != voice.PROVIDER_ELEVENLABS:
        return {"enabled": False}
    out = {"enabled": True, "tts_model": voice.tts_model_for(cfg),
           "tts_model_setting": request.app.state.settings.tts_model}
    try:
        out["quota"] = voice.subscription()
    except Exception:
        out["quota"] = None  # key may lack User:Read - fine
    return out


# #480: the voice model setting. The app-wide choice lives in
# config.local.json as `tts_model`, so it survives restarts and sits beside
# every other voice key; a seat's own choice lives on its participants row.
TTS_MODEL_ENV = config.ENV_PREFIX + "TTS_MODEL"


def _models_payload(request):
    cfg = request.app.state.settings.as_cfg()
    snap = tts_models.catalogue(voice.model_fetcher(cfg))
    return tts_models.describe(request.app.state.settings.tts_model, snap,
                               locked_by_env=bool(os.environ.get(TTS_MODEL_ENV)))


@router.get("/api/voice/models")
def voice_models(request: Request):
    """The models the picker offers, the current choice, what Automatic
    picks today and why. Fetches ElevenLabs' list when the hour-long cache
    is stale; a keyless install, or an unreachable API, gets the pinned list
    and says so in `source`."""
    return _models_payload(request)


class VoiceModelIn(BaseModel):
    model: str


@router.put("/api/voice/model")
def set_voice_model(body: VoiceModelIn, request: Request):
    """Save the app-wide voice model: "auto" or an id on the current list,
    anything else refused. Takes effect on the next reply, no restart."""
    if os.environ.get(TTS_MODEL_ENV):
        raise HTTPException(409, f"{TTS_MODEL_ENV} is set in the environment "
                                 "and wins over this setting. Remove it to "
                                 "choose the model here.")
    value = (body.model or "").strip()
    cfg = request.app.state.settings.as_cfg()
    snap = tts_models.catalogue(voice.model_fetcher(cfg))
    if not tts_models.valid_choice(value, snap["models"]):
        raise HTTPException(400, f"Unknown voice model {value[:64]!r}. "
                                 "Choose Automatic or a model from the list.")
    config.write_local_key("tts_model", value)
    request.app.state.settings = request.app.state.settings.model_copy(
        update={"tts_model": value})
    return _models_payload(request)


@router.get("/api/voice/voices")
def voice_voices(request: Request):
    cfg = request.app.state.settings.as_cfg()
    if voice.provider_for(cfg) != voice.PROVIDER_ELEVENLABS:
        raise HTTPException(400, voice.disabled_reason(cfg))
    try:
        return {"voices": voice.list_voices()}
    except Exception as e:
        raise HTTPException(502, f"Could not list voices: {e}")


@router.post("/api/voice/assign")
def voice_assign(request: Request):
    """Give every enabled participant a distinct voice if it doesn't have one."""
    cfg = request.app.state.settings.as_cfg()
    if voice.provider_for(cfg) != voice.PROVIDER_ELEVENLABS:
        raise HTTPException(400, voice.disabled_reason(cfg))
    voices = voice.list_voices()
    by_name = {v["name"]: v["voice_id"] for v in voices}
    pool = [by_name[n] for n in PREFERRED_VOICES if n in by_name]
    # #161: the provider's list order floats between calls, so the
    # remainder pool is sorted - two assign passes over the same account
    # pick the same voices, instead of re-rolling into whatever custom or
    # cloned voice happened to list first that day.
    pool += [v["voice_id"]
             for v in sorted(voices,
                             key=lambda x: ((x.get("name") or ""), x["voice_id"]))
             if v["voice_id"] not in pool]
    con = db.connect()
    participants = db.get_participants(con, enabled_only=True)
    used = {p["voice_id"] for p in participants if p["voice_id"]}
    for p in participants:
        if p["voice_id"]:
            continue
        pick = next((v for v in pool if v not in used), None)
        if pick:
            used.add(pick)
            con.execute("UPDATE participants SET voice_id=? WHERE id=?", (pick, p["id"]))
    con.commit()
    out = db.get_participants(con)
    con.close()
    return {"participants": out}


# The largest identity copy the batch path reads: MAX_UTTERANCE_SECONDS at
# the highest rate wav_pcm16 accepts, plus room for the header.
_BATCH_PCM_MAX_BYTES = diarize.MAX_UTTERANCE_SECONDS * 48000 * 2 + 4096


@router.post("/api/chats/{chat_id}/stt")
async def stt(chat_id: int, request: Request, file: UploadFile = File(...),
              duration_ms: int = Form(0), turn_id: str = Form(""),
              pcm: UploadFile | None = File(None)):
    """Batch speech-to-text: the fallback once realtime transcription
    fails, and the salvage for a turn realtime lost.

    #461: the client may also send `pcm`, a PCM-16 mono WAV copy of the
    same turn, with the turn's id. The turn then gets the identity check
    the realtime relay gives a committed turn, started before the
    transcription call so the label is usually parked before /send
    claims it. A turn the relay already checked is left alone. Async
    because the check is a task on the running loop; the transcription
    and the metering are blocking and run on worker threads."""
    cfg = request.app.state.settings.as_cfg()
    if voice.provider_for(cfg) != voice.PROVIDER_ELEVENLABS:
        raise HTTPException(400, voice.disabled_reason(cfg))
    data = await file.read()
    if not data:
        raise HTTPException(400, "Empty audio")
    turn_id = (turn_id or "").strip()[:64]
    if pcm is not None and turn_id and chat_id:
        try:
            await _batch_turn_check(chat_id, turn_id,
                                    await pcm.read(_BATCH_PCM_MAX_BYTES), cfg)
        except Exception:
            log.warning("batch identity check not started; transcription "
                        "continues", exc_info=True)
    try:
        text, model_used = await asyncio.to_thread(
            voice.transcribe, data, file.content_type, cfg)
    except RuntimeError as e:
        raise HTTPException(502, str(e))
    seconds = max(duration_ms, 0) / 1000
    await asyncio.to_thread(_meter_batch_stt, chat_id, seconds, cfg)
    return {"text": text, "model": model_used}


def _meter_batch_stt(chat_id, seconds, cfg):
    con = db.connect()
    try:
        db.log_voice_usage(con, chat_id, "stt", seconds,
                           voice.voice_cost("stt", seconds, cfg))
        con.commit()
    finally:
        con.close()


async def _batch_turn_check(chat_id, turn_id, wav, cfg):
    """Schedule the identity check for one batch-transcribed turn (#461),
    through the same routing rule as the relay's commits. Returns the
    route, or None when there was nothing to do: the relay already
    checked this turn, or the copy was not a PCM-16 mono WAV. Content-free
    log line: a route word and a duration."""
    if diarize.turn_checked(turn_id):
        return None
    audio = diarize.wav_pcm16(wav)
    if audio is None:
        return None
    pcm, rate = audio
    room_on, ambient_ok = await asyncio.to_thread(
        diarize.turn_check_state, chat_id, cfg)
    route = diarize.schedule_turn_check(
        chat_id, pcm, rate, diarize.batch_session(chat_id), cfg,
        room_on=room_on, ambient_ok=ambient_ok, turn_id=turn_id)
    log.info("batch turn check: chat=%s route=%s seconds=%.1f", chat_id,
             route, len(pcm) / 2 / rate)
    return route


@router.post("/api/voice/trace")
def voice_trace_ingest(payload: dict = Body(...)):
    """Durable, privacy-safe ingest for per-turn voice latency traces.

    The browser is the only place that sees a voice turn end to end, so it
    measures the stage durations and POSTs them here (best-effort, fire-and-
    forget from the client's side). We persist ONLY content-free stage timings -
    voice_trace.sanitize enforces that floor, dropping anything that isn't an
    allowlisted stage + a numeric duration + bounded provider/model labels. No
    transcript or reply text can enter this table.

    Also emits a structured log line per turn so the timeline is visible in
    `./start.sh` output during development without opening the DB."""
    turn_id, chat_id, stages = voice_trace.sanitize_turn(payload)
    if not turn_id or not stages:
        return {"stored": 0}
    con = db.connect()
    try:
        for s in stages:
            db.insert_voice_trace(con, turn_id, chat_id, s["stage"], s["ms"],
                                  provider=s["provider"], model=s["model"],
                                  tts_provider=s["tts_provider"], speaker=s["speaker"],
                                  tts_model=s["tts_model"])
        con.commit()
    finally:
        con.close()
    # Structured, content-free dev log: stage=ms pairs for this turn.
    timeline = " ".join(f"{s['stage']}={s['ms']:.0f}ms" for s in stages)
    log.info("voice_trace turn=%s chat=%s %s", turn_id, chat_id, timeline)
    return {"stored": len(stages)}


# The stall beacon's vocabulary (#171). Allowlisted so the WARNING line is
# content-free by construction, exactly like the trace's stage allowlist:
# an unknown kind is dropped, never logged. handoff_stalled (#304) is a
# finished voice turn that the server still hadn't saved as a message 30 s
# later (frontend/src/handoffWatch.js); its stage says which step it
# stopped at, from the same kind of allowlist.
STALL_KINDS = {"round_guard_forced", "gated_speech_stranded", "handoff_stalled"}
STALL_STAGES = {"transcribing", "empty", "failed", "held", "sent"}


@router.post("/api/voice/stall")
def voice_stall(payload: dict = Body(...)):
    """Content-free stall beacon from the voice client (#171).

    The latency trace begins at finalize, so a turn that never finalizes
    leaves no row, and the client's console diagnostics never leave the
    phone. This is the one signal a stuck voice session sends the server:
    a kind from the allowlist plus numeric context, logged at WARNING so
    it reaches data/service.log at the DEFAULT log level - no
    CROSSBAND_LOG_LEVEL change, no tethered browser console, phone-only
    diagnosable. Nothing is persisted; the log line is the product."""
    kind = payload.get("kind")
    if kind not in STALL_KINDS:
        return {"ok": False}

    def _num(key):
        v = payload.get(key)
        return round(float(v), 1) if isinstance(v, (int, float)) else None

    chat_id = payload.get("chat_id")
    stage = payload.get("stage")
    log.warning("voice stall: kind=%s chat=%s idle_ms=%s speech_ms=%s "
                "round_active=%s playing=%s stage=%s",
                kind, chat_id if isinstance(chat_id, int) else None,
                _num("idle_ms"), _num("speech_ms"),
                bool(payload.get("round_active")), _num("playing"),
                stage if stage in STALL_STAGES else None)
    return {"ok": True}


# #304 evidence capture: the one-tap dump. The client keeps a bounded ring
# of its [voice]/[round] control-flow events and any red error text+stack
# (frontend/src/voiceDebug.js); on the owner's explicit tap the ring lands
# here and is written to ONE json file under data/voice_debug/, together
# with the server's own correlated state at that moment - live capture
# sessions, the chat's identity decision history, the parked-label
# outcomes, and the recent latency summary. That file is the issue's whole
# evidence list in one artefact, correlatable by capture sid and turn id
# against the sid/turn lines already in data/service.log.
#
# Privacy floor, same as the trace and the stall beacon: entries are
# timestamps, short tags and bounded strings; the ring's sources never see
# transcript text (each source states that at its own definition), and the
# caps below hold whatever a client sends.
#
# Two ways in. The owner's tap is "manual". An automatic save (#304) names
# the stall kind that asked for it - only a STALL_KINDS word counts, and
# anything else is filed as manual. Automatic saves get a server-side floor
# of one per DEBUG_DUMP_AUTO_MIN_GAP_S on top of the client's own ten-minute
# limit, so two devices stalling on the same wedged round (or a client that
# lost its limit to a reload) still write one file. The owner's tap is never
# limited. Every file counts toward the same newest-20 retention.
DEBUG_DUMP_MAX_ENTRIES = 500
DEBUG_DUMP_TAG_CHARS = 64
DEBUG_DUMP_DATA_CHARS = 2000
DEBUG_DUMP_KEEP_FILES = 20
DEBUG_DUMP_AUTO_MIN_GAP_S = 300
# When the last automatic dump was written (time.monotonic()). A dict so
# conftest's single reset list can clear it between tests.
_auto_dumps: dict = {}


def sanitize_debug_entries(entries) -> list:
    """The client ring, held to the caps: newest DEBUG_DUMP_MAX_ENTRIES
    entries; each a numeric timestamp, a bounded tag and a bounded string
    (or nothing). Anything else is dropped, never logged."""
    out = []
    for e in (entries if isinstance(entries, list) else [])[-DEBUG_DUMP_MAX_ENTRIES:]:
        if not isinstance(e, dict):
            continue
        t, tag, data = e.get("t"), e.get("tag"), e.get("data")
        if not isinstance(t, (int, float)) or isinstance(t, bool) \
                or not isinstance(tag, str) or not tag:
            continue
        out.append({"t": round(float(t), 1),
                    "tag": tag[:DEBUG_DUMP_TAG_CHARS],
                    "data": data[:DEBUG_DUMP_DATA_CHARS]
                    if isinstance(data, str) else None})
    return out


@router.post("/api/voice/debug-dump")
def voice_debug_dump(payload: dict = Body(...)):
    trigger = payload.get("trigger")
    trigger = trigger if trigger in STALL_KINDS else "manual"
    if trigger != "manual":
        last = _auto_dumps.get("last")
        if last is not None and time.monotonic() - last < DEBUG_DUMP_AUTO_MIN_GAP_S:
            return {"ok": False, "reason": "rate_limited"}
        _auto_dumps["last"] = time.monotonic()
    entries = sanitize_debug_entries(payload.get("entries"))
    chat_id = payload.get("chat_id")
    chat_id = chat_id if isinstance(chat_id, int) else None
    try:
        summary = diagnostics.voice_latency_summary(2.0)
    except Exception:
        summary = None  # the dump must not fail on a diagnostics read
    bundle = {
        "dumped_at": db.now(),
        "trigger": trigger,
        "chat_id": chat_id,
        "client_entries": entries,
        "captures": capture_sessions(),
        "identity": {
            "last_decision": diarize.last_decision(chat_id)
            if chat_id is not None else None,
            "history": diarize.decision_history(chat_id)
            if chat_id is not None else [],
            "label_flow": diarize.label_flow(),
        },
        "trace_summary": summary,
        # #162: what each seat's completions did in this chat, content-free.
        "seat_trace": seat_trace.entries(chat_id) if chat_id is not None else [],
    }
    folder = db.DATA_DIR / "voice_debug"
    folder.mkdir(parents=True, exist_ok=True)
    name = "voice_debug_%s_%s.json" % (
        time.strftime("%Y%m%d_%H%M%S"), uuid.uuid4().hex[:6])
    (folder / name).write_text(json.dumps(bundle, indent=1))
    # Newest DEBUG_DUMP_KEEP_FILES dumps stay; older ones go. Sorted names
    # sort by time because the timestamp leads the name.
    for old in sorted(folder.glob("voice_debug_*.json"))[:-DEBUG_DUMP_KEEP_FILES]:
        try:
            old.unlink()
        except OSError:
            pass
    log.warning("voice debug dump: chat=%s entries=%d file=%s trigger=%s",
                chat_id, len(entries), name, trigger)
    # Where the file sits, for the owner's one-line notice: relative to the
    # repo in the usual layout (data/voice_debug/...), absolute otherwise.
    try:
        where = str((folder / name).relative_to(db.ROOT))
    except ValueError:
        where = str(folder / name)
    return {"ok": True, "file": name, "entries": len(entries),
            "trigger": trigger, "where": where}


@router.get("/api/voice/trace/summary")
def voice_trace_summary(window_hours: float = 24.0):
    """Development diagnostics: stage-level p50/p95 latency over the last
    `window_hours`, segmented by model, TTS provider and voice model. Backs
    a dev dashboard and answers the core question - which stage dominates
    the wait.

    Delegates to diagnostics.voice_latency_summary - shared with the
    get_diagnostic MCP tool's "voice_latency" diagnostic."""
    return diagnostics.voice_latency_summary(window_hours)


@router.websocket("/api/voice/tts")
async def tts_relay(ws: WebSocket):
    """Browser <-> backend <-> ElevenLabs streaming TTS relay. The client sends
    an init message, then text/flush/done frames; audio comes back as base64."""
    if not _ws_local(ws):
        await ws.close(code=4403)
        return
    await ws.accept()
    cfg = ws.app.state.settings.as_cfg()
    if voice.provider_for(cfg) != voice.PROVIDER_ELEVENLABS:
        await ws.send_json({"error": voice.disabled_reason(cfg)})
        await ws.close()
        return
    try:
        init = await ws.receive_json()
    except WebSocketDisconnect:
        return
    chat_id = init.get("chat_id")
    voice_id = init.get("voice_id") or ""
    if not voice_id:
        await ws.send_json({"error": "participant has no voice assigned"})
        await ws.close()
        return
    # #480: the seat's own model choice, else the app's. Resolved from the
    # cached or pinned model list: nothing on this path waits on ElevenLabs.
    seat = _seat_voice(init.get("seat"))
    choice = voice.tts_choice(cfg, seat["tts_model"])
    # #493: sentence chunks and the accent tag. Read by the dialogue socket
    # only, so every other model sends what it always has.
    carry = voice.tts_relay_state(cfg, seat["tts_v3_accent_tag"])
    chars = 0
    up = down = None
    try:
        async with contextlib.AsyncExitStack() as stack:
            eleven, model, route = await _open_tts_upstream(
                stack, choice["attempts"], voice_id)
            await eleven.send(voice.tts_open_message(route, cfg, voice_id))
            # Which model is speaking, for the latency trace's labels. The
            # browser ignores frames it doesn't know, so an older client is
            # unaffected.
            await ws.send_json({"tts_model": model})

            async def pump_up():
                nonlocal chars
                while True:
                    msg = await ws.receive_json()
                    text = msg.get("text")
                    if text:
                        chars += len(text)
                    for frame in voice.tts_upstream_frames(route, msg, voice_id, carry):
                        await eleven.send(frame)
                    if msg.get("done"):
                        return

            async def pump_down():
                try:
                    async for raw in eleven:
                        out, over = voice.tts_downstream(route, json.loads(raw))
                        if out:
                            await ws.send_json(out)
                        if over:
                            return
                    # ElevenLabs closed without a final frame - still tell the client we're done
                    await ws.send_json({"final": True})
                except (WebSocketDisconnect, RuntimeError):
                    # client closed the socket mid-stream (barge-in / turn end / a new
                    # turn tearing down this one). Sending after the ASGI close raises
                    # RuntimeError - stop the pump cleanly instead of dying unhandled.
                    return

            up = asyncio.create_task(pump_up())
            down = asyncio.create_task(pump_down())
            done, _ = await asyncio.wait({up, down}, return_when=asyncio.FIRST_EXCEPTION)
            # drain results so a finished task's exception isn't "never retrieved";
            # re-raise anything that isn't a normal client/upstream disconnect
            for _t in done:
                _exc = _t.exception()
                if _exc and not isinstance(_exc, (WebSocketDisconnect,
                                                  websockets.ConnectionClosed, RuntimeError)):
                    raise _exc
    except (WebSocketDisconnect, websockets.ConnectionClosed):
        pass
    except Exception as e:
        try:
            await ws.send_json({"error": str(e)})
        except Exception:
            pass
    finally:
        for task in (up, down):
            if task and not task.done():
                task.cancel()
        # The accent tag is sent as text, so it's billed as text (#493).
        chars += carry.get("tag_chars", 0)
        if chars:
            con = db.connect()
            db.log_voice_usage(con, chat_id, "tts", chars, voice.voice_cost("tts", chars, cfg))
            con.commit()
            con.close()
        try:
            await ws.close()
        except Exception:
            pass


_SEAT_VOICE_BLANK = {"tts_model": "", "tts_v3_accent_tag": ""}


def _seat_voice(slug) -> dict:
    """A seat's own voice model (#480) and v3 accent tag (#493), each ''
    when it follows the app. One indexed read, inline: the relay is on the
    reply's critical path and WAL readers never wait on a writer."""
    if not isinstance(slug, str) or not slug:
        return dict(_SEAT_VOICE_BLANK)
    try:
        con = db.connect()
        try:
            row = con.execute("SELECT tts_model, tts_v3_accent_tag FROM participants "
                              "WHERE slug=?", (slug,)).fetchone()
        finally:
            con.close()
    except Exception:
        log.warning("seat voice settings not read; the app settings speak",
                    exc_info=True)
        return dict(_SEAT_VOICE_BLANK)
    if not row:
        return dict(_SEAT_VOICE_BLANK)
    return {k: row[k] or "" for k in _SEAT_VOICE_BLANK}


async def _open_tts_upstream(stack, attempts, voice_id):
    """Connect to ElevenLabs with the first model it accepts (#480).

    ElevenLabs refuses a model it can't stream on a socket at the handshake
    (HTTP 400 `unsupported_model`), before any text is sent, so falling back
    costs one short round trip and loses nothing. The refusal is remembered
    for a day: Automatic skips the model and the settings page names it.
    Any other failure (a bad key, a rate limit, the network) is raised as
    before. Returns (connection, model, route)."""
    for i, model in enumerate(attempts):
        route = tts_models.route_for(model)
        url = voice.tts_ws_url(route, voice_id, model)
        try:
            conn = await stack.enter_async_context(
                websockets.connect(url, max_size=16 * 1024 * 1024))
        except websockets.InvalidStatus as e:
            if not tts_models.is_model_refusal(e) or i == len(attempts) - 1:
                raise
            tts_models.mark_refused(model)
            log.warning("tts model refused: model=%s route=%s next=%s",
                        model, route, attempts[i + 1])
            continue
        if i:
            log.warning("tts spoke with fallback: wanted=%s model=%s",
                        attempts[0], model)
        return conn, model, route
    raise RuntimeError("no voice model to try")


# #134: the capture-session registry - every live microphone, visible from
# every surface. The field failure: two capture sessions ran at once (a
# second tab or device left in voice), the owner ended the visible one,
# and the orphan kept hearing the room; nothing anywhere could show or
# stop it. Content-free entries: ids, chat, timing, a client hint - never
# audio. Mutated only on the event loop, so no lock.
_captures: dict = {}
CAPTURE_KILLED_CODE = 4001


def capture_sessions() -> list:
    # Snapshot before iterating: the readers (/api/voice/captures and the
    # #304 debug dump) are sync endpoints on a threadpool thread while the
    # event loop inserts and pops entries, and iterating the live dict can
    # raise "dictionary changed size during iteration" mid-request.
    return [{k: v[k] for k in ("sid", "chat_id", "started_at", "client")}
            for v in list(_captures.values())]


def _pop_capture(sid: str, reason: str) -> None:
    """Diagnostics-only wrapper around `_captures.pop`: logs sid, reason
    (client_done/disconnect/killed_remotely/exception/relay_exit) and the
    session's lifetime in seconds. Idempotent like the raw pop it replaces -
    every call site races the others (pump_up's own finally vs. the outer
    one), so a second pop for the same sid is expected and logs nothing.
    Part of the turn-handoff investigation's Bug B instrumentation: a
    lifetime that keeps running past the client's own reconnect (see
    frontend/src/voice.js's `stt:sid`/`stt:close` logs) is the signature of
    the orphaned-session false "2 microphones live" banner."""
    entry = _captures.pop(sid, None)
    if entry is None:
        return
    lifetime_s = time.time() - entry["started_at"]
    log.info("stt capture close: sid=%s chat=%s reason=%s lifetime_s=%.1f live_now=%d",
             sid, entry["chat_id"], reason, lifetime_s, len(_captures))


@router.get("/api/voice/captures")
def list_captures():
    """#134: the truth the every-surface mic banner reads."""
    return {"captures": capture_sessions()}


@router.post("/api/voice/captures/{sid}/kill")
async def kill_capture(sid: str):
    """#134: end a capture session from ANY surface. The relay socket
    closes with CAPTURE_KILLED_CODE; the owning client treats that code
    as a deliberate stop - full teardown, microphone tracks stopped, no
    auto-reopen ever."""
    entry = _captures.get(sid)
    if not entry:
        raise HTTPException(404, "no such capture session")
    try:
        await entry["ws"].close(code=CAPTURE_KILLED_CODE,
                                reason="ended by the owner")
    except Exception:
        pass
    _pop_capture(sid, "killed_remotely")
    return {"ok": True}


@router.websocket("/api/voice/stt-stream")
async def stt_stream_relay(ws: WebSocket):
    """Browser <-> backend <-> ElevenLabs Scribe v2 Realtime STT relay. The client
    streams base64 PCM-16 mono chunks; a frame with commit=true ends an utterance
    and the committed transcript streams back. Opt-in, parallel alternative to the
    batch /stt POST - the key stays server-side (xi-api-key header). One session
    handles many utterances, so it stays open until the client closes.

    Room mode (#28 phase 1): with `room_mode` set in the init message (or a
    later control frame `{"room_mode": true/false}` with no audio), the relay
    TEES each utterance's already-decoded PCM into a per-session buffer,
    sliced on the same commit boundaries the realtime path produces, and on
    each commit fires backend/diarize.py's fire-and-forget parallel pass.
    THE INVARIANT, pinned by tests/test_room_mode.py: the frames sent
    upstream to ElevenLabs are byte-for-byte identical with room mode on,
    off, or never mentioned, and nothing in this handler ever awaits the
    diarization task - the live path cannot be slowed or broken by it."""
    if not _ws_local(ws):
        await ws.close(code=4403)
        return
    await ws.accept()
    cfg = ws.app.state.settings.as_cfg()
    if voice.provider_for(cfg) != voice.PROVIDER_ELEVENLABS:
        await ws.send_json({"error": voice.disabled_reason(cfg)})
        await ws.close()
        return
    try:
        init = await ws.receive_json()
    except WebSocketDisconnect:
        return
    chat_id = init.get("chat_id")
    # #134: register this CAPTURE session and tell the client its id, so
    # every surface can list live microphones and the client can tell its
    # own session apart from an orphan's. Capture only - the TTS relay is
    # playback and never registers.
    sid = uuid.uuid4().hex[:12]
    _captures[sid] = {"sid": sid, "chat_id": chat_id,
                      "started_at": time.time(),
                      "client": (ws.headers.get("user-agent") or "")[:80],
                      "ws": ws}
    # Diagnostics-only, content-free: sid open/close-with-reason and session
    # lifetime, for the turn-handoff investigation (issue: voice turn-handoff
    # stuck in listening / false "2 microphones live"). Bug B's leading
    # hypothesis is a client reconnect registering a NEW sid here before the
    # OLD one is provably dead - this pairs with the client-side
    # `[voice] ... stt:sid` / `stt:close` logs in frontend/src/voice.js so a
    # field capture can show a stale sid's lifetime overlapping a fresh one
    # for the same chat_id.
    log.info("stt capture open: sid=%s chat=%s live_now=%d",
             sid, chat_id, len(_captures))
    await ws.send_json({"session": sid})
    # Capture experiment (#28 phase 4): record which mic profile this session
    # captured with, so field tests can compare crosstalk label rates between
    # suppression-on and suppression-off capture. INFO and content-free
    # (an allowlisted profile name, never audio or text); older clients send
    # nothing and nothing is logged.
    profile = capture_profile(init)
    if profile:
        log.info("stt capture profile: chat=%s profile=%s", chat_id, profile)
    seconds = 0.0
    up = down = None
    last_partial = ""  # freshest partial transcript - the prewarm query
    # #85/#104: pair each commit's turn_id with the final it produces. The
    # upstream returns finals in commit order on this one socket, so a FIFO
    # here - the single place commit and final meet - lets every final go
    # back down stamped with the id of the commit it belongs to, and the
    # client can enforce only-one-wins per utterance instead of guessing.
    commit_turn_fifo: list = []
    # Room-mode session state (utterance tee + label bookkeeping). Constructed
    # unconditionally. Phase 2 (#28): the tee itself now runs on EVERY
    # session - a bounded local buffer append per frame, still nothing on the
    # upstream byte path - because the spoken introduction that flips room
    # mode on arrives BEFORE the mode is on, and the introduction utterance's
    # own audio is the owner's first voice anchor. With the mode off the
    # buffer is only ever stashed locally at each commit; no batch call, no
    # task, no label - the phase-1 pins in tests/test_room_mode.py still hold.
    room = diarize.RoomSession(enabled=bool(init.get("room_mode")))
    # Session-open reads (one worker-thread trip, NEVER on the audio path):
    # seed the server-side room-mode mirror from the chat row, and collect the
    # names the transcriber should spell consistently. A chat whose room mode
    # was flipped durably in an earlier session diarizes from the first
    # utterance of this one: that is what "voices are remembered" means end
    # to end.
    #
    # Keyterms (#28 phase 3): the owner's `user_name` plus the present
    # roster's display names ride the upstream connection URL's keyterms
    # parameter, so the realtime transcriber stops spelling the people in
    # the room by ear. Chosen here, once, at open - the per-frame relay loop
    # below is untouched, and a failed read degrades to the owner's name
    # alone, never to a broken session.
    keyterm_names = [cfg.get("user_name") or ""]
    if chat_id:
        try:
            def _session_open_reads():
                con = db.connect()
                try:
                    row = con.execute("SELECT room_mode, ambient_off "
                                      "FROM chats WHERE id=?",
                                      (chat_id,)).fetchone()
                    roster = db.get_room_roster(con, chat_id,
                                                present_only=True)
                finally:
                    con.close()
                from .. import anchors
                people = anchors.store().people()
                preferred = {p["name"].lower(): p["preferred_name"]
                             for p in people}
                names = [preferred.get(r["name"].lower(), r["name"])
                         for r in roster]
                # Every REMEMBERED person's names ride the keyterm hints too
                # (#28, sixth field test): pre-arm the roster is empty, so a
                # known name got no transcription bias and arrived misspelt
                # ("Rina"). Remembered people are exactly who is likely to
                # speak in this house; the relay caps the list at the API's
                # limit downstream. Preferred and given forms both help.
                for p in people:
                    names.append(preferred.get(p["name"].lower()) or p["name"])
                    names.append(p["name"])
                    # Merged-away spellings ride too (#28, names collapse
                    # by voice): a person the store knows under a second
                    # spelling should have every form biased, or the
                    # transcriber re-mints the very spelling drift the
                    # merge just resolved.
                    names.extend(p["merged_names"])
                on = bool(row and row["room_mode"])
                disarmed = bool(row and row["ambient_off"])
                # Ambient local check (#28): matcher enabled, and some
                # sufficient remembered voice to match. Since PR-B this is
                # the ONLY automatic arming door - the bounded EL
                # session-start sniff retired with the cloud identity path,
                # so no arming decision ever costs a batch call. #461: this
                # is eligibility only. Whether the room is on, off or solo
                # is decided per commit, because it can change mid-session;
                # folding the open-time state in here left a session that
                # opened armed with no check at all once the room went solo.
                ambient = diarize.ambient_eligible(people, cfg)
                return on, names, disarmed, ambient
            enabled, roster_names, disarmed, ambient_ok = \
                await asyncio.to_thread(_session_open_reads)
            room_state.seed_mirrors(chat_id, enabled=enabled,
                                    ambient_disarmed=disarmed)
            keyterm_names += roster_names
            room.ambient_on = ambient_ok
        except Exception:
            log.warning("room-mode seed failed; session continues", exc_info=True)
    try:
        async with websockets.connect(
            voice.stt_ws_url(keyterm_names),
            additional_headers={"xi-api-key": voice.api_key()},
            max_size=16 * 1024 * 1024,
        ) as eleven:

            async def pump_up():
                # Diagnostics-only: which of the three ways this loop ends -
                # the client sending {"done": true}, the socket actually
                # disconnecting, or some other exception - so the sid close
                # log below (Bug B instrumentation) can distinguish a clean
                # handoff from the "client moved on, server hasn't noticed
                # yet" case the false "2 microphones live" banner needs.
                _close_reason = "disconnect"
                try:
                    nonlocal seconds
                    first = True
                    while True:
                        msg = await ws.receive_json()
                        if msg.get("done"):
                            _close_reason = "client_done"
                            return
                        if "room_mode" in msg and "audio" not in msg:
                            # Control frame, ours alone: toggle the tee and send
                            # NOTHING upstream - the ElevenLabs byte stream stays
                            # identical to a session that never toggled. A
                            # mid-session capture-profile change (#28 phase 4)
                            # rides the same frame and is logged the same
                            # content-free way as the init's.
                            room.set_enabled(msg.get("room_mode"))
                            p = capture_profile(msg)
                            if p:
                                log.info("stt capture profile: chat=%s profile=%s",
                                         chat_id, p)
                            continue
                        if msg.get("speculative") and "audio" not in msg:
                            # Silence-start hint (#28 PR-B), ours alone - NOTHING
                            # goes upstream for it, pinned like the room_mode
                            # control frame. Fire the LOCAL-ONLY identity check
                            # on the buffered utterance now, so the verdict is
                            # cached before the commit frame arrives. create_task
                            # inside, never awaited; a failure to schedule must
                            # not break live transcription.
                            try:
                                # #461: solo runs the check too (labelling
                                # only), so its head start runs as well.
                                if chat_id and (room.enabled
                                                or diarize.room_enabled(chat_id)
                                                or room.ambient_on):
                                    diarize.schedule_speculative(chat_id, room,
                                                                 cfg)
                            except Exception:
                                log.warning("speculative scheduling failed; live "
                                            "transcription continues",
                                            exc_info=True)
                            continue
                        audio = msg.get("audio") or ""
                        sr = int(msg.get("sample_rate", 16000))
                        if audio:
                            try:
                                raw = base64.b64decode(audio)
                                seconds += len(raw) / 2 / sr
                                # The tee: a local buffer append of bytes the
                                # metering above already decoded. Nothing here
                                # touches the upstream payload below. Always on
                                # (phase 2) so the introduction utterance itself
                                # can seed the owner's anchor - see the RoomSession
                                # comment above for why that is safe.
                                room.add_audio(raw, sr)
                            except Exception:
                                pass
                        payload = {
                            "message_type": "input_audio_chunk",
                            "audio_base_64": audio,
                            "commit": bool(msg.get("commit")),
                            "sample_rate": sr,
                        }
                        if first and msg.get("previous_text"):
                            payload["previous_text"] = msg["previous_text"]
                        first = False
                        if payload["commit"] and chat_id:
                            # Speech just ended - start the ambient recall
                            # NOW, overlapped with ElevenLabs finalizing the
                            # transcript, keyed on the freshest partial. The round
                            # only adopts it if it matches the final text.
                            # Best-effort BY CONSTRUCTION: a prewarm is an
                            # optimization, and no failure in it may ever break
                            # live transcription (it did once - a missing import
                            # killed the relay on the first commit frame).
                            # Content-free INFO line: proves the hook fired and
                            # whether there was any partial text to prewarm from.
                            log.info("stt commit: chat=%s partial_chars=%d",
                                     chat_id, len(last_partial))
                            try:
                                engine.prewarm_recall(chat_id, last_partial,
                                                      ws.app.state.memory)
                            except Exception:
                                log.warning("recall prewarm failed; transcription "
                                            "continues without it", exc_info=True)
                        if payload["commit"]:
                            # Commit boundary = utterance boundary: slice the teed
                            # audio. With room mode effective (the client's toggle
                            # OR the server-side flag an introduction flipped -
                            # diarize.room_enabled is a dict lookup, no I/O), fire
                            # the parallel diarization pass; otherwise stash the
                            # utterance locally so a confirmed introduction can
                            # claim it as the owner's anchor. create_task only -
                            # NEVER awaited here; the commit frame below goes
                            # upstream exactly as it always has, and a failure to
                            # even schedule must not break live transcription
                            # (same posture as the prewarm hook).
                            #
                            # The commit frame's `turn_id` (#28 phase 3) is the
                            # client's voice-trace correlation id - the SAME id
                            # its /send will persist on the user message, which
                            # is what lets the pass label the exact turn. Ours
                            # alone: it is not part of the upstream payload
                            # built above, so the ElevenLabs byte stream stays
                            # identical whether or not it is sent.
                            try:
                                pcm, pcm_sr = room.take_utterance()
                                # Claim the speculative silence-start entry (#28
                                # PR-B) synchronously, so it can never leak onto
                                # the next utterance. A dict pop - no I/O; the
                                # staleness judgment happens inside the pass, on
                                # a worker thread.
                                spec = room.take_speculative(len(pcm)) \
                                    if room.speculative else None
                                commit_turn_id = (str(msg.get("turn_id") or "")
                                                  .strip()[:64] or None)
                                commit_turn_fifo.append(commit_turn_id)
                                # One routing rule for every voiced turn
                                # (#461), shared with the batch /stt path: an
                                # armed room runs the armed pass; otherwise
                                # the turn is stashed for an introduction to
                                # claim, and the ambient local check runs -
                                # the on-device matcher, which NEVER calls
                                # ElevenLabs. In solo that check labels and
                                # never arms (the sacred disarm is read
                                # inside it, from the chat row). create_task
                                # only, NEVER awaited; the upstream byte
                                # stream is untouched either way.
                                diarize.schedule_turn_check(
                                    chat_id, pcm, pcm_sr, room, cfg,
                                    room_on=(room.enabled
                                             or diarize.room_enabled(chat_id)),
                                    ambient_ok=room.ambient_on,
                                    turn_id=commit_turn_id, speculative=spec)
                            except Exception:
                                log.warning("diarize scheduling failed; live "
                                            "transcription continues", exc_info=True)
                        await eleven.send(json.dumps(payload))

                except Exception as exc:
                    _close_reason = f"exception:{type(exc).__name__}"
                    raise
                finally:
                    # #134: capture is over when the client's frames stop -
                    # done, disconnect, or error alike. The handler may keep
                    # draining upstream after this; the registry must not
                    # wait for it (the outer finally stays as the backstop).
                    _pop_capture(sid, _close_reason)
            async def pump_down():
                nonlocal last_partial
                try:
                    async for raw in eleven:
                        data = json.loads(raw)
                        mt = data.get("message_type")
                        if mt == "partial_transcript":
                            last_partial = data.get("text", "")
                            await ws.send_json({"partial": data.get("text", "")})
                        elif mt in ("committed_transcript", "committed_transcript_with_timestamps"):
                            out = {"final": data.get("text", "")}
                            tid = commit_turn_fifo.pop(0) if commit_turn_fifo \
                                else None
                            if tid:
                                out["turn_id"] = tid
                            await ws.send_json(out)
                        elif mt == "error" or data.get("error"):
                            await ws.send_json({"error": data.get("message") or
                                                data.get("error") or "stt error"})
                except (WebSocketDisconnect, RuntimeError):
                    return

            up = asyncio.create_task(pump_up())
            down = asyncio.create_task(pump_down())
            # FIRST_COMPLETED, not FIRST_EXCEPTION: pump_down never self-terminates
            # (a session has many commit cycles), so we end when the client closes
            # (pump_up raises/returns) or the upstream drops.
            done, _ = await asyncio.wait({up, down}, return_when=asyncio.FIRST_COMPLETED)
            for _t in done:
                _exc = _t.exception()
                if _exc and not isinstance(_exc, (WebSocketDisconnect,
                                                  websockets.ConnectionClosed, RuntimeError)):
                    raise _exc
    except (WebSocketDisconnect, websockets.ConnectionClosed):
        pass
    except Exception as e:
        # The client hears about this ("realtime transcription error") - the
        # server must too, or relay deaths are undiagnosable (they were).
        log.warning("stt relay died for chat %s: %s", chat_id, e, exc_info=True)
        try:
            await ws.send_json({"error": str(e)})
        except Exception:
            pass
    finally:
        for task in (up, down):
            if task and not task.done():
                task.cancel()
        _pop_capture(sid, "relay_exit")   # #134: the registry never lies - backstop, usually a no-op second pop
        if seconds:
            con = db.connect()
            db.log_voice_usage(con, chat_id, "stt", seconds, voice.voice_cost("stt", seconds, cfg))
            con.commit()
            con.close()
        try:
            await ws.close()
        except Exception:
            pass
