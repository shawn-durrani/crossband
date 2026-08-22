"""ElevenLabs integration: streaming TTS relay config, speech-to-text, voices,
quota. The API key stays server-side; the browser talks only to this backend.
Every synthesized character and transcribed second is logged to voice_usage."""

import json
import os

import httpx

ELEVEN_BASE = "https://api.elevenlabs.io"
TTS_WS_URL = (
    "wss://api.elevenlabs.io/v1/text-to-speech/{voice_id}/stream-input"
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
    """Non-streaming TTS for the synthetic benchmark (#94): one POST, the
    whole mp3 back. The live voice path stays on the streaming websocket;
    this exists so a benchmark can time and retain synthesis without playing
    it. Voice settings mirror tts_init_message so the artefact sounds like
    the room."""
    model_id = cfg.get("tts_model") or "eleven_flash_v2_5"
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
