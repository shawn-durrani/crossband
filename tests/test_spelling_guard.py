"""A word spelt out is not a name (#494).

In a Scrabble game on 26 September, "It's spelled good. G-O-A-D." was heard
as a name correction, and the chat posted "Heard a name correction, and
nothing changed". #474 had already told the prompt a spelt word isn't a
name, and the model still wobbles, so a plain rule now stands after the
parse: in a turn that spells a word out, a correction counts only when the
turn marks the word as a name. Every turn here is made up, with the
synthetic roster (Alex the owner, Sam, Dave, Mateo).

1. spelt_words finds letter by letter spellings and nothing else.
2. keep_name_corrections drops game spellings, keeps #474's real
   corrections, leaves a turn with no spelling alone, and never counts
   bare letters on their own, even close to a name in the room.
3. parse_merged applies the rule when given the turn, so the harness and
   the live scan share it, and a game spelling beside a real no-op keeps
   that no-op's line.
4. End to end through /send: the game word never renames the guest who
   spoke last and posts no line, a spelt name still renames them, and a
   spelt name that fits nobody still posts its line.
"""

import json

import pytest
from fastapi.testclient import TestClient

from backend import anchors, db, intent, introductions
from backend.app import create_app
from backend.config import Settings
from roomkit import _wait_for, as_utility_completion

# Word game turns, and the corrections a wobbling model might hand back
# for them: the spelt word, or the word the transcript misheard.
GAME_TURNS = [
    ("Is Z-O-O-S a word?", "Zoos"),
    ("It's spelled good. G-O-A-D.", "Goad"),
    ("It's spelled good. G-O-A-D.", "Good"),
    ("Q-A-T.", "Qat"),
    ("Sam, can I put Q-A-T on the triple word score?", "Qat"),
    ("No, it's J-I-N-X, not jinks.", "Jinx"),
    ("Is Z O O S a word?", "Zoos"),
    ("Q, A, T. That's a real word.", "Qat"),
    ("Hey Dave, is C-A-V-E a word?", "Cave"),
]

# #474's real corrections, spelt out, and the other ways a turn says the
# letters are a name. Each must still count as it is.
REAL_CORRECTIONS = [
    ("It's Mateo, M-A-T-E-O, not Matteo.", {"who": "Matteo", "name": "Mateo"}),
    ("No, no. It's Mateo, M-A-T-E-O.", {"who": "", "name": "Mateo"}),
    ("Sam's name is spelt S-A-M-M, with two Ms.",
     {"who": "Sam", "name": "Samm"}),
    ("My name's spelt M-A-T-E-O, one T.", {"who": "owner", "name": "Mateo"}),
    ("Call her Sam, S-A-M.", {"who": "", "name": "Sam"}),
    ("His surname is M-A-T-E-O-S.", {"who": "", "name": "Mateos"}),
    ("She's called Samm, S-A-M-M.", {"who": "", "name": "Samm"}),
]


# ── 1. spelt_words ─────────────────────────────────────────────────────────

def test_spelt_words_finds_letters_spelt_out():
    assert introductions.spelt_words("It's spelled good. G-O-A-D.") == ["Goad"]
    assert introductions.spelt_words("is z-o-o-s a word") == ["Zoos"]
    assert introductions.spelt_words("Is Z O O S a word?") == ["Zoos"]
    assert introductions.spelt_words("Q, A, T.") == ["Qat"]
    assert introductions.spelt_words("Q.A.T.") == ["Qat"]
    assert introductions.spelt_words("A-L, then Q-A-T") == ["Al", "Qat"]


def test_spelt_words_leaves_ordinary_hyphens_and_abbreviations_alone():
    for text in ("a T-shirt and an x-ray", "Sam-Alex is here", "a co-op",
                 "at 5 p.m. in the U.S.", "plan A, B", "e.g. this one",
                 "IT O O", "", None):
        assert introductions.spelt_words(text) == [], text


# ── 2. keep_name_corrections ───────────────────────────────────────────────

