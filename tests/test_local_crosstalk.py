"""Crosstalk, split on this computer (#482 item D).

A turn two voices spoke in is labelled from the tracker's spans and
Scribe's word times, on this computer: no voice clip goes to the cloud.
What these tests pin, in order:

1. WHICH TURNS. A second voice counts once it spoke for a second or more;
   a shorter one leaves the turn's label exactly as it was.
2. ATTRIBUTION. Each word goes to the voice speaking at its midpoint:
   sure inside one voice's span, never sure in an overlap (the main voice
   takes it) or far from every span, and a word on a voice too short to
   list goes to the main voice, not sure.
3. THE CLOCKS. Words are lined up with the tracker at the commit, so a
   turn reads the same after the tracker's session restarts or the
   socket reconnects, and a tracker that heard less than Scribe gets the
   marker with no split.
4. THE LABEL. The shape the UI and the seats already render: every voice
   named or "Voice N", crosstalk, overlap and segments. A voice still
   listening is an unidentified speaker to the seats, never the owner, and
   memory files the turn as guest:unknown.
5. THE PASS. A two-voice turn waits for its words only as long as it must,
   a single-voice turn doesn't wait and is labelled as before, and the
   label is parked in time for the message.
6. END TO END. Relay, tracker and pass together, across a tracker restart
   and a socket reconnect.

Keyless and offline: Scribe, the diariser and the speaker model are fakes.
Synthetic roster (Alex the owner, Sam, Dave).
"""

import asyncio
import base64
import json
import time

import pytest
from fastapi.testclient import TestClient

from backend import (crosstalk, db, diarize, memory_client, voice,
                     voice_pass, voice_sessions as vss)
from backend.app import create_app
from backend.config import Settings
from backend.providers import _crosstalk_tail, _user_turn_head
from roomkit import _insert_user_message, _message_labels, _wait_for
from tests.conftest import speech_pcm

SR = 16000
OWNER = "Alex"
ONLY_CFG = {"user_name": OWNER, "diarize_shadow_url": "http://127.0.0.1:8910"}


def _span(slot, start, end, overlap=False):
    return {"slot": slot, "start": start, "end": end, "overlap": overlap}


def _voice(state="named", name="Sam", seconds=2.0, first=0.0):
    return {"state": state, "name": name if state == "named" else "",
            "pid": "p-" + name if state == "named" else "", "score": 0.9,
            "prob": 0.9, "human": False, "seconds": seconds, "first": first}


def _got(voices, spans, main=1, turn_s=3.0, overlap_s=0.0):
    """The naming's answer for a turn, as the live feed gives it."""
    v = voices[main]
    return {"voice": main, "state": v["state"], "name": v["name"],
            "pid": v["pid"], "score": v["score"], "prob": v["prob"],
            "human": False, "method": "calibrated", "voice_clean_s": 5.0,
            "clean_spans": [], "voices_in_turn": len(voices),
            "overlap_s": overlap_s, "voices": voices, "spans": spans,
            "turn_s": turn_s}


# A 3 s turn: Alex alone to 1.6 s, both at once to 1.9 s, Sam alone to 3 s.
SPANS = [_span(1, 0.0, 1.6), _span(1, 1.6, 1.9, True),
         _span(2, 1.6, 1.9, True), _span(2, 1.9, 3.0)]
WORDS = [(0.1, 0.5, "are"), (0.6, 1.0, "we"), (1.1, 1.5, "going"),
         (1.62, 1.85, "now"), (2.0, 2.4, "yes"), (2.5, 2.9, "soon")]
TEXT = "are we going now yes soon"


def _two(sam_state="named"):
    return {1: _voice(name=OWNER, seconds=1.9, first=0.0),
            2: _voice(state=sam_state, name="Sam", seconds=1.4, first=1.6)}


def _entry(words=WORDS, stt_s=3.0):
    return {"words": list(words), "stt_s": stt_s}


# ---------- 1. which turns ----------

