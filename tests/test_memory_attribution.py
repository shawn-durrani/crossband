"""Guest attribution on memory ingest (#28 phase 3, contract per membro#31).

Room mode means turns from people other than the owner reach the memory
handoff. Before this phase, every one of them ingested as "user" - so a
guest's "I'm allergic to penicillin" would have become a first-person fact
about the owner. These tests pin the four speaker classes one message may
carry into /ingest, at three levels: the pure rule, the leave-hook handoff
end to end, and the wire payload itself.

THE FOUR CASES:
1. the owner's own turns (no labels, or labels confidently the owner) go as
   "user", byte-identically to what has always been sent;
2. a turn confidently attributed to one named guest goes as
   "guest:<preferred name>";
3. an uncertain-labelled turn goes as "guest:unknown" - never the owner,
   never the guessed name;
4. a turn carrying an OPEN attribution flag goes as "guest:unknown" even if
   its label reads confident.

SOURCE_APP stays the permanent historical wire value throughout - membro
keys conversation identity on it. An older membro treats the guest:* classes
as unrecognised (untrusted), so none of this waits on the membro-side build.
"""

import asyncio
import json

import pytest

from backend import anchors, db, engine
from backend.app import create_app
from backend.config import Settings
from backend.memory_client import (GUEST_UNKNOWN, SOURCE_APP, MemoryClient,
                                   ingest_speaker)


def _msg(mid, speaker, labels=None, uncertain=None, voice_labels=None):
    m = {"id": mid, "speaker": speaker, "content": f"m{mid}",
         "created_at": 1751600000.0}
    if voice_labels is not None:
        m["voice_labels"] = voice_labels
    elif labels is not None:
        m["voice_labels"] = json.dumps(
            {"clusters": ["s0"], "labels": labels,
             **({"uncertain": uncertain} if uncertain is not None else {})})
    return m


# ── the pure rule, the four cases ───────────────────────────────────────────

def test_case_1_owner_turns_stay_user_exactly_as_today():
    # no labels at all (typed, or diarization never said anything)
    assert ingest_speaker(_msg(1, "user"), set(), "Shawn") == "user"
    # the schema default and junk both read as unlabelled
    assert ingest_speaker(_msg(2, "user", voice_labels=""), set(), "Shawn") == "user"
    assert ingest_speaker(_msg(3, "user", voice_labels="{broken"), set(), "Shawn") == "user"
    # confidently matched to the owner: still "user"
    assert ingest_speaker(_msg(4, "user", ["Shawn"], []), set(), "Shawn") == "user"
    assert ingest_speaker(_msg(5, "user", ["shawn"], []), set(), "Shawn") == "user"
    # non-user speakers pass through untouched
    assert ingest_speaker(_msg(6, "claude"), set(), "Shawn") == "claude"
    assert ingest_speaker(_msg(7, "system"), set(), "Shawn") == "system"


def test_case_2_confident_guest_turn_carries_their_preferred_name():
    m = _msg(1, "user", ["Lex"], [])
    assert ingest_speaker(m, set(), "Shawn") == "guest:Lex"
    # the correctable display name wins when one is set
    assert ingest_speaker(m, set(), "Shawn",
                          {"lex": "Alex"}) == "guest:Alex"


def test_case_3_uncertain_turn_is_never_the_owner_and_never_the_guess():
    # an eliminated/still-learning name: uncertain, so unknown
    got = ingest_speaker(_msg(1, "user", ["Alex"], ["Alex"]), set(), "Shawn")
    assert got == GUEST_UNKNOWN
    assert "Alex" not in got and "user" not in got
    # phase-1 ordinals are uncertainty by construction
    assert ingest_speaker(_msg(2, "user", ["Voice 2"]), set(),
                          "Shawn") == GUEST_UNKNOWN
    # one confident name + one uncertain voice in the same turn: the turn as
    # a whole is not confidently one person's words
    assert ingest_speaker(_msg(3, "user", ["Alex", "Voice 2"], ["Voice 2"]),
                          set(), "Shawn") == GUEST_UNKNOWN


def test_case_4_open_flag_forces_unknown_even_over_a_confident_label():
    m = _msg(9, "user", ["Alex"], [])
    assert ingest_speaker(m, {9}, "Shawn") == GUEST_UNKNOWN
    # the same turn without the flag would have been Alex's
    assert ingest_speaker(m, set(), "Shawn") == "guest:Alex"


def test_multiple_confident_voices_fail_safe_to_unknown():
    """Two people confidently sharing one turn: the text cannot be split, so
    it must not be attributed to either - and NEVER absorbed as the owner,
    even when the owner is one of the two voices."""
    assert ingest_speaker(_msg(1, "user", ["Shawn", "Alex"], []), set(),
                          "Shawn") == GUEST_UNKNOWN


