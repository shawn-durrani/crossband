"""ElevenLabs integration: streaming TTS relay config, speech-to-text, voices,
quota. The API key stays server-side; the browser talks only to this backend.
Every synthesized character and transcribed second is logged to voice_usage."""

import base64
import json
import os

import httpx

from . import tts_models, tts_v3

ELEVEN_BASE = "https://api.elevenlabs.io"
# The two streaming sockets a reply can be spoken through (#480). Which one
# a model uses is tts_models.route_for's call: the text-to-speech socket
# refuses every eleven_v3 id, and the dialogue socket takes only those.
TTS_WS_URL = (
    "wss://api.elevenlabs.io/v1/text-to-speech/{voice_id}/stream-input"
    "?model_id={model_id}&output_format=mp3_44100_128"
)
TTD_WS_URL = (
    "wss://api.elevenlabs.io/v1/text-to-dialogue/stream-input"
    "?model_id={model_id}&output_format=mp3_44100_128"
)
# Scribe v2 Realtime streaming STT - opt-in, parallel to the batch transcribe()
# POST. Auth is the xi-api-key header, set on the websocket in routers/voice.py.
STT_WS_URL = "wss://api.elevenlabs.io/v1/speech-to-text/realtime"
TIMEOUT = 30

# Keyterm biasing on the realtime STT connection (#28 phase 3): roster names
# ride the websocket URL's `keyterms` query parameter so the transcriber
# spells the people in the room consistently instead of by ear. Caps match
# the ElevenLabs documented limits: at most 50 terms, each at most 20
# characters. Names only, chosen at session OPEN - nothing per-frame changes.
KEYTERMS_MAX = 50
KEYTERM_MAX_CHARS = 20


def clean_keyterms(names) -> list:
    """Bounded, de-duplicated keyterm list from candidate names: whitespace
    trimmed, empties dropped, each term capped at KEYTERM_MAX_CHARS, at most
    KEYTERMS_MAX terms, first-seen order, case-insensitive de-dup (the bias
    works on either casing; sending both wastes a slot)."""
    out, seen = [], set()
    for raw in names or []:
        if not isinstance(raw, str):
            continue
        term = raw.strip()[:KEYTERM_MAX_CHARS].strip()
        if not term or term.lower() in seen:
            continue
        seen.add(term.lower())
        out.append(term)
        if len(out) >= KEYTERMS_MAX:
            break
    return out


def stt_ws_url(keyterms=None) -> str:
    """The realtime STT connection URL, with keyterm biasing when there are
    names to bias towards. No keyterms = the exact historical URL, so a
    session with nobody to name connects byte-for-byte as it always has."""
    terms = clean_keyterms(keyterms)
    if not terms:
        return STT_WS_URL
    from urllib.parse import urlencode
    return STT_WS_URL + "?" + urlencode([("keyterms", t) for t in terms])


def api_key():
    return os.environ.get("ELEVENLABS_API_KEY")


def enabled():
    return bool(api_key())


# ---- provider seam ----
# Every voice surface (both relays, the batch STT upload, voice listing and
# assign, transcribe_audio_url, the /api/state echo, the benchmark
# catalogue) resolves through provider_for(), never enabled(), so the
# choice of engine lives in exactly one place. A later local-engine PR
# registers an engine under one of these names and flips the reserved
# branch below: it does not touch endpoints.
PROVIDER_ELEVENLABS = "elevenlabs"
# RESERVED, not implemented here: a fully local STT/TTS stack (candidates
# include faster-whisper or Moonshine for STT, Kokoro for TTS), no cloud
# egress. The value names the class, not an engine choice: whichever local
# stack lands, the setting stays truthful.
PROVIDER_LOCAL = "local"
# Voice unavailable: the same clean state a keyless machine has today.
NO_VOICE = "none"