@pytest.mark.parametrize("text,word", GAME_TURNS)
@pytest.mark.parametrize("who", ["", "owner", "Sam"])
def test_a_word_spelt_in_a_game_is_no_correction(text, word, who):
    corr = [{"who": who, "name": word}]
    assert introductions.keep_name_corrections(corr, text) == []


@pytest.mark.parametrize("text,corr", REAL_CORRECTIONS)
def test_a_name_spelt_out_still_counts(text, corr):
    assert introductions.keep_name_corrections([corr], text) == [corr]


def test_a_turn_with_no_spelling_is_untouched():
    for text, corr in (
            ("it's actually spelt with a K", {"who": "", "name": "Aleks"}),
            ("call her Sam", {"who": "", "name": "Sam"}),
            ("Matteo is the spelling but it's pronounced Mateo",
             {"who": "", "name": "Matteo", "also": "Mateo"})):
        assert introductions.keep_name_corrections([corr], text) == [corr]
    assert introductions.keep_name_corrections([], "G-O-A-D") == []


def test_the_name_has_to_be_written_as_a_name_mid_sentence():
    """The transcript capitalises names and little else mid-sentence. A
    capital that only starts a sentence says nothing, and neither does a
    different name nearby, such as the person spoken to."""
    keep = introductions.keep_name_corrections
    mateo = [{"who": "", "name": "Mateo"}]
    assert keep(mateo, "so it's “Mateo”, M-A-T-E-O") == mateo
    assert keep(mateo, "No, Mateo's spelt M-A-T-E-O.") == mateo
    assert keep(mateo, "Mateo. M-A-T-E-O.") == []
    assert keep(mateo, "No. \"Mateo\", M-A-T-E-O.") == []
    assert keep(mateo, "it's mateo, M-A-T-E-O") == []


def test_bare_letters_never_count_on_their_own():
    """"No, it's M-A-T-E-O" after the app named someone Matteo is the same
    shape as "No, it's C-A-V-E" in a game with Dave playing. The words
    can't tell them apart, and a wrong rename is locked, so neither counts.
    Saying the name before spelling it does (REAL_CORRECTIONS)."""
    keep = introductions.keep_name_corrections
    assert keep([{"who": "", "name": "Mateo"}], "No, it's M-A-T-E-O.") == []
    assert keep([{"who": "Matteo", "name": "Mateo"}],
                "No, it's M-A-T-E-O.") == []
    assert keep([{"who": "", "name": "Cave"}], "No, it's C-A-V-E.") == []


# ── 3. parse_merged ────────────────────────────────────────────────────────

def test_parse_merged_applies_the_rule_when_given_the_turn():
    reply = json.dumps({
        "corrections": [{"who": "", "name": "Goad", "also": ""}],
        "depth": [{"seat": "Claude", "depth": "deep", "once": False}]})
    text = "Think harder, Claude. It's spelled good. G-O-A-D."
    heard = intent.parse_merged(reply, text)
    assert heard["corrections"] == []
    assert heard["depth"] == [{"seat": "Claude", "depth": "deep",
                               "once": False}]   # other axes untouched
    # without the turn there is nothing to check, as before
    assert intent.parse_merged(reply)["corrections"] == [
        {"who": "", "name": "Goad"}]


def test_a_game_spelling_beside_a_no_op_keeps_that_no_ops_line():
    reply = json.dumps({"mode_command": "on",
                        "corrections": [{"who": "", "name": "Zoos"}]})
    verdict = intent.parse_merged(reply, "Room mode on. Is Z-O-O-S a word?")
    line = intent.nothing_changed_line(verdict, {"mode_command": "no_change"})
    assert "room mode on" in line and "nothing changed" in line


def test_set_aside_is_an_allowlisted_outcome():
    assert "spelling_set_aside" in introductions.SCAN_OUTCOMES


# ── 4. end to end through /send ────────────────────────────────────────────

@pytest.fixture
def app(tmp_path):
    settings = Settings(data_dir=str(tmp_path / "data"),
                        memory_url="http://127.0.0.1:1")
    return create_app(settings)