def test_wire_name_is_single_line_and_colon_free():
    got = ingest_speaker(_msg(1, "user", ["A:l\nex"], []), set(), "Shawn")
    assert got.startswith("guest:")
    body = got[len("guest:"):]
    assert ":" not in body and "\n" not in body


# ── the leave-hook handoff, end to end ──────────────────────────────────────

class _FakeResp:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


class _FakeHttp:
    """Stands in for MemoryClient's httpx.AsyncClient: healthy service,
    every POST recorded."""

    def __init__(self):
        self.posts = []

    async def get(self, url):
        return _FakeResp({"status": "ok", "contract_version": "1.1"})

    async def post(self, url, json=None):
        self.posts.append((url, json))
        return _FakeResp({"ok": True, "job_id": "j1"})


@pytest.fixture
def app(tmp_path):
    return create_app(Settings(data_dir=str(tmp_path / "data"),
                               memory_url="http://127.0.0.1:1"))


def test_handoff_resolves_speakers_and_source_app_is_untouched(app):
    """The whole path: labelled turns in the database go through the leave
    hook and arrive at /ingest carrying the resolved speaker classes -
    all four cases in one conversation - under the permanent SOURCE_APP."""
    cfg = {"user_name": "Shawn"}
    con = db.connect()
    cur = con.execute(
        "INSERT INTO chats(title, created_at, updated_at) VALUES('t', 0, 0)")
    con.commit()
    chat_id = cur.lastrowid
    m_owner = db.insert_message(con, chat_id, "user", "my turn, unlabelled")
    m_guest = db.insert_message(con, chat_id, "user", "a guest's turn")
    db.set_message_voice_labels(con, m_guest["id"],
                                {"clusters": ["s1"], "labels": ["Lex"],
                                 "uncertain": []})
    m_uncertain = db.insert_message(con, chat_id, "user", "who was that")
    db.set_message_voice_labels(con, m_uncertain["id"],
                                {"clusters": ["s2"], "labels": ["Voice 2"],
                                 "uncertain": ["Voice 2"]})
    m_flagged = db.insert_message(con, chat_id, "user", "doubted turn")
    db.set_message_voice_labels(con, m_flagged["id"],
                                {"clusters": ["s1"], "labels": ["Lex"],
                                 "uncertain": []})
    db.insert_room_flag(con, chat_id, "mismatch", message_id=m_flagged["id"])
    m_model = db.insert_message(con, chat_id, "claude", "a reply")
    con.close()
    # Lex's correctable display name is Alex - the ingest must use it
    pid = anchors.store().ensure_person("Lex")
    assert anchors.store().set_preferred_name(pid, "Alex")

    memory = MemoryClient(base_url="http://127.0.0.1:1")
    fake = _FakeHttp()
    memory._client = fake
    asyncio.run(engine.leave_chat_job(chat_id, cfg, memory))

    ingests = [body for url, body in fake.posts if url.endswith("/ingest")]
    assert len(ingests) == 1
    payload = ingests[0]
    assert payload["source_app"] == SOURCE_APP == "multi-model-chat"
    speakers = {int(m["external_id"]): m["speaker"]
                for m in payload["messages"]}
    assert speakers[m_owner["id"]] == "user"
    assert speakers[m_guest["id"]] == "guest:Alex"
    assert speakers[m_uncertain["id"]] == GUEST_UNKNOWN
    assert speakers[m_flagged["id"]] == GUEST_UNKNOWN
    assert speakers[m_model["id"]] == "claude"
    # the durable rows themselves are untouched - attribution is resolved on
    # the way OUT, not rewritten in the transcript
    con = db.connect()
    row = con.execute("SELECT speaker FROM messages WHERE id=?",
                      (m_guest["id"],)).fetchone()
    con.close()
    assert row["speaker"] == "user"


def test_resolved_flag_restores_confident_attribution(app):
    """A flag the owner resolved no longer forces unknown: only OPEN flags
    doubt a turn."""
    cfg = {"user_name": "Shawn"}
    con = db.connect()
    cur = con.execute(
        "INSERT INTO chats(title, created_at, updated_at) VALUES('t', 0, 0)")
    con.commit()
    chat_id = cur.lastrowid
    m = db.insert_message(con, chat_id, "user", "a guest's turn")
    db.set_message_voice_labels(con, m["id"],
                                {"clusters": ["s1"], "labels": ["Alex"],
                                 "uncertain": []})
    db.insert_room_flag(con, chat_id, "mismatch", message_id=m["id"])
    db.resolve_room_flags(con, chat_id, message_id=m["id"])
    con.close()

    memory = MemoryClient(base_url="http://127.0.0.1:1")
    fake = _FakeHttp()
    memory._client = fake
    asyncio.run(engine.leave_chat_job(chat_id, cfg, memory))
    ingests = [body for url, body in fake.posts if url.endswith("/ingest")]
    assert ingests[0]["messages"][0]["speaker"] == "guest:Alex"
