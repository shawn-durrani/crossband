"""Eleven v3 holds one accent through a reply (#493).

The owner heard Claude's v3 voice change accent every time it spoke. The
dialogue socket opened at stability 0.5 and got the reply word by word, so
a fresh generation started about every 40 characters, each free to land in
a different accent. Three settings steer it, and these tests pin them:

1. `tts_v3_stability` reaches the socket's open message, and a bad value
   speaks with the default.
2. Every model on the text-to-speech socket sends the same bytes as
   before, whatever the three settings say.
3. Sentence chunks: text is held mid-sentence and sent whole at a sentence
   end, at the 250 character cap, or at a flush or done, and the pieces
   put back together are the reply, so words never run together.
4. `tts_v3_accent_tag` goes in front of every v3 piece and nowhere else:
   not the browser's frames, not the database, not another socket.
5. A tag that isn't one short bracketed phrase is refused on save and
   ignored in config.
6. The settings are documented, and the schema step lands old seats on
   the app's tag.

Keyless: every socket is a fake.
"""

import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend import benchmark, db, tts_models, tts_v3, voice
from tests.test_tts_relay import VOICE, _set, _speak, app, upstreams  # noqa: F401

REPO = Path(__file__).resolve().parents[1]
DIALOGUE, TTS = tts_models.ROUTE_DIALOGUE, tts_models.ROUTE_TTS
TAG = "[Australian accent]"
REPLY = ("Sure, here's the plan for Saturday. Dave brings the ladder at nine! "
         "Is that too early?\nSam can bring lunch.")


def _frames(msgs, carry, route=DIALOGUE):
    return [json.loads(f) for m in msgs
            for f in voice.tts_upstream_frames(route, m, VOICE, carry)]


def _texts(frames):
    return [f["inputs"][0]["text"] for f in frames if "inputs" in f]


def _deltas(text, size=3):
    """A reply the way a model streams it: short pieces, whitespace
    wherever it falls, some pieces only whitespace."""
    out, word = [], ""
    for ch in text:
        if ch.isspace():
            if word:
                out.append(word)
                word = ""
            out.append(ch)
        else:
            word += ch
            if len(word) >= size:
                out.append(word)
                word = ""
    if word:
        out.append(word)
    return out


def _chunked(**over):
    return voice.tts_relay_state({"tts_v3_sentence_chunks": True, **over})


# ---------- 1. stability ----------

@pytest.mark.parametrize("setting,number", [
    ("creative", 0.0), ("natural", 0.5), ("robust", 1.0), ("Robust ", 1.0),
    (None, 1.0), ("", 1.0), ("0.3", 1.0), ("wobbly", 1.0), (0.5, 1.0)])
def test_the_open_message_carries_the_chosen_stability(monkeypatch, setting, number):
    monkeypatch.setattr(voice, "api_key", lambda: "test-key")
    cfg = {} if setting is None else {"tts_v3_stability": setting}
    assert json.loads(voice.tts_open_message(DIALOGUE, cfg, VOICE)) == {
        "voices": [VOICE], "xi_api_key": "test-key",
        "voice_settings": {"stability": number}}


def test_the_relay_opens_v3_with_the_setting(app, upstreams):
    for setting, number in (("creative", 0.0), ("natural", 0.5), ("robust", 1.0)):
        _set(app, tts_model="eleven_v3_conversational", tts_v3_stability=setting)
        with TestClient(app, base_url="http://127.0.0.1") as c:
            _speak(c)
        assert upstreams.last.sent[0]["voice_settings"] == {"stability": number}


def test_a_bad_stability_is_named_once_in_the_log(caplog):
    for _ in range(3):
        tts_v3.stability({"tts_v3_stability": "wobbly"})
    assert [r.getMessage() for r in caplog.records].count(
        "tts_v3_stability 'wobbly' is not creative, natural or robust; "
        "speaking with robust") == 1


# ---------- 2. every other model is untouched ----------

