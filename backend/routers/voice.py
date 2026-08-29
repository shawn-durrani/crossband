"""Voice endpoints: ElevenLabs proxying with the key held server-side.

- batch STT POST (+ per-chat usage metering)
- realtime STT websocket relay to Scribe v2 Realtime (fallback semantics: the
  batch POST remains the default path; this relay is the opt-in parallel one)
- TTS websocket relay with the init message + adaptive chunk scheduling
"""

import asyncio
import base64
import json
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
    app = ws.app
    allowed = getattr(app.state, "allowed_hosts", auth_mod.GATE_LOOPBACK_HOSTS)
    if (ws.url.hostname or "").lower() not in allowed:
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

from .. import (db, diagnostics, diarize, engine, room_state, voice,
                voice_trace)

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
    out = {"enabled": True, "tts_model": request.app.state.settings.tts_model}
    try:
        out["quota"] = voice.subscription()
    except Exception:
        out["quota"] = None  # key may lack User:Read - fine
    return out


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


@router.post("/api/chats/{chat_id}/stt")
def stt(chat_id: int, request: Request, file: UploadFile = File(...),
        duration_ms: int = Form(0)):
    cfg = request.app.state.settings.as_cfg()
    if voice.provider_for(cfg) != voice.PROVIDER_ELEVENLABS:
        raise HTTPException(400, voice.disabled_reason(cfg))
    data = file.file.read()
    if not data:
        raise HTTPException(400, "Empty audio")
    try:
        text, model_used = voice.transcribe(data, file.content_type, cfg)
    except RuntimeError as e:
        raise HTTPException(502, str(e))
    seconds = max(duration_ms, 0) / 1000
    con = db.connect()
    db.log_voice_usage(con, chat_id, "stt", seconds, voice.voice_cost("stt", seconds, cfg))
    con.commit()
    con.close()
    return {"text": text, "model": model_used}


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
                                  tts_provider=s["tts_provider"], speaker=s["speaker"])
        con.commit()
    finally:
        con.close()
    # Structured, content-free dev log: stage=ms pairs for this turn.
    timeline = " ".join(f"{s['stage']}={s['ms']:.0f}ms" for s in stages)
    log.info("voice_trace turn=%s chat=%s %s", turn_id, chat_id, timeline)
    return {"stored": len(stages)}


# The stall beacon's vocabulary (#171). Allowlisted so the WARNING line is
# content-free by construction, exactly like the trace's stage allowlist:
# an unknown kind is dropped, never logged.
STALL_KINDS = {"round_guard_forced", "gated_speech_stranded"}


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
    log.warning("voice stall: kind=%s chat=%s idle_ms=%s speech_ms=%s "
                "round_active=%s playing=%s",
                kind, chat_id if isinstance(chat_id, int) else None,
                _num("idle_ms"), _num("speech_ms"),
                bool(payload.get("round_active")), _num("playing"))
    return {"ok": True}


@router.get("/api/voice/trace/summary")
def voice_trace_summary(window_hours: float = 24.0):
    """Development diagnostics: stage-level p50/p95 latency over the last
    `window_hours`, segmented by model and TTS provider. Backs a dev dashboard
    and answers the core question - which stage dominates the wait.

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
    url = voice.TTS_WS_URL.format(voice_id=voice_id, model_id=cfg["tts_model"])
    chars = 0
    up = down = None
    try:
        async with websockets.connect(url, max_size=16 * 1024 * 1024) as eleven:
            await eleven.send(voice.tts_init_message(cfg))

            async def pump_up():
                nonlocal chars
                while True:
                    msg = await ws.receive_json()
                    text = msg.get("text")
                    if text:
                        chars += len(text)
                        await eleven.send(json.dumps({"text": text}))
                    if msg.get("flush"):
                        await eleven.send(json.dumps({"text": " ", "flush": True}))
                    if msg.get("done"):
                        await eleven.send(json.dumps({"text": ""}))
                        return

            async def pump_down():
                try:
                    async for raw in eleven:
                        data = json.loads(raw)
                        out = {}
                        if data.get("audio"):
                            out["audio"] = data["audio"]
                        if data.get("isFinal"):
                            out["final"] = True
                        if data.get("error"):
                            out["error"] = data.get("message") or data["error"]
                        if out:
                            await ws.send_json(out)
                        if data.get("isFinal") or data.get("error"):
                            return
                    # ElevenLabs closed without isFinal - still tell the client we're done
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
        if chars:
            con = db.connect()
            db.log_voice_usage(con, chat_id, "tts", chars, voice.voice_cost("tts", chars, cfg))
            con.commit()
            con.close()
        try:
            await ws.close()
        except Exception:
            pass


# #134: the capture-session registry - every live microphone, visible from
# every surface. The field failure: two capture sessions ran at once (a
# second tab or device left in voice), the owner ended the visible one,
# and the orphan kept hearing the room; nothing anywhere could show or
# stop it. Content-free entries: ids, chat, timing, a client hint - never
# audio. Mutated only on the event loop, so no lock.
_captures: dict = {}
CAPTURE_KILLED_CODE = 4001


def capture_sessions() -> list:
    return [{k: v[k] for k in ("sid", "chat_id", "started_at", "client")}
            for v in _captures.values()]


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
                # Ambient local check (#28): room off, not disarmed, matcher
                # enabled, and some sufficient remembered voice to match.
                # Since PR-B this is the ONLY automatic arming door - the
                # bounded EL session-start sniff retired with the cloud
                # identity path, so no arming decision ever costs a batch
                # call.
                ambient = (not on) and (not disarmed) and \
                    diarize.ambient_eligible(people, cfg)
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
                                if chat_id and not diarize.ambient_off(chat_id) \
                                        and (room.enabled
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
                                if room.enabled or diarize.room_enabled(chat_id):
                                    diarize.schedule_pass(chat_id, pcm, pcm_sr,
                                                          db.now(), room, cfg,
                                                          turn_id=commit_turn_id,
                                                          speculative=spec)
                                else:
                                    diarize.stash_utterance(chat_id, pcm, pcm_sr)
                                    # Automatic arming while room mode is off
                                    # (#28), unless the owner has said "solo
                                    # mode" (the sacred disarm, honoured cheaply
                                    # via the live mirror here and re-checked
                                    # inside each pass). ONE door since PR-B:
                                    # the ambient local check - the on-device
                                    # matcher, which NEVER calls ElevenLabs (the
                                    # bounded EL session-start sniff retired
                                    # with the cloud identity path). create_task
                                    # only, NEVER awaited; the upstream byte
                                    # stream is untouched either way.
                                    if diarize.ambient_off(chat_id):
                                        pass
                                    elif room.ambient_on:
                                        diarize.schedule_ambient(
                                            chat_id, pcm, pcm_sr, db.now(),
                                            room, cfg, turn_id=commit_turn_id,
                                            speculative=spec)
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