def test_a_second_voice_counts_from_one_second():
    voices = {1: _voice(name=OWNER, seconds=2.5),
              2: _voice(seconds=0.6, first=1.0)}
    assert crosstalk.listed_voices(_got(voices, [])) == [1]
    voices[2]["seconds"] = 1.0
    assert crosstalk.listed_voices(_got(voices, [])) == [1, 2]


def test_voices_are_listed_main_first_then_in_the_order_they_spoke():
    voices = {1: _voice(name=OWNER, seconds=1.2, first=2.0),
              2: _voice(seconds=3.0, first=0.5),
              3: _voice(name="Dave", seconds=1.5, first=0.0)}
    assert crosstalk.listed_voices(_got(voices, [], main=2)) == [2, 3, 1]


def test_an_answer_with_no_breakdown_lists_nothing():
    assert crosstalk.listed_voices(None) == []
    assert crosstalk.listed_voices({"voice": 0, "state": "named"}) == []


# ---------- 2. attribution ----------

WEIGHT = {1: 1.9, 2: 1.4}


def _who(words, spans=SPANS, main=1, weight=WEIGHT):
    return [(slot, sure) for slot, sure, _ in
            crosstalk.attribute(words, spans, main, weight)]


def test_a_word_inside_one_voice_is_that_voices_and_sure():
    assert _who([(0.1, 0.5, "a"), (2.0, 2.4, "b")]) == [(1, True), (2, True)]


def test_a_word_goes_by_its_midpoint_at_a_boundary():
    # 1.4 to 1.7: midpoint 1.55, still Alex alone
    assert _who([(1.4, 1.7, "a")]) == [(1, True)]
    # 1.8 to 2.1: midpoint 1.95, Sam alone
    assert _who([(1.8, 2.1, "a")]) == [(2, True)]
    # a span is half open: a midpoint on its end belongs to the next
    spans = [_span(1, 0.0, 1.0), _span(2, 1.0, 2.0)]
    assert _who([(0.9, 1.1, "a")], spans) == [(2, True)]


def test_an_overlapped_word_goes_to_the_main_voice_and_is_never_sure():
    assert _who([(1.62, 1.85, "a")]) == [(1, False)]
    assert _who([(1.62, 1.85, "a")], main=2) == [(2, False)]


def test_an_overlap_without_the_main_voice_goes_to_the_longer_speaker():
    spans = [_span(1, 0.0, 1.0), _span(2, 1.0, 2.0, True),
             _span(3, 1.0, 2.0, True)]
    assert _who([(1.2, 1.6, "a")], spans, main=1,
                 weight={1: 1.0, 2: 1.0, 3: 2.5}) == [(3, False)]


def test_a_word_in_a_gap_goes_to_the_one_voice_near_it():
    spans = [_span(1, 0.0, 1.0), _span(2, 2.0, 3.0)]
    assert _who([(1.05, 1.25, "a")], spans) == [(1, True)]      # 0.15 away
    assert _who([(1.75, 1.95, "a")], spans, main=1) == [(2, True)]
    # near both: the main voice, not sure
    close = [_span(1, 0.0, 1.0), _span(2, 1.3, 2.0)]
    assert _who([(1.1, 1.2, "a")], close, main=2) == [(2, False)]
    # near nobody: the main voice, not sure
    assert _who([(1.4, 1.6, "a")], spans, main=2) == [(2, False)]


def test_a_word_on_a_voice_too_short_to_list_goes_to_the_main_voice():
    got = crosstalk.attribute([(2.6, 2.8, "hm")], [_span(3, 2.45, 2.95)], 1,
                              WEIGHT)
    assert got == [(3, True, "hm")]
    # voice 3 spoke half a second, so the label doesn't list it
    segs = crosstalk.split(got, {1: OWNER, 2: "Sam"}, 1)
    assert segs == [{"label": OWNER, "text": "hm", "uncertain": True}]


