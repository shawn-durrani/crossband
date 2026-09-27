"""Keyless stand-ins, for checking the rig itself. Never for numbers.

MockRenderer speaks each line as a buzz at a pitch of its own per person,
shaped like syllables, about a third of a second a word, so the mixer and
the truth run on audio of the right length. MockAdapter answers from the
truth it's handed, wrong on a fixed few turns, so the report has every
kind of verdict to show.
"""

import hashlib

import numpy as np

from eval_voice import cast as cast_mod
from eval_voice.adapter import Adapter, ConversationResult, EventCheck, Heard
from eval_voice.mix import SAMPLE_RATE, to_pcm

PITCH = {"Alex": 110.0, "Sam": 210.0, "Dave": 125.0, "Mateo": 140.0}
WORD_S = 0.33


class MockRenderer:
    def __init__(self):
        self.model = "mock"
        self.new_chars = self.cached_chars = 0
        self.new_lines = self.cached_lines = 0

    def line(self, speaker: str, text: str) -> bytes:
        n_words = max(1, len(text.split()))
        n = int(n_words * WORD_S * SAMPLE_RATE)
        t = np.arange(n) / SAMPLE_RATE
        f0 = PITCH.get(speaker, 150.0)
        voice = sum(np.sin(2 * np.pi * f0 * k * t) / k for k in (1, 2, 3))
        syllables = 0.55 + 0.45 * np.sin(2 * np.pi * t / WORD_S) ** 2
        pad = np.zeros(int(0.1 * SAMPLE_RATE))
        self.new_chars += len(text)
        self.new_lines += 1
        return to_pcm(np.concatenate([pad, 0.2 * voice * syllables, pad]))

    def stats(self) -> dict:
        return {"model": self.model, "new_chars": self.new_chars,
                "cached_chars": self.cached_chars, "new_lines": self.new_lines,
                "cached_lines": self.cached_lines}


def _slip(script_id: str, index: int) -> bool:
    """A fixed one turn in eleven gets a wrong name."""
    h = hashlib.sha256(f"{script_id}:{index}".encode()).digest()
    return h[0] % 11 == 0


class MockAdapter(Adapter):
    name = "mock"

    def enrol(self, voices: dict) -> dict:
        return {"people": {n: {"clips": 3, "seconds": 28.0} for n in voices}}

    def converse(self, script_id: str, turns: list) -> ConversationResult:
        heard, events = [], []
        roster = list(cast_mod.CAST)
        for mt in turns:
            t = mt.truth
            h = Heard(index=t.index, labelled=True, in_time=True,
                      transcript=" ".join(v.words for v in t.voices))
            if _slip(script_id, t.index):
                h.names = [next(n for n in roster if n not in t.names)]
            elif t.crosstalk:
                h.crosstalk = True
                h.names = list(t.names)
                h.segments = [{"name": v.name, "text": v.words, "unsure": False}
                              for v in t.voices]
            elif t.alone_s(t.main) < 1.5:
                h.placeholders, h.reason = 1, "listening"
            elif not t.enrolled and not t.introduced:
                h.placeholders, h.reason = 1, "new_voice"
            else:
                h.names = [t.main]
                h.learning = not t.enrolled
            heard.append(h)
            for e in t.events:
                kind, value = next(iter(e.items()))
                events.append(EventCheck(index=t.index, kind=kind, value=value,
                                         result="heard", seen=value))
        return ConversationResult(script=script_id, heard=heard, events=events,
                                  diagnostics={"mock": True})