def provider_for(cfg) -> str:
    """THE single selection point for a voice request: the provider this
    config resolves to, or NO_VOICE when voice is cleanly unavailable.

    `cfg` is the as_cfg() dict passed from the endpoints and tools (or any
    dict carrying the settings). Rules, pinned by tests/test_voice_provider.py:
    - "auto" (default): ElevenLabs when ELEVENLABS_API_KEY exists, else
      NO_VOICE. Byte-for-byte today's semantics, keyless state included.
    - "elevenlabs": the same, chosen explicitly.
    - "local": NO_VOICE while no local engine is installed, even with
      a key set: reserved and unimplemented, so it can never egress.
    - anything else (typo, non-string): treated as "auto" - the repo
      convention that a bad value is ignored, not crashed on, and auto's
      egress is still gated on a key the operator supplied anyway.
    """
    want = (cfg or {}).get("voice_provider")
    if not isinstance(want, str):
        want = "auto"
    want = want.strip() or "auto"
    if want not in (PROVIDER_ELEVENLABS, PROVIDER_LOCAL):
        want = "auto"
    if want == PROVIDER_LOCAL:
        return NO_VOICE
    return PROVIDER_ELEVENLABS if api_key() else NO_VOICE


def disabled_reason(cfg) -> str:
    """The rejection sentence for a NO_VOICE surface. Byte-identical with
    today's message whenever the cause is a missing key; names the
    reservation when the operator asked for local engines that are not
    installed yet, so the reason is honest in both states."""
    want = (cfg or {}).get("voice_provider")
    if isinstance(want, str) and want.strip() == PROVIDER_LOCAL:
        return ("Local voice engines (voice_provider=local) "
                "are not installed yet")
    return "ELEVENLABS_API_KEY not set"


def _headers():
    return {"xi-api-key": api_key()}


def list_voices():
    r = httpx.get(f"{ELEVEN_BASE}/v1/voices", headers=_headers(), timeout=TIMEOUT)
    r.raise_for_status()
    return [
        {
            "voice_id": v["voice_id"],
            "name": v.get("name", v["voice_id"]),
            "category": v.get("category", ""),
            "preview_url": v.get("preview_url"),
        }
        for v in r.json().get("voices", [])
    ]


def list_models():
    """GET /v1/models, raw. tts_models.parse_models keeps the speech ones."""
    r = httpx.get(f"{ELEVEN_BASE}/v1/models", headers=_headers(), timeout=10)
    r.raise_for_status()
    return r.json()


def model_fetcher(cfg):
    """list_models when voice runs on ElevenLabs, else None, so a keyless
    install (and the keyless test suite) never asks for the list."""
    return list_models if provider_for(cfg) == PROVIDER_ELEVENLABS else None


def tts_choice(cfg, seat_choice=""):
    """Resolve which model speaks for this config and seat, from the cached
    or pinned list, never waiting on the network. A stale list is refreshed
    in the background for the next reply."""
    fetch = model_fetcher(cfg)
    tts_models.refresh_soon(fetch)
    snap = tts_models.catalogue()
    refused = tts_models.refused_ids()
    resolved = tts_models.resolve(cfg.get("tts_model"), seat_choice,
                                  models=snap["models"], refused=refused)
    resolved["attempts"] = tts_models.attempts(resolved, snap["models"], refused)
    return resolved


def tts_model_for(cfg, seat_choice="") -> str:
    """The model id a reply would be spoken with right now."""
    return tts_choice(cfg, seat_choice)["model"]


def subscription():
    """Live credit balance for the quota display. Needs User:Read on the key."""
    r = httpx.get(f"{ELEVEN_BASE}/v1/user/subscription", headers=_headers(), timeout=TIMEOUT)
    r.raise_for_status()
    d = r.json()
    return {
        "tier": d.get("tier"),
        "character_count": d.get("character_count"),
        "character_limit": d.get("character_limit"),
        "next_reset_unix": d.get("next_character_count_reset_unix"),
    }


def transcribe(audio_bytes, mime, cfg):
    """Speech-to-text via Scribe v2. (No scribe_v1 fallback: ElevenLabs removes
    it on 2026-07-09 - a fallback to a dead model is just a slower error.)"""
    model_id = cfg.get("stt_model") or "scribe_v2"
    r = httpx.post(
        f"{ELEVEN_BASE}/v1/speech-to-text",
        headers=_headers(),
        data={"model_id": model_id},
        files={"file": ("utterance.webm", audio_bytes, mime or "audio/webm")},
        timeout=60,
    )
    if r.status_code < 400:
        return r.json().get("text", "").strip(), model_id
    raise RuntimeError(f"Speech-to-text failed ({r.status_code}: {r.text[:200]})")