def test_words_group_into_segments_by_voice_and_sureness():
    got = crosstalk.attribute(WORDS, SPANS, 1, WEIGHT)
    segs = crosstalk.split(got, {1: OWNER, 2: "Sam"}, 1)
    assert segs == [
        {"label": OWNER, "text": "are we going", "uncertain": False},
        {"label": OWNER, "text": "now", "uncertain": True},
        {"label": "Sam", "text": "yes soon", "uncertain": False}]


def test_too_many_changes_of_voice_is_noise():
    spans = [_span(1 + i % 2, i * 0.5, i * 0.5 + 0.5) for i in range(30)]
    words = [(i * 0.5 + 0.1, i * 0.5 + 0.4, "w") for i in range(30)]
    got = crosstalk.attribute(words, spans, 1, {1: 7.5, 2: 7.5})
    assert crosstalk.split(got, {1: OWNER, 2: "Sam"}, 1) == []


# ---------- 3. the clocks ----------

def test_word_times_are_read_from_the_turns_start_on_the_socket():
    raw = [{"text": "late", "start": 12.3, "end": 12.6, "type": "word"},
           {"text": " ", "start": 12.6, "end": 12.6, "type": "spacing"},
           {"text": "(laughs)", "start": 12.7, "end": 13.0,
            "type": "audio_event"},
           {"text": "", "start": 13.0, "end": 13.1, "type": "word"},
           {"text": "odd", "start": "x", "type": "word"}, "junk"]
    assert crosstalk.turn_words(raw, 12.0) == [(0.3, 0.6, "late"),
                                               (0.7, 1.0, "(laughs)")]


def test_the_same_turn_on_any_socket_clock_gives_the_same_words():
    raw = [{"text": "hi", "start": 0.2, "end": 0.5, "type": "word"}]
    shifted = [dict(raw[0], start=40.2, end=40.5)]
    crosstalk.put_words("a", raw, {"start": 0.0, "end": 1.0})
    crosstalk.put_words("b", shifted, {"start": 40.0, "end": 41.0})
    assert crosstalk.take_words("a")[1] == crosstalk.take_words("b")[1] == {
        "words": [(0.2, 0.5, "hi")], "stt_s": 1.0}


def test_words_line_up_with_the_tracker_at_the_commit():
    assert crosstalk.align(_entry(), 3.0) == WORDS
    # the tracker heard 1.5 s more: audio its feed kept from a socket that
    # died before it committed. Lined up at the commit, the words move on.
    moved = crosstalk.align(_entry(), 4.5)
    assert moved[0] == pytest.approx((1.6, 2.0, "are"))
    # within the slack either way is fine
    assert crosstalk.align(_entry(), 2.8) is not None


def test_a_tracker_that_heard_less_than_scribe_gets_no_split():
    assert crosstalk.align(_entry(), 2.5) is None
    payload = crosstalk.label(_got(_two(), SPANS, turn_s=2.5), [1, 2],
                              _entry())
    assert payload["crosstalk"] is True and "segments" not in payload


def test_no_words_or_no_clock_gives_no_split():
    assert crosstalk.align(None, 3.0) is None
    assert crosstalk.align({"words": None}, 3.0) is None
    assert crosstalk.align(_entry(), None) is None


# ---------- 4. the label ----------

def test_the_label_names_each_voice_and_carries_the_split():
    payload = crosstalk.label(_got(_two(), SPANS, overlap_s=0.3), [1, 2],
                              _entry())
    assert payload == {
        "clusters": ["session"], "labels": [OWNER, "Sam"], "uncertain": [],
        "source": "session", "crosstalk": True, "overlap": True,
        "segments": [
            {"label": OWNER, "text": "are we going", "uncertain": False},
            {"label": OWNER, "text": "now", "uncertain": True},
            {"label": "Sam", "text": "yes soon", "uncertain": False}]}
    # no owner marker on a two-voice turn: it would tick every chip
    assert "owner" not in payload and "score" not in payload