FLASH_OPEN = ('{"text": " ", "xi_api_key": "test-key", "voice_settings": '
              '{"stability": 0.5, "similarity_boost": 0.75, "speed": 1.0}, '
              '"generation_config": {"chunk_length_schedule": [120, 200, 260, 290]}}')


@pytest.mark.parametrize("model", ["eleven_flash_v2_5", "eleven_multilingual_v2"])
def test_the_speech_socket_sends_the_same_bytes_whatever_the_v3_settings(
        app, upstreams, model):
    deltas = ["Hello", " ", "there.", " How", " are", " you?"]
    sent = []
    for over in ({}, {"tts_v3_stability": "creative", "tts_v3_sentence_chunks": True,
                      "tts_v3_accent_tag": TAG},
                 {"tts_v3_stability": "natural", "tts_v3_sentence_chunks": False,
                  "tts_v3_accent_tag": "[strong British accent]"}):
        _set(app, tts_model=model, **over)
        with TestClient(app, base_url="http://127.0.0.1") as c:
            _speak(c, text=deltas)
        sent.append(upstreams.last.raw)
    assert sent[0] == [FLASH_OPEN, '{"text": "Hello"}', '{"text": " "}',
                       '{"text": "there."}', '{"text": " How"}', '{"text": " are"}',
                       '{"text": " you?"}', '{"text": " ", "flush": true}',
                       '{"text": ""}']
    assert sent[1] == sent[0] and sent[2] == sent[0]


def test_speech_socket_frames_never_read_the_v3_state():
    msgs = [{"text": "Hi."}, {"text": " "}, {"text": "Bye"}, {"flush": True, "done": True}]
    plain = _frames(msgs, {}, route=TTS)
    assert _frames(msgs, _chunked(tts_v3_accent_tag=TAG), route=TTS) == plain
    assert plain == [{"text": "Hi."}, {"text": " "}, {"text": "Bye"},
                     {"text": " ", "flush": True}, {"text": ""}]


def test_whole_reply_synthesis_off_v3_posts_what_it_always_did(monkeypatch):
    monkeypatch.setattr(voice, "api_key", lambda: "test-key")
    posted = []

    class _R:
        status_code, content, text = 200, b"mp3", ""

    monkeypatch.setattr(voice.httpx, "post",
                        lambda url, **kw: posted.append(kw["json"]) or _R())
    voice.synthesize("Hi there.", VOICE, {
        "tts_model": "eleven_flash_v2_5", "tts_v3_stability": "creative",
        "tts_v3_accent_tag": TAG})
    assert posted == [{"text": "Hi there.", "model_id": "eleven_flash_v2_5",
                       "voice_settings": {"stability": 0.5, "similarity_boost": 0.75,
                                          "speed": 1.0}}]


# ---------- 3. sentence chunks ----------

def test_mid_sentence_text_is_held_and_keeps_the_socket_alive():
    carry = _chunked()
    frames = _frames([{"text": "Hello"}, {"text": " there"}, {"text": ","},
                      {"text": " Alex"}], carry)
    assert frames == [{"keep_alive": True}] * 4
    assert carry["held"] == "Hello there, Alex"


def test_a_sentence_goes_as_soon_as_the_next_piece_shows_it_ended():
    carry = _chunked()
    assert _frames([{"text": "Hello there."}], carry) == [{"keep_alive": True}]
    assert _frames([{"text": " How"}], carry) == [
        {"inputs": [{"text": "Hello there.", "voice_id": VOICE}]}]
    assert carry["held"] == " How"
    # when one piece carries the end and the next word, it goes at once
    carry = _chunked()
    assert _texts(_frames([{"text": "Sure. Here"}], carry)) == ["Sure."]


