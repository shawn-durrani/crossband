"""Speak each line in its person's ElevenLabs voice, once.

Every rendered line is kept on disk under the rig's cache, keyed by the
voice, the model and the exact text, so a rerun of the same scripts costs
nothing and needs no key. Only a line the cache hasn't seen is sent to
ElevenLabs, and that line's characters are what the run spends.

Audio comes back as 16 kHz mono 16-bit samples, the rate the app hears
at, so nothing is resampled on the way in.
"""

import hashlib
import json
import os
import tempfile
from pathlib import Path

from eval_voice import cast as cast_mod

TTS_URL = "https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
OUTPUT_FORMAT = "pcm_16000"
SAMPLE_RATE = 16000
DEFAULT_MODEL = "eleven_multilingual_v2"
# Bump when the request changes in a way the key doesn't already cover,
# so old renders are never mistaken for new ones.
RENDER_VERSION = 1
TIMEOUT_S = 90.0


class RenderError(RuntimeError):
    pass


def cache_key(voice_id: str, model: str, text: str) -> str:
    raw = json.dumps([RENDER_VERSION, voice_id, model, OUTPUT_FORMAT, text],
                     ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def elevenlabs_fetch(api_key: str):
    """The real request, as a function the renderer calls for each line it
    hasn't got. Split out so the tests can hand in a fake."""
    def fetch(voice_id: str, model: str, text: str) -> bytes:
        import httpx
        resp = httpx.post(
            TTS_URL.format(voice_id=voice_id),
            params={"output_format": OUTPUT_FORMAT},
            headers={"xi-api-key": api_key, "Content-Type": "application/json"},
            json={"text": text, "model_id": model},
            timeout=TIMEOUT_S)
        if resp.status_code != 200:
            raise RenderError(f"ElevenLabs refused a line: HTTP "
                              f"{resp.status_code}")
        return resp.content
    return fetch


class Renderer:
    """Renders lines through `fetch`, or from the cache. `fetch` is None
    when there's no key, and then only cached lines can be spoken."""

    def __init__(self, cache_dir, fetch=None, model: str = DEFAULT_MODEL,
                 voices=None):
        self.dir = Path(cache_dir) / "lines"
        self.fetch = fetch
        self.model = model
        self.voices = voices or cast_mod.voices()
        self.new_chars = self.cached_chars = 0
        self.new_lines = self.cached_lines = 0

    def path_for(self, speaker: str, text: str) -> Path:
        key = cache_key(self.voices[speaker].voice_id, self.model, text)
        return self.dir / key[:2] / f"{key}.pcm"

    def line(self, speaker: str, text: str) -> bytes:
        if speaker not in self.voices:
            raise RenderError(f"{speaker!r} has no voice in the cast")
        path = self.path_for(speaker, text)
        if path.exists():
            self.cached_chars += len(text)
            self.cached_lines += 1
            return path.read_bytes()
        if self.fetch is None:
            raise RenderError(
                "a line isn't in the cache and there's no ElevenLabs key to "
                "render it; pass --env with ELEVENLABS_API_KEY")
        pcm = self.fetch(self.voices[speaker].voice_id, self.model, text)
        if not pcm or len(pcm) % 2:
            raise RenderError("ElevenLabs sent back audio of the wrong shape")
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".part")
        with os.fdopen(fd, "wb") as fh:
            fh.write(pcm)
        os.replace(tmp, path)
        self.new_chars += len(text)
        self.new_lines += 1
        return pcm

    def stats(self) -> dict:
        return {"model": self.model, "new_chars": self.new_chars,
                "cached_chars": self.cached_chars, "new_lines": self.new_lines,
                "cached_lines": self.cached_lines}