def test_a_voice_still_listening_is_voice_n_and_never_sure():
    payload = crosstalk.label(_got(_two("listening"), SPANS), [1, 2],
                              _entry())
    assert payload["labels"] == [OWNER, "Voice 2"]
    assert payload["uncertain"] == ["Voice 2"]
    assert payload["overlap"] is False
    assert [s["uncertain"] for s in payload["segments"]] == [
        False, True, True]


def test_the_seats_hear_an_unidentified_speaker_never_the_owner():
    cfg = {"user_name": OWNER, "room_mode": True}
    msg = {"speaker": "user", "content": TEXT, "voice_turn_id": "t1",
           "voice_labels": json.dumps(crosstalk.label(
               _got(_two("listening"), SPANS), [1, 2], _entry()))}
    assert _user_turn_head(msg, cfg) == \
        f"{OWNER} + unidentified speaker (in the room)"
    tail = _crosstalk_tail(msg, cfg)
    assert f'{OWNER}: "are we going"' in tail
    assert 'unidentified speaker: "now"' in tail
    assert 'unidentified speaker: "yes soon"' in tail
    assert "Voice 2" not in tail
    # nobody named at all: never the owner
    nobody = {1: _voice("listening", seconds=1.9), 2: _voice(
        "listening", seconds=1.4, first=1.6)}
    msg["voice_labels"] = json.dumps(crosstalk.label(
        _got(nobody, SPANS), [1, 2], _entry()))
    assert _user_turn_head(msg, cfg) == \
        "Unidentified speaker (in the room)"
    assert OWNER not in _crosstalk_tail(msg, cfg)


def test_memory_files_a_two_voice_turn_as_guest_unknown():
    for voices in (_two(), _two("listening")):
        msg = {"id": 1, "speaker": "user", "voice_labels": json.dumps(
            crosstalk.label(_got(voices, SPANS), [1, 2], _entry()))}
        assert memory_client.ingest_speaker(msg, owner_name=OWNER) == \
            "guest:unknown"


def test_the_ui_reads_the_same_keys_it_always_has():
    """frontend/src/voiceChips.js reads labels, uncertain, crosstalk and
    segments of {label, text, uncertain}; nothing else is needed."""
    payload = crosstalk.label(_got(_two("listening"), SPANS), [1, 2],
                              _entry())
    assert payload["crosstalk"] is True
    assert all(set(s) == {"label", "text", "uncertain"}
               and isinstance(s["label"], str) and isinstance(s["text"], str)
               and isinstance(s["uncertain"], bool)
               for s in payload["segments"])
    assert set(payload["uncertain"]) <= set(payload["labels"])


def test_a_split_that_doesnt_read_as_the_message_is_dropped_at_insert():
    payload = crosstalk.label(_got(_two(), SPANS), [1, 2], _entry())
    diarize.park_label("t1", payload)
    assert diarize.claim_label("t1", TEXT.capitalize() + ".") == payload
    diarize.park_label("t2", payload)
    claimed = diarize.claim_label("t2", "a longer turn. " + TEXT)
    assert "segments" not in claimed and claimed["crosstalk"] is True


# ---------- 5. the pass ----------

@pytest.fixture
def app(tmp_path):
    settings = Settings(data_dir=str(tmp_path / "data"),
                        memory_url="http://127.0.0.1:1", user_name=OWNER)
    a = create_app(settings)
    vss._reset_for_tests()
    yield a
    vss._reset_for_tests()


def _chat(client):
    return client.post("/api/chats", json={"participant_ids": []}).json()["id"]


def _run(chat_id, got, monkeypatch, turn="t1"):
    async def await_turn(turn_id, timeout=vss.LIVE_WAIT_S, step=0.02):
        return got
    monkeypatch.setattr(vss, "await_turn", await_turn)
    monkeypatch.setattr(vss, "name_single_turn", lambda *a, **k: None)
    t0 = time.monotonic()
    asyncio.run(voice_pass.run(chat_id, speech_pcm(3.0, amp=4000), SR,
                               db.now(), diarize.RoomSession(),
                               dict(ONLY_CFG), turn))
    return time.monotonic() - t0


