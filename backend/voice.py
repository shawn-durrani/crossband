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


def transcribe_diarized(audio_bytes, mime, cfg):
    """The room-mode second pass (#28 phase 1): batch Scribe v2 with
    diarize=true, returning the FULL response (per-word speaker_id clusters),
    not just the text. Batch is the only place diarization exists - the
    realtime model trades it away for latency - which is why room mode runs
    this in parallel rather than touching the live relay's stream. No
    num_speakers hint in phase 1 (the roster arrives with phase 2)."""
    model_id = cfg.get("stt_model") or "scribe_v2"
    r = httpx.post(
        f"{ELEVEN_BASE}/v1/speech-to-text",
        headers=_headers(),
        data={"model_id": model_id, "diarize": "true"},
        files={"file": ("utterance.wav", audio_bytes, mime or "audio/wav")},
        timeout=60,
    )
    if r.status_code < 400:
        return r.json()
    raise RuntimeError(f"Diarized speech-to-text failed ({r.status_code}: {r.text[:200]})")


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
