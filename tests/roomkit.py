"""Helpers the room and voice suites share (#238).

Only the ones that were genuinely the same. The review that prompted this
counted seven copies of `FakeEleven` and 45 of `def app` and called them
duplicates; they are not. `FakeEleven` has four variants across its seven
copies and `app` has fifteen across its forty-five, and the differences are
what each suite is actually testing. Flattening those into one signature is
how a set of readable fixtures becomes a parameter list nobody can follow.

What is here is the set that normalised to one body, plus three where the
only difference was a default:

- `loud_pcm`: identical bodies, two different comments.
- `_wait_for`: `timeout=3.0` in one file and `6.0` in nine. Six wins, so the
  one file's failures now take three seconds longer to report.
- `_insert_user_message`: one variant took `voice_turn_id`, the other did not.
  `db.insert_message` defaults it to `""`, so the wider signature is a strict
  superset.

Import what you need. Nothing here is a fixture, so nothing here is magic.
"""
import json
import time

from backend import anchors, db

from conftest import speech_pcm


def loud_pcm(seconds, sample_rate=16000):
    """Speech-shaped PCM-16 at a strong level, passing every gate."""
    return speech_pcm(seconds, sample_rate)


def _pcm(seconds=2.0, rate=16000, amp=6000):
    """Speech-shaped, because the anchor gate rejects a Nyquist square."""
    return speech_pcm(seconds, rate, amp=amp)


def _wait_for(pred, timeout=6.0, interval=0.02):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        v = pred()
        if v:
            return v
        time.sleep(interval)
    return pred()


def sse_events(body):
    return [json.loads(line[6:]) for line in body.splitlines()
            if line.startswith("data: ")]


def _insert_user_message(chat_id, text="hello world", voice_turn_id=""):
    con = db.connect()
    try:
        return db.insert_message(con, chat_id, "user", text,
                                 voice_turn_id=voice_turn_id)
    finally:
        con.close()


def _message_labels(msg_id):
    con = db.connect()
    try:
        row = con.execute("SELECT voice_labels FROM messages WHERE id=?",
                          (msg_id,)).fetchone()
        return row["voice_labels"] if row else None
    finally:
        con.close()


def _chat_room_mode(chat_id):
    con = db.connect()
    try:
        row = con.execute("SELECT room_mode FROM chats WHERE id=?",
                          (chat_id,)).fetchone()
        return bool(row and row["room_mode"])
    finally:
        con.close()


def _stt_usage_rows():
    con = db.connect()
    try:
        return con.execute(
            "SELECT COUNT(*) FROM voice_usage WHERE kind='stt'").fetchone()[0]
    finally:
        con.close()


def _remember(name, clips=3):
    """A person with a bank that counts as remembered: one introduction clip,
    then accumulation. An accumulation-only bank has no remembered-first
    rights, which test_audition_gate.py owns."""
    store = anchors.store()
    pid = store.ensure_person(name)
    assert store.add_clip(pid, loud_pcm(2.0), 16000, source="introduction")
    for _ in range(clips - 1):
        assert store.add_clip(pid, loud_pcm(2.0), 16000, source="accumulated")
    return pid