def test_a_two_voice_turn_is_labelled_with_its_split(app, monkeypatch):
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = _chat(c)
        m = _insert_user_message(chat, text=TEXT, voice_turn_id="t1")
        crosstalk.put_words("t1", [{"text": w, "start": a + 5.0,
                                    "end": b + 5.0, "type": "word"}
                                   for a, b, w in WORDS],
                            {"start": 5.0, "end": 8.0})
        _run(chat, _got(_two("listening"), SPANS, overlap_s=0.3),
             monkeypatch)
        labels = json.loads(_message_labels(m["id"]))
    assert labels["crosstalk"] is True and labels["overlap"] is True
    assert labels["labels"] == [OWNER, "Voice 2"]
    assert [s["text"] for s in labels["segments"]] == [
        "are we going", "now", "yes soon"]
    # the words were taken: nothing holds the transcript any more
    assert crosstalk.take_words("t1") == (False, None)


def test_words_that_arrive_while_the_pass_waits_are_used(app, monkeypatch):
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = _chat(c)
        m = _insert_user_message(chat, text=TEXT, voice_turn_id="t1")

        async def later():
            await asyncio.sleep(0.2)
            crosstalk.put_words("t1", [{"text": w, "start": a, "end": b,
                                        "type": "word"}
                                       for a, b, w in WORDS],
                                {"start": 0.0, "end": 3.0})

        async def both():
            async def await_turn(turn_id, timeout=vss.LIVE_WAIT_S, step=0.02):
                return _got(_two(), SPANS)
            monkeypatch.setattr(vss, "await_turn", await_turn)
            await asyncio.gather(later(), voice_pass.run(
                chat, speech_pcm(3.0, amp=4000), SR, db.now(),
                diarize.RoomSession(), dict(ONLY_CFG), "t1"))
        asyncio.run(both())
        labels = json.loads(_message_labels(m["id"]))
    assert len(labels["segments"]) == 3


def test_a_turn_with_no_word_times_gets_the_marker_at_once(app, monkeypatch):
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = _chat(c)
        m = _insert_user_message(chat, text=TEXT, voice_turn_id="t1")
        crosstalk.no_words("t1")
        took = _run(chat, _got(_two(), SPANS), monkeypatch)
        labels = json.loads(_message_labels(m["id"]))
    assert labels["crosstalk"] is True and "segments" not in labels
    assert labels["labels"] == [OWNER, "Sam"]
    assert took < crosstalk.WORDS_WAIT_S


def test_words_that_never_come_leave_the_marker_after_the_wait(
        app, monkeypatch):
    monkeypatch.setattr(crosstalk, "WORDS_WAIT_S", 0.1)
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = _chat(c)
        m = _insert_user_message(chat, text=TEXT, voice_turn_id="t1")
        _run(chat, _got(_two(), SPANS), monkeypatch)
        labels = json.loads(_message_labels(m["id"]))
    assert labels["crosstalk"] is True and "segments" not in labels


def test_a_single_voice_turn_is_labelled_as_before_and_never_waits(
        app, monkeypatch):
    waited = []

    async def await_words(turn_id, timeout=None, step=None):
        waited.append(turn_id)
    monkeypatch.setattr(crosstalk, "await_words", await_words)
    one = {1: _voice(name="Sam", seconds=2.8)}
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = _chat(c)
        m = _insert_user_message(chat, text=TEXT, voice_turn_id="t1")
        _run(chat, _got(one, [_span(1, 0.0, 2.8)]), monkeypatch)
        labels = json.loads(_message_labels(m["id"]))
    assert waited == []
    assert labels == {"clusters": ["session"], "labels": ["Sam"],
                      "uncertain": [], "source": "session", "score": 0.9}