@pytest.fixture
def utility(monkeypatch):
    state = {"verdict": {}}

    async def fake_utility(prompt, cfg, max_tokens=2000):
        return json.dumps(state["verdict"])

    monkeypatch.setattr("backend.llm_util.utility_complete_with_usage",
                        as_utility_completion(fake_utility))
    return state


def _spoke(chat_id, name):
    """A turn the voice check confidently named `name`."""
    con = db.connect()
    try:
        mid = db.insert_message(con, chat_id, "user", "hello there")["id"]
        db.set_message_voice_labels(con, mid, {"clusters": ["s0"],
                                               "labels": [name],
                                               "uncertain": []})
    finally:
        con.close()


def _system_lines(chat_id):
    con = db.connect()
    try:
        return [m["content"] for m in db.get_chat_messages(con, chat_id)
                if m["speaker"] == "system"]
    finally:
        con.close()


def _send(client, chat_id, text):
    with client.stream("POST", f"/api/chats/{chat_id}/send",
                       json={"text": text}) as r:
        body = b"".join(r.iter_bytes())
    assert b'"done"' in body


def _verdicts(caplog, chat_id):
    return [r.message for r in caplog.records
            if f"introduction scan verdict: chat={chat_id} " in r.message]


def _scan(client, caplog, chat_id, text):
    with caplog.at_level("INFO", logger="crossband.introductions"):
        _send(client, chat_id, text)
        assert _wait_for(lambda: _verdicts(caplog, chat_id))
    return _verdicts(caplog, chat_id)[0]


@pytest.mark.parametrize("guest,text,word", [
    ("Sam", "It's spelled good. G-O-A-D.", "Goad"),
    ("Matteo", "No, it's M-A-T-E-O.", "Mateo"),
])
def test_spelt_letters_never_rename_the_guest_who_spoke_last(
        app, utility, caplog, guest, text, word):
    """The 26 September shape, and bare letters close to the guest's own
    name, with that guest talking just before. The model hands back the
    spelt word as an unnamed correction, which used to rename the guest,
    locked, or post the stray line."""
    utility["verdict"] = {"corrections": [{"who": "", "name": word}]}
    with TestClient(app, base_url="http://127.0.0.1") as c:
        anchors.store().ensure_person(guest)
        chat = c.post("/api/chats", json={"participant_ids": []}).json()
        _spoke(chat["id"], guest)
        verdict = _scan(c, caplog, chat["id"], text)
        assert "outcome=spelling_set_aside" in verdict
        person = anchors.store().find_by_name(guest)
        assert person["preferred_name"] == guest and not person["owner_set"]
        assert _system_lines(chat["id"]) == []


def test_a_spelt_name_still_renames_the_guest_who_spoke_last(app, utility,
                                                            caplog):
    utility["verdict"] = {"corrections": [{"who": "", "name": "Mateo"}]}
    with TestClient(app, base_url="http://127.0.0.1") as c:
        anchors.store().ensure_person("Sam")
        chat = c.post("/api/chats", json={"participant_ids": []}).json()
        _spoke(chat["id"], "Sam")
        verdict = _scan(c, caplog, chat["id"], "No, no. It's Mateo, M-A-T-E-O.")
        assert "outcome=name_corrected" in verdict
        assert anchors.store().find_by_name("Sam")["preferred_name"] == "Mateo"


def test_a_spelt_name_that_fits_nobody_still_posts_its_line(app, utility,
                                                           caplog):
    """The turn plainly meant a name, and the app couldn't tell whose: the
    owner and the seats both need to know it didn't land (#258)."""
    utility["verdict"] = {"corrections": [{"who": "", "name": "Samm"}]}
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={"participant_ids": []}).json()
        verdict = _scan(c, caplog, chat["id"], "Her name's spelt S-A-M-M.")
        assert "outcome=correction_unmatched" in verdict
        lines = _wait_for(lambda: _system_lines(chat["id"]))
        assert len(lines) == 1 and "Heard a name correction" in lines[0]
