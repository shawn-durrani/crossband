"""Background beds: recordings of a real-sounding room, made once by
ElevenLabs' sound effects model.

The rig's cafe and road noise is made on this computer from random
numbers, which is steady and nothing like a room. A bed is the other
kind: 30 seconds of a place, made from a short description and kept in
the rig's cache like the rendered lines, so a rerun costs nothing and
needs no key. Each bed is made to loop, and each turn takes its own
stretch of it, picked by the script and turn, so the same script always
mixes to the same audio.

A bed can hold what made-up noise can't, like a room full of people
talking with no words clear. That's the hard case for naming a voice,
because the noise is voices too.
"""

import hashlib
import json
import os
import tempfile
from pathlib import Path

SFX_URL = "https://api.elevenlabs.io/v1/sound-generation"
MODEL = "eleven_text_to_sound_v2"
OUTPUT_FORMAT = "pcm_16000"
SECONDS = 30.0                  # the longest the model makes
USD_PER_MINUTE = 0.12           # ElevenLabs' API rate for sound effects
# Bump when the request changes in a way the key doesn't already cover.
BED_VERSION = 1
TIMEOUT_S = 120.0

BEDS = {
    "chatter": ("Ambience of a busy cafe at lunchtime. A steady murmur of "
                "many people talking at once, no words clear, cups and "
                "saucers clinking, an espresso machine hissing now and "
                "then. No music."),
    "kitchen": ("Ambience of a home kitchen in the evening. An extractor fan "
                "humming, a tap running on and off, plates and cutlery "
                "clinking in the sink. No voices, no music."),
}


class BedError(RuntimeError):
    pass


def cache_key(prompt: str, seconds: float = SECONDS) -> str:
    raw = json.dumps([BED_VERSION, MODEL, OUTPUT_FORMAT, True, seconds,
                      prompt], ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def elevenlabs_fetch(api_key: str):
    """The real request, as a function Beds calls for a bed it hasn't got.
    Split out so the tests can hand in a fake."""
    def fetch(prompt: str, seconds: float) -> bytes:
        import httpx
        resp = httpx.post(
            SFX_URL, params={"output_format": OUTPUT_FORMAT},
            headers={"xi-api-key": api_key,
                     "Content-Type": "application/json"},
            json={"text": prompt, "model_id": MODEL, "loop": True,
                  "duration_seconds": seconds},
            timeout=TIMEOUT_S)
        if resp.status_code != 200:
            try:
                why = str((resp.json().get("detail") or {}).get("status")
                          or "")
            except (ValueError, AttributeError):
                why = ""
            if why == "missing_permissions":
                raise BedError("the ElevenLabs key can't make sound "
                               "effects. Give it the sound effects "
                               "permission, and the next run makes the "
                               "beds.")
            raise BedError(f"ElevenLabs refused the bed, HTTP "
                           f"{resp.status_code}.")
        return resp.content
    return fetch


class Beds:
    """Beds through `fetch`, or from the cache. `fetch` is None when
    there's no key, and then only cached beds can be used."""

    def __init__(self, cache_dir, fetch=None):
        self.dir = Path(cache_dir) / "beds"
        self.fetch = fetch
        self.new_seconds = 0.0
        self.cached = 0

    def path_for(self, kind: str) -> Path:
        return self.dir / f"{kind}-{cache_key(BEDS[kind])[:16]}.pcm"

    def bed(self, kind: str) -> bytes:
        """The bed's audio, 16 kHz mono 16-bit."""
        if kind not in BEDS:
            raise BedError(f"no bed called {kind!r}")
        path = self.path_for(kind)
        if path.exists():
            self.cached += 1
            return path.read_bytes()
        if self.fetch is None:
            raise BedError(
                f"the {kind} bed isn't in the cache and there's no "
                "ElevenLabs key to make it.")
        pcm = self.fetch(BEDS[kind], SECONDS)
        if not pcm or len(pcm) % 2 or len(pcm) < 16000 * 2:
            raise BedError("ElevenLabs sent back a bed of the wrong shape.")
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".part")
        with os.fdopen(fd, "wb") as fh:
            fh.write(pcm)
        os.replace(tmp, path)
        self.new_seconds += len(pcm) / 2 / 16000
        return pcm

    def stats(self) -> dict:
        return {"new_seconds": round(self.new_seconds, 1),
                "cached": self.cached,
                "usd": round(self.new_seconds / 60 * USD_PER_MINUTE, 4)}