def test_a_short_second_voice_leaves_the_label_as_before(app, monkeypatch):
    voices = {1: _voice(name=OWNER, seconds=2.5),
              2: _voice(seconds=0.4, first=2.0)}
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = _chat(c)
        m = _insert_user_message(chat, text=TEXT, voice_turn_id="t1")
        _run(chat, _got(voices, [_span(1, 0.0, 2.5), _span(2, 2.0, 2.4)]),
             monkeypatch)
        labels = json.loads(_message_labels(m["id"]))
    assert labels["labels"] == [OWNER] and labels.get("owner") is True
    assert "crosstalk" not in labels


def test_the_label_is_parked_before_the_message_is_saved(app, monkeypatch):
    """The words come before the browser has the final, so by the time
    /send saves the message the label is waiting to be claimed, split and
    all, and the pass still finds the row it labelled."""
    from backend import anchors
    remembered = []
    monkeypatch.setattr(anchors, "remember_audio",
                        lambda mid, pcm, sr, n: remembered.append((mid, n)))
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = _chat(c)
        crosstalk.put_words("t1", [{"text": w, "start": a, "end": b,
                                    "type": "word"} for a, b, w in WORDS],
                            {"start": 0.0, "end": 3.0})

        async def send_later():
            await asyncio.sleep(0.3)
            con = db.connect()
            try:
                return db.insert_message(
                    con, chat, "user", TEXT, voice_turn_id="t1",
                    voice_labels=diarize.claim_label("t1", TEXT))
            finally:
                con.close()

        async def both():
            async def await_turn(turn_id, timeout=vss.LIVE_WAIT_S, step=0.02):
                return _got(_two(), SPANS)
            monkeypatch.setattr(vss, "await_turn", await_turn)
            msg, _ = await asyncio.gather(send_later(), voice_pass.run(
                chat, speech_pcm(3.0, amp=4000), SR, db.now(),
                diarize.RoomSession(), dict(ONLY_CFG), "t1"))
            return msg
        msg = asyncio.run(both())
    stored = json.loads(msg["voice_labels"])
    assert stored["crosstalk"] is True and len(stored["segments"]) == 3
    # the pass knew the row was its own: the two-voice audio is remembered
    # for tap-to-correct as two voices, so a correction learns nothing
    assert remembered == [(msg["id"], 2)]


def test_correcting_a_two_voice_turn_names_no_session_voice(app,
                                                            monkeypatch):
    """The tap names the turn but can't say which of its voices it meant,
    so no session voice takes the name. The marker stays, the split goes."""
    named = []
    monkeypatch.setattr(vss, "human_named",
                        lambda *a, **k: named.append(a) or True)
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = _chat(c)
        m = _insert_user_message(chat, text=TEXT, voice_turn_id="t1")
        con = db.connect()
        try:
            db.set_message_voice_labels(con, m["id"], crosstalk.label(
                _got(_two("listening"), SPANS), [1, 2], _entry()))
        finally:
            con.close()
        r = c.post(f"/api/chats/{chat}/messages/{m['id']}/speaker",
                   json={"name": "Dave"})
        assert r.status_code == 200
        labels = json.loads(_message_labels(m["id"]))
        assert named == []
        assert labels["labels"] == ["Dave"] and labels["crosstalk"] is True
        assert "segments" not in labels
        # a one-voice turn still names its session voice
        m2 = _insert_user_message(chat, voice_turn_id="t2")
        con = db.connect()
        try:
            db.set_message_voice_labels(con, m2["id"], diarize.label_payload(
                [], clusters=("session",), unresolved="listening"))
        finally:
            con.close()
        c.post(f"/api/chats/{chat}/messages/{m2['id']}/speaker",
               json={"name": "Dave"})
        assert [a[1] for a in named] == ["t2"]


# ---------- 6. end to end ----------

