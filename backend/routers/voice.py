"""Voice endpoints: ElevenLabs proxying with the key held server-side.

- batch STT POST (+ per-chat usage metering)
- realtime STT websocket relay to Scribe v2 Realtime (fallback semantics: the
  batch POST remains the default path; this relay is the opt-in parallel one)
- TTS websocket relay with the init message + adaptive chunk scheduling
"""

import asyncio
import base64
import json
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
    moment a password exists."""
    from .. import auth as auth_mod
    app = ws.app
    allowed = getattr(app.state, "allowed_hosts", {"127.0.0.1", "localhost", "::1"})
    if (ws.url.hostname or "").lower() not in allowed:
        return False
    origin = ws.headers.get("origin")
    if origin is not None and (urlparse(origin).hostname or "").lower() not in allowed:
        return False
    if getattr(app.state, "auth_enrolled", False):
        return auth_mod.session_ok(app, ws.cookies.get(auth_mod.SESSION_COOKIE))
    return True
import logging

from fastapi import APIRouter, Body, File, Form, HTTPException, Request, UploadFile, WebSocket
from fastapi import WebSocketDisconnect

from .. import db, diagnostics, diarize, engine, voice, voice_trace

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
    if not voice.enabled():
        return {"enabled": False}
    out = {"enabled": True, "tts_model": request.app.state.settings.tts_model}
    try:
        out["quota"] = voice.subscription()
    except Exception:
        out["quota"] = None  # key may lack User:Read - fine
    return out


@router.get("/api/voice/voices")
def voice_voices():
    if not voice.enabled():
        raise HTTPException(400, "ELEVENLABS_API_KEY not set")
    try:
        return {"voices": voice.list_voices()}
    except Exception as e:
        raise HTTPException(502, f"Could not list voices: {e}")


@router.post("/api/voice/assign")
def voice_assign():
    """Give every enabled participant a distinct voice if it doesn't have one."""
    if not voice.enabled():
        raise HTTPException(400, "ELEVENLABS_API_KEY not set")
    voices = voice.list_voices()
    by_name = {v["name"]: v["voice_id"] for v in voices}
    pool = [by_name[n] for n in PREFERRED_VOICES if n in by_name]
    pool += [v["voice_id"] for v in voices if v["voice_id"] not in pool]
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
    if not voice.enabled():
        raise HTTPException(400, "ELEVENLABS_API_KEY not set")
    cfg = request.app.state.settings.as_cfg()
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
    if not voice.enabled():
        await ws.send_json({"error": "ELEVENLABS_API_KEY not set"})
        await ws.close()
        return
    cfg = ws.app.state.settings.as_cfg()
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
    if not voice.enabled():
        await ws.send_json({"error": "ELEVENLABS_API_KEY not set"})
        await ws.close()
        return
    cfg = ws.app.state.settings.as_cfg()
    try:
        init = await ws.receive_json()
    except WebSocketDisconnect:
        return
    chat_id = init.get("chat_id")
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
                    row = con.execute("SELECT room_mode FROM chats WHERE id=?",
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
                on = bool(row and row["room_mode"])
                # Session-start sniff eligibility (#28, third field test):
                # room mode off but sufficient remembered non-owner voices
                # exist. Decided here, once, off the audio path - the same
                # people read the keyterms already needed.
                sniff = (not on) and diarize.sniff_eligible(
                    people, cfg.get("user_name"))
                return on, names, sniff
            enabled, roster_names, sniff_ok = await asyncio.to_thread(
                _session_open_reads)
            diarize.set_room_enabled(chat_id, enabled)
            keyterm_names += roster_names
            room.sniff_remaining = (diarize.SNIFF_UTTERANCES if sniff_ok
                                    else 0)
        except Exception:
            log.warning("room-mode seed failed; session continues", exc_info=True)
    try:
        async with websockets.connect(
            voice.stt_ws_url(keyterm_names),
            additional_headers={"xi-api-key": voice.api_key()},
            max_size=16 * 1024 * 1024,
        ) as eleven:

            async def pump_up():
                nonlocal seconds
                first = True
                while True:
                    msg = await ws.receive_json()
                    if msg.get("done"):
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
                            commit_turn_id = (str(msg.get("turn_id") or "")
                                              .strip()[:64] or None)
                            if room.enabled or diarize.room_enabled(chat_id):
                                diarize.schedule_pass(chat_id, pcm, pcm_sr,
                                                      db.now(), room, cfg,
                                                      turn_id=commit_turn_id)
                            else:
                                diarize.stash_utterance(chat_id, pcm, pcm_sr)
                                # Session-start sniff (#28): with room mode
                                # off but remembered voices in the house,
                                # the first couple of committed utterances
                                # ALSO run the diarization pass, listening
                                # for a known non-owner voice or a second
                                # cluster - which arms room mode without an
                                # introduction. create_task only, same as
                                # the pass above: NEVER awaited here, and
                                # the upstream byte stream is untouched.
                                if room.sniff_remaining > 0:
                                    room.sniff_remaining -= 1
                                    diarize.schedule_sniff(
                                        chat_id, pcm, pcm_sr, db.now(),
                                        room, cfg, turn_id=commit_turn_id)
                        except Exception:
                            log.warning("diarize scheduling failed; live "
                                        "transcription continues", exc_info=True)
                    await eleven.send(json.dumps(payload))

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
                            await ws.send_json({"final": data.get("text", "")})
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
        if seconds:
            con = db.connect()
            db.log_voice_usage(con, chat_id, "stt", seconds, voice.voice_cost("stt", seconds, cfg))
            con.commit()
            con.close()
        try:
            await ws.close()
        except Exception:
            pass