@pytest.mark.parametrize("text,ready,held", [
    ("Is it? Yes", "Is it?", " Yes"),
    ("Wow! Then", "Wow!", " Then"),
    ("One.\nTwo", "One.\n", "Two"),
    ("A list\n- item", "A list\n", "- item"),
    ('She said "yes." Then', 'She said "yes."', " Then"),
    ("Done (mostly). Next", "Done (mostly).", " Next"),
    ("Wait... what", "Wait...", " what"),
    ("One. Two. Three", "One. Two.", " Three"),
    ("Pi is 3.14 today", "", "Pi is 3.14 today"),
    ("See example.com now", "", "See example.com now"),
    ("\n\nHi", "", "\n\nHi"),
])
def test_where_a_sentence_ends(text, ready, held):
    assert tts_v3.take_ready(text) == (ready, held)


def test_an_abbreviation_is_split_like_a_sentence_and_the_words_stay_apart():
    carry = _chunked()
    frames = _frames([{"text": "I saw Dr."}, {"text": " Mateo"}, {"text": " today."},
                      {"flush": True, "done": True}], carry)
    assert _texts(frames) == ["I saw Dr.", " Mateo today."]


def test_the_cap_sends_a_long_run_at_a_word_boundary():
    words = " ".join(f"word{i}" for i in range(80))  # 549 characters, no full stop
    carry = _chunked()
    frames = _frames([{"text": d} for d in _deltas(words)] + [{"done": True}], carry)
    texts = _texts(frames)
    assert len(texts) == 3
    assert "".join(texts) == words
    # each capped piece stops right before a space once the held text passes
    # the cap, so no word is cut, and the next piece starts with that space
    at = 0
    for t in texts[:-1]:
        assert tts_v3.SENTENCE_CAP - 10 < len(t) <= tts_v3.SENTENCE_CAP
        at += len(t)
        assert words[at] == " "
    assert all(t.startswith(" ") for t in texts[1:])
    # both capped pieces went out while the model was still writing
    assert [i for i, f in enumerate(frames) if "inputs" in f][:2] < [len(frames) - 2] * 2


def test_the_cap_sends_one_huge_word_whole():
    blob = "x" * 300
    assert tts_v3.take_ready(blob) == (blob, "")
    assert tts_v3.take_ready(" " + blob) == (" " + blob, "")


def test_flush_and_done_send_what_is_held():
    carry = _chunked()
    assert _frames([{"text": "Still going"}, {"flush": True}], carry) == [
        {"keep_alive": True},
        {"inputs": [{"text": "Still going", "voice_id": VOICE}]}, {"flush": True}]
    assert "held" not in carry
    carry = _chunked()
    assert _frames([{"text": "Last bit"}, {"done": True}], carry) == [
        {"keep_alive": True},
        {"inputs": [{"text": "Last bit", "voice_id": VOICE}]}, {"close_socket": True}]
    # the browser's own ending: text, then flush and done in one frame
    carry = _chunked()
    assert _frames([{"text": "The end", "flush": True, "done": True}], carry) == [
        {"keep_alive": True},
        {"inputs": [{"text": "The end", "voice_id": VOICE}]},
        {"flush": True}, {"close_socket": True}]


def test_whitespace_alone_is_never_sent_and_leads_the_next_piece():
    carry = _chunked()
    frames = _frames([{"text": "Hi."}, {"text": " "}, {"flush": True}, {"text": "Bye."},
                      {"done": True}], carry)
    assert frames == [
        {"keep_alive": True},
        {"inputs": [{"text": "Hi.", "voice_id": VOICE}]},
        {"flush": True},
        {"keep_alive": True},
        {"inputs": [{"text": " Bye.", "voice_id": VOICE}]},
        {"close_socket": True}]


@pytest.mark.parametrize("size", [1, 3, 7, 1000])
def test_the_pieces_put_back_together_are_the_reply(size):
    carry = _chunked()
    frames = _frames([{"text": d} for d in _deltas(REPLY, size)]
                     + [{"flush": True, "done": True}], carry)
    texts = _texts(frames)
    assert "".join(texts) == REPLY
    if size < 1000:
        assert texts == ["Sure, here's the plan for Saturday.",
                         " Dave brings the ladder at nine!", " Is that too early?\n",
                         "Sam can bring lunch."]