class SessionDiariser:
    """diarserve's /sessions routes: each turn's spans are scripted in TURN
    time and answered in the session's own time, like the real one, so a
    reopened session starts its clock again."""

    def __init__(self, script):
        self.script = list(script)
        self.opened = 0
        self.pushed = {}
        self.turn_start = {}

    def __call__(self, method, url, content=None):
        if method == "POST" and url.endswith("/sessions"):
            self.opened += 1
            sid = f"s{self.opened}"
            self.pushed[sid] = 0.0
            return {"session": sid}
        sid = url.split("/sessions/", 1)[1].split("/", 1)[0]
        if url.endswith("/audio"):
            if self.turn_start.get(sid) is None:
                self.turn_start[sid] = self.pushed[sid]
            self.pushed[sid] += len(content) / 2 / SR
            return {"spans": []}
        if url.endswith("/end-turn"):
            base = self.turn_start.pop(sid, 0.0) or 0.0
            spans = self.script.pop(0) if self.script else []
            return {"spans": [dict(s, start=s["start"] + base,
                                   end=s["end"] + base) for s in spans]}
        return {}


class ScribeFake:
    """Scribe with word times on, counting from its own socket's first
    audio (tests/test_stt_relay.py has the fuller one)."""

    def __init__(self, script):
        self.script = list(script)
        self.queue = asyncio.Queue()
        self.sent_s = 0.0
        self.turn_start = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def __aiter__(self):
        return self

    async def __anext__(self):
        return await self.queue.get()

    async def send(self, raw):
        msg = json.loads(raw)
        audio = base64.b64decode(msg.get("audio_base_64") or "")
        if audio:
            if self.turn_start is None:
                self.turn_start = self.sent_s
            self.sent_s += len(audio) / 2 / SR
        if not msg.get("commit"):
            return
        start, self.turn_start = self.turn_start, None
        words = self.script.pop(0)
        text = " ".join(w for _, _, w in words)
        self.queue.put_nowait(json.dumps(
            {"message_type": voice.PLAIN_FINAL, "text": text}))
        self.queue.put_nowait(json.dumps({
            "message_type": voice.TIMED_FINAL, "text": text,
            "words": [{"text": w, "start": a + start, "end": b + start,
                       "type": "word", "speaker_id": None}
                      for a, b, w in words]}))


ALEX_FP = [1.0, 0.0, 0.0, 0.0]
SAM_FP = [0.0, 1.0, 0.0, 0.0]


@pytest.fixture
def e2e(tmp_path, monkeypatch):
    from backend import auth
    from backend.routers import voice as voice_router
    settings = Settings(data_dir=str(tmp_path / "data"),
                        memory_url="http://127.0.0.1:1", user_name=OWNER,
                        diarize_shadow_url="http://127.0.0.1:8910")
    a = create_app(settings)
    vss._reset_for_tests()
    monkeypatch.setattr(vss, "gate", lambda pcm, sr: None)
    # the first voice sounds like Alex, the second like nobody saved
    monkeypatch.setattr(vss, "embed_live", lambda pcm, sr, cfg: ALEX_FP)
    monkeypatch.setattr(vss, "embed_eres_live", lambda pcm, sr, cfg: None)
    monkeypatch.setattr(vss, "_live_candidates", lambda chat_id: [])
    monkeypatch.setattr(vss, "bank", lambda *a, **k: {
        "alex": {"name": OWNER, "clips": [ALEX_FP]},
        "sam": {"name": "Sam", "clips": [SAM_FP]}})
    monkeypatch.setattr(vss, "_bar", lambda *a, **k: {
        "threshold": 0.5, "margin": 0.1, "source": "t"})
    calls = []
    monkeypatch.setattr(voice.httpx, "post",
                        lambda *a, **k: calls.append(1))
    monkeypatch.setattr(voice_router.engine, "prewarm_recall",
                        lambda *a, **k: None)
    monkeypatch.setattr(voice_router.voice, "enabled", lambda: True)
    monkeypatch.setattr(voice_router.voice, "api_key", lambda: "test-key")
    monkeypatch.setattr(auth, "GATE_LOOPBACK_HOSTS",
                        auth.GATE_LOOPBACK_HOSTS | {"testserver"})
    a.state.allowed_hosts = {"testserver", "127.0.0.1", "localhost", "::1"}
    voice_router._captures.clear()
    yield a, calls, voice_router
    vss._reset_for_tests()