def transcribe_diarized(audio_bytes, mime, cfg, num_speakers=None):
    """The room-mode second pass (#28): batch Scribe v2 with diarize=true,
    returning the FULL response (per-word speaker_id clusters), not just the
    text. Batch is the only place diarization exists - the realtime model
    trades it away for latency - which is why room mode runs this in parallel
    rather than touching the live relay's stream. `num_speakers` (phase 2) is
    the roster-size-plus-one hint; None sends no hint at all, which keeps the
    roster-less request byte-identical to phase 1's."""
    model_id = cfg.get("stt_model") or "scribe_v2"
    data = {"model_id": model_id, "diarize": "true"}
    if num_speakers:
        data["num_speakers"] = str(int(num_speakers))
    r = httpx.post(
        f"{ELEVEN_BASE}/v1/speech-to-text",
        headers=_headers(),
        data=data,
        files={"file": ("utterance.wav", audio_bytes, mime or "audio/wav")},
        timeout=60,
    )
    if r.status_code < 400:
        return r.json()
    raise RuntimeError(f"Diarized speech-to-text failed ({r.status_code}: {r.text[:200]})")


def synthesize(text, voice_id, cfg):
    """Non-streaming TTS for the synthetic benchmark (#94): the whole mp3
    back. The live voice path stays on the streaming sockets; this exists so
    a benchmark can time and retain synthesis without playing it. The model
    resolves exactly as a live reply's would (#480). Voice settings mirror
    tts_init_message so the artefact sounds like the room."""
    model_id = tts_model_for(cfg)
    if tts_models.route_for(model_id) == tts_models.ROUTE_DIALOGUE:
        return _synthesize_dialogue(text, voice_id, model_id, cfg)
    r = httpx.post(
        f"{ELEVEN_BASE}/v1/text-to-speech/{voice_id}",
        params={"output_format": "mp3_44100_128"},
        headers=_headers(),
        json={"text": text, "model_id": model_id,
              "voice_settings": {"stability": 0.5, "similarity_boost": 0.75,
                                 "speed": cfg.get("tts_speed", 1.0)}},
        timeout=60,
    )
    if r.status_code < 400:
        return r.content
    raise RuntimeError(f"Text-to-speech failed ({r.status_code}: {r.text[:200]})")


def _synthesize_dialogue(text, voice_id, model_id, cfg):
    """A whole reply through the dialogue socket, the same one a live v3
    reply uses (ElevenLabs lists v3 Conversational on that socket only).
    One connection: register the voice, send the text, close, collect
    the audio."""
    from websockets.sync.client import connect
    url = tts_ws_url(tts_models.ROUTE_DIALOGUE, voice_id, model_id)
    chunks = []
    # The whole reply still goes as one piece, now with the live path's
    # accent tag in front and its stability in the open message (#493).
    text = tts_v3.with_tag(text, tts_v3.accent_tag(cfg))
    with connect(url, max_size=16 * 1024 * 1024, open_timeout=30) as ws:
        ws.send(tts_open_message(tts_models.ROUTE_DIALOGUE, cfg, voice_id))
        ws.send(json.dumps({"inputs": [{"text": text, "voice_id": voice_id}]}))
        ws.send(json.dumps({"close_socket": True}))
        while True:
            data = json.loads(ws.recv(timeout=60))
            if data.get("audio"):
                chunks.append(base64.b64decode(data["audio"]))
            if data.get("error"):
                raise RuntimeError("Text-to-speech failed "
                                   f"({str(data.get('message') or data['error'])[:200]})")
            if data.get("is_final"):
                return b"".join(chunks)


def tts_init_message(cfg):
    """First message on the ElevenLabs TTS websocket: auth + adaptive chunking.
    Small first chunk for fast time-to-first-audio, larger after for prosody."""
    return json.dumps({
        "text": " ",
        "xi_api_key": api_key(),
        "voice_settings": {"stability": 0.5, "similarity_boost": 0.75,
                           "speed": cfg.get("tts_speed", 1.0)},
        "generation_config": {"chunk_length_schedule": [120, 200, 260, 290]},
    })


def tts_ws_url(route, voice_id, model_id):
    """The upstream socket URL for one reply. The model id has already been
    validated against the model list; the voice id sits in the path on the
    text-to-speech socket and in the first message on the dialogue one."""
    if route == tts_models.ROUTE_DIALOGUE:
        return TTD_WS_URL.format(model_id=model_id)
    return TTS_WS_URL.format(voice_id=voice_id, model_id=model_id)