def test_without_sentence_chunks_each_piece_goes_as_it_arrives():
    carry = voice.tts_relay_state({"tts_v3_sentence_chunks": False})
    frames = _frames([{"text": "Hello"}, {"text": " "}, {"text": "there."},
                      {"flush": True, "done": True}], carry)
    assert frames == [{"inputs": [{"text": "Hello", "voice_id": VOICE}]},
                      {"keep_alive": True},
                      {"inputs": [{"text": " there.", "voice_id": VOICE}]},
                      {"flush": True}, {"close_socket": True}]


def test_sentence_chunks_default_on_through_the_relay(app, upstreams):
    _set(app, tts_model="eleven_v3_conversational")
    with TestClient(app, base_url="http://127.0.0.1") as c:
        got = _speak(c, text=_deltas(REPLY))
    assert got[0] == {"tts_model": "eleven_v3_conversational"}
    assert got[-1] == {"final": True}
    texts = _texts(upstreams.last.sent)
    assert texts[0] == "Sure, here's the plan for Saturday."
    assert "".join(texts) == REPLY


# ---------- 4. the accent tag ----------

def test_the_tag_leads_every_piece_with_and_without_sentence_chunks():
    for chunks in (True, False):
        carry = voice.tts_relay_state({"tts_v3_sentence_chunks": chunks,
                                       "tts_v3_accent_tag": TAG})
        texts = _texts(_frames([{"text": d} for d in _deltas(REPLY)]
                               + [{"flush": True, "done": True}], carry))
        assert texts and all(t.lstrip().startswith(TAG + " ") for t in texts)
        # take the tag back out and the reply is whole again
        assert "".join(t.replace(TAG + " ", "") for t in texts) == REPLY
        assert carry["tag_chars"] == len(texts) * (len(TAG) + 1)


def test_the_tag_goes_after_the_whitespace_that_keeps_words_apart():
    assert tts_v3.with_tag(" How are you?", TAG) == f" {TAG} How are you?"
    assert tts_v3.with_tag("\nNext.", TAG) == f"\n{TAG} Next."
    assert tts_v3.with_tag("First.", TAG) == f"{TAG} First."
    assert tts_v3.with_tag(" As is.", "") == " As is."


def _every_text_value_in_the_database():
    con = db.connect()
    try:
        values = []
        tables = [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")]
        for t in tables:
            for row in con.execute(f"SELECT * FROM {t}"):
                values += [v for v in tuple(row) if isinstance(v, str)]
        return values
    finally:
        con.close()


def test_the_tag_reaches_elevenlabs_and_nothing_else(app, upstreams):
    _set(app, tts_model="eleven_v3_conversational", tts_v3_accent_tag=TAG)
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={}).json()
        con = db.connect()
        db.insert_message(con, chat["id"], "claude", REPLY)
        con.commit()
        con.close()
        got = _speak(c, text=_deltas(REPLY), chat_id=chat["id"])
        messages = c.get(f"/api/chats/{chat['id']}").json()["messages"]
    # upstream: every piece carries it
    texts = _texts(upstreams.last.sent)
    assert texts and all(t.lstrip().startswith(TAG) for t in texts)
    # the browser's frames, the saved message and every row of every table
    # never do
    assert TAG not in json.dumps(got)
    assert [m["content"] for m in messages if m["speaker"] == "claude"] == [REPLY]
    assert not [v for v in _every_text_value_in_the_database() if TAG in v]
    # the tag's characters are metered, since ElevenLabs bills them
    con = db.connect()
    units = con.execute("SELECT units FROM voice_usage WHERE chat_id=?",
                        (chat["id"],)).fetchone()[0]
    con.close()
    assert units == len(REPLY) + len(texts) * (len(TAG) + 1)


def test_only_the_relay_and_its_rules_ever_add_the_tag():
    """The tag exists only in frames sent to ElevenLabs. with_tag is the one
    way to put it on text, so only voice.py may call it."""
    callers = sorted(p.relative_to(REPO).as_posix()
                     for p in (REPO / "backend").rglob("*.py")
                     if "with_tag(" in p.read_text() and p.name != "tts_v3.py")
    assert callers == ["backend/voice.py"]