def _speak(ws, pcm, turn_id):
    step = int(0.1 * SR) * 2
    chunks = [pcm[i:i + step] for i in range(0, len(pcm), step)]
    for i, ch in enumerate(chunks):
        frame = {"audio": base64.b64encode(ch).decode(), "sample_rate": SR,
                 "commit": i == len(chunks) - 1}
        if frame["commit"]:
            frame["turn_id"] = turn_id
        ws.send_json(frame)
    return ws.receive_json()


@pytest.mark.parametrize("tracker_restarts,socket_reconnects",
                         [(False, False), (True, False), (False, True),
                          (True, True)])
def test_relay_tracker_and_pass_split_a_turn_end_to_end(
        e2e, monkeypatch, tracker_restarts, socket_reconnects):
    """Turn 1 is Alex alone. Turn 2 is Alex, then both, then Sam. Whether
    the tracker's session restarted between them and whether the browser
    reconnected the socket, turn 2 reads the same."""
    app, cloud, voice_router = e2e
    diar = SessionDiariser([[_span(1, 0.0, 3.0)], SPANS])
    monkeypatch.setattr(vss, "_request", diar)
    turn1 = [(0.2, 0.8, "hello"), (1.0, 1.6, "there"),
             (1.8, 2.6, "everyone")]
    fakes = [ScribeFake([turn1, WORDS])] if not socket_reconnects else [
        ScribeFake([turn1]), ScribeFake([WORDS])]
    monkeypatch.setattr(voice_router.websockets, "connect",
                        lambda *a, **k: fakes.pop(0) if len(fakes) > 1
                        else fakes[0])
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = _chat(c)
        ws_cm = c.websocket_connect("/api/voice/stt-stream")
        ws = ws_cm.__enter__()
        ws.send_json({"chat_id": chat})
        assert ws.receive_json()["session"]
        assert _speak(ws, speech_pcm(3.0, amp=5000), "t1") == {
            "final": "hello there everyone", "turn_id": "t1"}
        m1 = _insert_user_message(chat, text="hello there everyone",
                                  voice_turn_id="t1")
        assert _wait_for(lambda: _message_labels(m1["id"]))
        if tracker_restarts:
            # what a failed call does: the session is dropped, and the next
            # push opens a new one whose clock starts at zero
            with vss._lock:
                vss._sessions.pop(chat)
        if socket_reconnects:
            ws.send_json({"done": True})
            ws_cm.__exit__(None, None, None)
            ws_cm = c.websocket_connect("/api/voice/stt-stream")
            ws = ws_cm.__enter__()
            ws.send_json({"chat_id": chat})
            assert ws.receive_json()["session"]
        assert _speak(ws, speech_pcm(3.0, amp=5000), "t2") == {
            "final": TEXT, "turn_id": "t2"}
        m2 = _insert_user_message(chat, text=TEXT, voice_turn_id="t2")
        raw = _wait_for(lambda: _message_labels(m2["id"]))
        ws.send_json({"done": True})
        ws_cm.__exit__(None, None, None)
    one, two = json.loads(_message_labels(m1["id"])), json.loads(raw)
    assert one["labels"] == [OWNER] and "crosstalk" not in one
    assert two["labels"] == [OWNER, "Voice 2"]
    assert two["uncertain"] == ["Voice 2"]
    assert two["crosstalk"] is True and two["overlap"] is True
    assert two["segments"] == [
        {"label": OWNER, "text": "are we going", "uncertain": False},
        {"label": OWNER, "text": "now", "uncertain": True},
        {"label": "Voice 2", "text": "yes soon", "uncertain": True}]
    assert diar.opened == (2 if tracker_restarts else 1)
    assert cloud == []
