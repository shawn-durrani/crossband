"""Background identity work must never starve the request path (#133).

The field failure: in room mode, replies stalled hard right after a
still-learning guest's turns. Every utterance of a not-yet-sufficient
bank triggers clip banking plus the pairwise hygiene audit, and all of
it ran via asyncio.to_thread - the SAME default executor /send uses to
persist the user's message before a round can dispatch. The room sat in
"listening" while background voice-ID work held every worker.

The contract under test:

- diarize.py owns a dedicated, bounded executor: no call site in the
  module uses asyncio.to_thread (source-greped, so a future edit cannot
  quietly reintroduce the starvation), and the pool is its own;
- the hygiene audit is debounced: inside the cool-down window a changed
  bank defers (nothing lost - the fingerprint keeps the debt on the
  books and the next call after the window runs it).
"""

import pathlib
import time

from backend import diarize, voiceid

SRC = pathlib.Path(diarize.__file__).read_text()


def test_no_diarize_call_site_uses_the_default_executor():
    calls = [l for l in SRC.splitlines()
             if "asyncio.to_thread(" in l and not l.strip().startswith("#")]
    assert calls == [], calls


def test_the_voice_executor_is_dedicated_and_bounded():
    assert diarize._VOICE_EXECUTOR._max_workers == 2
    assert diarize._VOICE_EXECUTOR._thread_name_prefix == "voiceid"


def test_audit_debounce_defers_and_never_forgets(monkeypatch):
    voiceid._reset_for_tests()
    ran = []
    monkeypatch.setattr(voiceid, "audit_banks", lambda cfg: ran.append(1) or True)
    monkeypatch.setattr(voiceid, "_get_extractor", lambda cfg: object())

    fingerprints = iter(["a", "b", "b", "c"])
    class _Store:
        def clip_fingerprint(self):
            return next(fingerprints)
    monkeypatch.setattr(voiceid.anchors, "store", lambda: _Store())

    assert voiceid.audit_banks_if_changed({}) is True      # 'a': runs
    assert voiceid.audit_banks_if_changed({}) is False     # 'b': in window
    assert len(ran) == 1
    # window passes: the SAME changed shape ('b') now runs - deferred,
    # never dropped
    monkeypatch.setattr(voiceid, "_audit_last_ran",
                        time.monotonic() - voiceid.AUDIT_MIN_INTERVAL_S - 1)
    assert voiceid.audit_banks_if_changed({}) is True
    assert len(ran) == 2
    # unchanged shape stays a no-op regardless of the window
    monkeypatch.setattr(voiceid, "_audit_last_ran", 0.0)
    fingerprints = iter(["b"])
    assert voiceid.audit_banks_if_changed({}) is False
    voiceid._reset_for_tests()