def test_a_seats_own_tag_wins_and_a_blank_one_follows_the_app(app, upstreams):
    _set(app, tts_model="eleven_v3_conversational", tts_v3_accent_tag=TAG)
    with TestClient(app, base_url="http://127.0.0.1") as c:
        con = db.connect()
        pid, slug = con.execute("SELECT id, slug FROM participants LIMIT 1").fetchone()
        con.close()
        r = c.patch(f"/api/participants/{pid}",
                    json={"tts_v3_accent_tag": "  [strong British accent] "})
        assert r.status_code == 200
        assert r.json()["tts_v3_accent_tag"] == "[strong British accent]"
        _speak(c, seat=slug, text="Good morning.")
        assert _texts(upstreams.last.sent) == ["[strong British accent] Good morning."]
        _speak(c, seat="nobody", text="Good morning.")
        assert _texts(upstreams.last.sent) == [f"{TAG} Good morning."]
        c.patch(f"/api/participants/{pid}", json={"tts_v3_accent_tag": ""})
        _speak(c, seat=slug, text="Good morning.")
        assert _texts(upstreams.last.sent) == [f"{TAG} Good morning."]


def test_a_seat_tag_on_a_speech_socket_model_is_never_sent(app, upstreams):
    with TestClient(app, base_url="http://127.0.0.1") as c:
        con = db.connect()
        pid, slug = con.execute("SELECT id, slug FROM participants LIMIT 1").fetchone()
        con.close()
        c.patch(f"/api/participants/{pid}", json={"tts_v3_accent_tag": TAG})
        _speak(c, seat=slug)
    assert "/text-to-speech/" in upstreams.last.url
    assert not [r for r in upstreams.last.raw if "accent" in r]


def test_the_benchmark_clip_uses_the_seats_tag_and_the_stability(monkeypatch):
    monkeypatch.setattr(voice, "api_key", lambda: "test-key")
    sent = []

    class FakeSync:
        def __init__(self):
            self.replies = [json.dumps({"audio": "QUJD"}), json.dumps({"is_final": True})]

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def send(self, raw):
            sent.append(json.loads(raw))

        def recv(self, timeout=None):
            return self.replies.pop(0)

    import websockets.sync.client as sync_client
    monkeypatch.setattr(sync_client, "connect", lambda url, **kw: FakeSync())
    seat = {"slug": "claude", "tts_model": "eleven_v3_conversational",
            "tts_v3_accent_tag": TAG}
    cfg = benchmark.seat_voice_cfg(seat, {"tts_model": "eleven_flash_v2_5",
                                          "tts_v3_stability": "creative"})
    assert voice.synthesize("Hello there.", VOICE, cfg) == b"ABC"
    assert sent == [
        {"voices": [VOICE], "xi_api_key": "test-key", "voice_settings": {"stability": 0.0}},
        {"inputs": [{"text": f"{TAG} Hello there.", "voice_id": VOICE}]},
        {"close_socket": True}]
    assert benchmark.seat_public({**seat, "name": "Claude"})["tts_v3_accent_tag"] == TAG


# ---------- 5. a bad tag is refused ----------

GOOD_TAGS = ["", "[Australian accent]", " [strong British accent] ",
             "[New Zealand accent]", "[Québécois accent]", "[soft Irish lilt]",
             "[" + "a" * 38 + "]"]
BAD_TAGS = ["Australian accent", "[Australian accent", "Australian accent]",
            "[Australian accent] Say hi", "[Australian] [British]", "[]", "[ ]",
            "[123]", "[<b>bold</b>]", "[Scots\naccent]", "[accent_one]",
            "[" + "a" * 39 + "]", "[[nested]]", None, 3]


@pytest.mark.parametrize("value", GOOD_TAGS)
def test_a_good_tag_is_accepted(value):
    assert tts_v3.valid_tag(value)
    assert tts_v3.clean_tag(value) == value.strip()