def tts_open_message(route, cfg, voice_id):
    """The first upstream message. The dialogue socket registers the one
    voice this connection speaks with and takes only a stability setting,
    which tts_v3_stability picks (#493). It buffers on its own fixed
    threshold of about 40 characters and 8 words, so there's no chunk
    schedule to send."""
    if route == tts_models.ROUTE_DIALOGUE:
        return json.dumps({"voices": [voice_id], "xi_api_key": api_key(),
                           "voice_settings": {"stability": tts_v3.stability(cfg)}})
    return tts_init_message(cfg)


def tts_relay_state(cfg, seat_tag=""):
    """The per-reply state tts_upstream_frames keeps. Only the dialogue
    socket reads it: sentence chunks and the accent tag (#493)."""
    return tts_v3.relay_state(cfg, seat_tag)


def tts_upstream_frames(route, msg, voice_id, carry):
    """Translate one browser frame ({text}, {flush}, {done}, in that order
    when combined) into upstream messages. The browser speaks one protocol
    whichever socket is behind the relay.

    On the dialogue socket, `carry` is the reply's state. Without sentence
    chunks, a whitespace-only text (the browser's idle keepalive, or the gap
    between two deltas) becomes a keep_alive, and the whitespace is held in
    `carry` and sent ahead of the next real text so words never run
    together. With them, text is held until a sentence ends (#493). Either
    way the accent tag, when there is one, goes in front of every piece."""
    out = []
    text = msg.get("text")
    if route == tts_models.ROUTE_DIALOGUE:
        if carry.get("chunks"):
            out += _held_frames(text, msg, voice_id, carry)
        else:
            if isinstance(text, str) and text:
                if text.strip():
                    out.append(_dialogue_input(carry.pop("ws", "") + text,
                                               voice_id, carry))
                else:
                    carry["ws"] = (carry.get("ws", "") + text)[-4:]
                    out.append({"keep_alive": True})
        if msg.get("flush"):
            out.append({"flush": True})
        if msg.get("done"):
            out.append({"close_socket": True})
    else:
        if text:
            out.append({"text": text})
        if msg.get("flush"):
            out.append({"text": " ", "flush": True})
        if msg.get("done"):
            out.append({"text": ""})
    return [json.dumps(f) for f in out]


def _held_frames(text, msg, voice_id, carry):
    """Sentence chunks on the dialogue socket. Text joins what's held, and
    everything up to the last sentence end goes out as one piece. A frame
    that sends nothing becomes a keep_alive, so the socket's 20 second
    timer never runs out while a sentence is still arriving. A flush or done
    sends whatever is held. Whitespace stays held until real text follows
    it, so it always leads the next piece."""
    out = []
    if isinstance(text, str) and text:
        ready, carry["held"] = tts_v3.take_ready(carry.get("held", "") + text)
        out.append(_dialogue_input(ready, voice_id, carry) if ready
                   else {"keep_alive": True})
    if (msg.get("flush") or msg.get("done")) and carry.get("held", "").strip():
        out.append(_dialogue_input(carry.pop("held"), voice_id, carry))
    return out


def _dialogue_input(piece, voice_id, carry):
    """One piece of text for the dialogue socket, with the reply's accent
    tag in front. The tag's characters are counted, because ElevenLabs
    bills them like any other text."""
    tag = carry.get("tag")
    if tag:
        carry["tag_chars"] = carry.get("tag_chars", 0) + len(tag) + 1
    return {"inputs": [{"text": tts_v3.with_tag(piece, tag), "voice_id": voice_id}]}


def tts_downstream(route, data):
    """One upstream message as the browser frame to send, and whether the
    reply is over. The sockets name their last message differently:
    `isFinal` on text-to-speech, `is_final` on dialogue."""
    out = {}
    if data.get("audio"):
        out["audio"] = data["audio"]
    final = data.get("is_final") if route == tts_models.ROUTE_DIALOGUE \
        else data.get("isFinal")
    if final:
        out["final"] = True
    if data.get("error"):
        out["error"] = data.get("message") or data["error"]
    return out, bool(final or data.get("error"))


def voice_cost(kind, units, cfg):
    """Estimated $ from the editable voice_pricing map in config."""
    pricing = cfg.get("voice_pricing") or {}
    if kind == "tts":  # units = characters
        per_m = pricing.get("tts_per_1m_chars")
        return (units * per_m / 1_000_000) if per_m else None
    if kind == "stt":  # units = seconds
        per_hr = pricing.get("stt_per_hour")
        return (units * per_hr / 3600) if per_hr else None
    return None