@pytest.mark.parametrize("value", BAD_TAGS)
def test_a_bad_tag_is_refused(value):
    assert not tts_v3.valid_tag(value)
    assert tts_v3.clean_tag(value) == ""


def test_a_bad_seat_tag_is_refused_on_save(app):
    with TestClient(app, base_url="http://127.0.0.1") as c:
        con = db.connect()
        pid = con.execute("SELECT id FROM participants LIMIT 1").fetchone()[0]
        con.close()
        for bad in [b for b in BAD_TAGS if isinstance(b, str)]:
            r = c.patch(f"/api/participants/{pid}", json={"tts_v3_accent_tag": bad})
            assert r.status_code == 400, bad
            assert "bracketed phrase" in r.json()["detail"]
        r = c.post("/api/participants", json={
            "name": "Mateo", "provider": "openai", "model": "gpt-5.1",
            "tts_v3_accent_tag": "[Australian accent] and more"})
        assert r.status_code == 400
        r = c.post("/api/participants", json={
            "name": "Mateo", "provider": "openai", "model": "gpt-5.1",
            "tts_v3_accent_tag": f" {TAG}"})
        assert r.json()["tts_v3_accent_tag"] == TAG
        con = db.connect()
        tags = {row[0] for row in con.execute("SELECT tts_v3_accent_tag FROM participants")}
        con.close()
    assert tags == {"", TAG}


def test_a_bad_app_tag_sends_nothing_and_says_why(caplog):
    carry = voice.tts_relay_state({"tts_v3_accent_tag": "[Australian accent] hi"})
    assert carry["tag"] == ""
    assert _texts(_frames([{"text": "Hello.", "done": True}], carry)) == ["Hello."]
    assert "tts_v3_accent_tag is not one [bracketed phrase]" in caplog.text


# ---------- 6. documented, and the schema step ----------

def test_the_three_settings_are_documented_with_their_defaults():
    doc = (REPO / "docs" / "CONFIG.md").read_text()
    for row in ("| `tts_v3_stability` | `robust` |",
                "| `tts_v3_sentence_chunks` | `true` |",
                "| `tts_v3_accent_tag` | `\"\"` |"):
        assert row in doc, row


def test_v30_to_v31_lands_every_seat_on_the_app_tag(tmp_path):
    data = tmp_path / "data31"
    data.mkdir()
    con0 = sqlite3.connect(data / "chat.db")
    con0.executescript(
        "CREATE TABLE participants(id INTEGER PRIMARY KEY, slug TEXT NOT NULL UNIQUE,"
        " name TEXT NOT NULL, provider TEXT NOT NULL, model TEXT NOT NULL,"
        " base_url TEXT, api_key_env TEXT, system_prompt TEXT NOT NULL DEFAULT '',"
        " color TEXT NOT NULL DEFAULT '#a1a1aa', voice_id TEXT NOT NULL DEFAULT '',"
        " voice_gain REAL NOT NULL DEFAULT 1.0, tts_model TEXT NOT NULL DEFAULT '',"
        " reasoning_effort TEXT NOT NULL DEFAULT '',"
        " thinking_control TEXT NOT NULL DEFAULT '', keep_alive TEXT NOT NULL DEFAULT '',"
        " enabled INTEGER NOT NULL DEFAULT 1, position INTEGER NOT NULL DEFAULT 0,"
        " lifecycle TEXT NOT NULL DEFAULT 'trial', created_at REAL NOT NULL);"
        "INSERT INTO participants(slug, name, provider, model, tts_model, created_at)"
        " VALUES('claude', 'Claude', 'anthropic', 'claude-opus-4-8', 'eleven_v3', 1);")
    con0.execute("PRAGMA user_version = 30")
    con0.commit()
    con0.close()
    db.configure(data)
    db.init()
    con = db.connect()
    try:
        assert con.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION == 31
        row = con.execute("SELECT tts_model, tts_v3_accent_tag FROM participants").fetchone()
        assert tuple(row) == ("eleven_v3", "")
    finally:
        con.close()
