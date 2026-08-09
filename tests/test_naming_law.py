"""Naming is law (#28): owner-set names are locked and universal, spoken
corrections set them, and variants merge instead of duplicating.

What these tests pin, in order:

1. OWNER-SET LOCK. set_preferred_name marks the record owner-set; once set,
   every automated path (alias capture, anything calling with
   owner_set=False) is refused, and only another owner action changes it.
2. THE PREFERRED NAME WINS EVERYWHERE. The projection's turn head and
   crosstalk tail speak the preferred name (with the owner-confident-equals-
   unlabelled byte-identity pin untouched), memory ingest resolves merged-
   away names to guest:<preferred>, and the roster snapshot already carries
   display_name (pinned in test_room_identify).
3. SPOKEN CORRECTIONS. "her name is spelt ..." rides the same never-awaited
   scan: the prefilter gates one utility call, a confirmed correction sets
   the preferred name owner-set, matching is conservative (roster/remembered
   by the old name, else the most recent confidently-labelled speaker), and
   ambiguity does nothing rather than guessing. Verdict-line outcomes are
   allowlisted.
4. VARIANTS MERGE. An introduction name that is confidently a folded-form
   variant (case, diacritics, edit distance scaled by length) of a
   remembered person re-identifies them instead of minting a twin; a
   close-but-not-confident name raises ONE merge-question flag; a confident
   voice match on the introduction utterance re-identifies even when the
   name is nothing alike, and never seeds the owner's anchor from the
   guest's audio.
5. THE MERGE ENDPOINT. Renaming person A to a name that belongs to B
   returns the conflict instead of renaming; the merge endpoint folds banks
   under keep-best-N (evicted files deleted), keeps the OLDEST person_id,
   re-points roster rows, resolves the answered merge question, and the
   survivor answers to both names.
6. FORGET STAYS FORGOTTEN. A forgotten person's name reappearing via
   introduction creates a fresh record with a fresh (empty) bank.
"""

import json
import os
import time

import pytest
from fastapi.testclient import TestClient

from backend import anchors, db, diarize, introductions, memory_client
from backend.app import create_app
from backend.config import Settings
from backend.providers import _crosstalk_tail, _user_turn_head


@pytest.fixture
def app(tmp_path):
    diarize._ROOM_ENABLED.clear()
    diarize._STASHED.clear()
    diarize._LAST_DECISION.clear()
    settings = Settings(data_dir=str(tmp_path / "data"),
                        memory_url="http://127.0.0.1:1")
    return create_app(settings)


@pytest.fixture
def utility(monkeypatch):
    """Mock the utility model at the llm_util seam, same shape as the intro
    suite's: state['verdict'] is returned (JSON), state['calls'] records
    prompts."""
    state = {"verdict": {}, "calls": []}

    async def fake_utility(prompt, cfg, max_tokens=2000):
        state["calls"].append(prompt)
        return json.dumps(state["verdict"])

    monkeypatch.setattr("backend.llm_util.utility_complete", fake_utility)
    return state


def loud_pcm(seconds, sample_rate=16000):
    return b"\x00\x40" * int(seconds * sample_rate)


def _wait_for(pred, timeout=6.0, interval=0.02):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        v = pred()
        if v:
            return v
        time.sleep(interval)
    return pred()


def _mint_sufficient(name, clips=3):
    store = anchors.store()
    pid = store.ensure_person(name)
    for _ in range(clips):
        assert store.add_clip(pid, loud_pcm(2.0), 16000, source="accumulated")
    return pid


def _mk_chat():
    con = db.connect()
    try:
        cur = con.execute("INSERT INTO chats(title, created_at, updated_at) "
                          "VALUES('t', 0, 0)")
        con.commit()
        return cur.lastrowid
    finally:
        con.close()


def _roster(chat_id):
    con = db.connect()
    try:
        return db.get_room_roster(con, chat_id, present_only=True)
    finally:
        con.close()


def _flags(chat_id, open_only=True):
    con = db.connect()
    try:
        return db.get_room_flags(con, chat_id, open_only=open_only)
    finally:
        con.close()


def _send(client, chat_id, text):
    with client.stream("POST", f"/api/chats/{chat_id}/send",
                       json={"text": text}) as r:
        body = b"".join(r.iter_bytes())
    assert b'"done"' in body
    return body


CFG = {"user_name": "Alex", "room_roster_max": 6}


# ── 1. the owner-set lock ───────────────────────────────────────────────────

def test_owner_set_locks_the_name_against_automated_paths(app):
    with TestClient(app, base_url="http://127.0.0.1"):
        store = anchors.store()
        pid = store.ensure_person("Mateo")
        # An automated path may set a preferred name while unlocked...
        assert store.set_preferred_name(pid, "Teo", owner_set=False)
        person = next(p for p in store.people() if p["person_id"] == pid)
        assert person["preferred_name"] == "Teo"
        assert person["owner_set"] is False
        # ...the owner's word locks it...
        assert store.set_preferred_name(pid, "Matteo")
        person = next(p for p in store.people() if p["person_id"] == pid)
        assert person["preferred_name"] == "Matteo"
        assert person["owner_set"] is True
        # ...and from then on the automated path is refused.
        assert not store.set_preferred_name(pid, "Teo", owner_set=False)
        person = next(p for p in store.people() if p["person_id"] == pid)
        assert person["preferred_name"] == "Matteo"
        # Another OWNER action still wins - the owner may change their mind.
        assert store.set_preferred_name(pid, "Mat")
        assert next(p for p in store.people()
                    if p["person_id"] == pid)["preferred_name"] == "Mat"


def test_alias_capture_stays_creation_only_and_unlocked(app):
    """A re-introduction's alias never touches an existing person's name -
    and the alias it DOES set at creation is not owner-set, so the owner's
    later correction always wins."""
    with TestClient(app, base_url="http://127.0.0.1"):
        chat_id = _mk_chat()
        introductions.apply_scan(
            chat_id, {"introductions": ["Samantha"], "departures": [],
                      "aliases": {"Samantha": "Sam"}}, CFG)
        person = anchors.store().find_by_name("Samantha")
        assert person["preferred_name"] == "Sam"
        assert person["owner_set"] is False
        # a later re-introduction with a different alias changes nothing
        introductions.apply_scan(
            chat_id, {"introductions": ["Samantha"], "departures": [],
                      "aliases": {"Samantha": "Sammy"}}, CFG)
        assert anchors.store().find_by_name("Samantha")["preferred_name"] == "Sam"


def test_rename_endpoint_sets_owner_set(app):
    with TestClient(app, base_url="http://127.0.0.1") as c:
        pid = anchors.store().ensure_person("Mateo")
        assert c.post(f"/api/voice/people/{pid}/name",
                      json={"name": "Matteo"}).json() == {"ok": True}
        person = c.get("/api/voice/people").json()["people"][0]
        assert person["preferred_name"] == "Matteo"
        assert person["owner_set"] is True


# ── 2. the preferred name wins everywhere ───────────────────────────────────

def _labelled_msg(labels, uncertain=(), extra=None):
    payload = {"clusters": ["c%d" % i for i in range(len(labels))],
               "labels": list(labels), "uncertain": list(uncertain)}
    if extra:
        payload.update(extra)
    return {"id": 1, "speaker": "user", "content": "hi",
            "created_at": 1751600000.0, "voice_labels": json.dumps(payload)}


def test_projection_head_speaks_the_preferred_name():
    cfg = {"user_name": "Alex",
           "preferred_names": {"mateo": "Matteo", "samm": "Sam"}}
    assert _user_turn_head(_labelled_msg(["Mateo"]), cfg) \
        == "Matteo (in the room)"
    # merged-away identity names resolve too
    assert _user_turn_head(_labelled_msg(["Samm"]), cfg) \
        == "Sam (in the room)"
    # an unknown name and an absent map pass through unchanged
    assert _user_turn_head(_labelled_msg(["Dave"]), cfg) \
        == "Dave (in the room)"
    assert _user_turn_head(_labelled_msg(["Mateo"]), {"user_name": "Alex"}) \
        == "Mateo (in the room)"


def test_owner_confident_byte_identity_pin_survives_the_preferred_map():
    """Confidently the owner alone still renders as the bare owner name -
    even when the owner's own record carries a preferred spelling. The
    owner-confident-equals-unlabelled pin outranks display preference."""
    cfg = {"user_name": "Alex", "preferred_names": {"alex": "Aleks"}}
    assert _user_turn_head(_labelled_msg(["Alex"]), cfg) == "Alex"


def test_uncertain_labels_never_leak_a_preferred_name():
    cfg = {"user_name": "Alex", "preferred_names": {"mateo": "Matteo"}}
    head = _user_turn_head(_labelled_msg(["Mateo"], uncertain=["Mateo"]), cfg)
    assert "Matteo" not in head and "Mateo" not in head
    assert "unidentified speaker" in head.lower()


def test_crosstalk_tail_speaks_the_preferred_name():
    cfg = {"user_name": "Alex", "preferred_names": {"mateo": "Matteo"}}
    msg = _labelled_msg(["Mateo", "Voice 2"], uncertain=["Voice 2"], extra={
        "crosstalk": True, "overlap": False,
        "segments": [{"label": "Mateo", "text": "over here",
                      "uncertain": False},
                     {"label": "Voice 2", "text": "hello", "uncertain": True}],
    })
    tail = _crosstalk_tail(msg, cfg)
    assert 'Matteo: "over here"' in tail
    assert 'unidentified speaker: "hello"' in tail
    assert "Mateo:" not in tail


def test_ingest_resolves_merged_names_to_the_preferred_name():
    msg = {"speaker": "user", "id": 7,
           "voice_labels": json.dumps({"labels": ["Samm"], "uncertain": []})}
    speaker = memory_client.ingest_speaker(
        msg, frozenset(), "Alex", {"samm": "Sam"})
    assert speaker == "guest:Sam"


# ── 3. spoken corrections ───────────────────────────────────────────────────

def test_correction_prefilter_truth_table():
    yes = [
        "her name is spelt Samm with two Ms",
        "it's actually spelt with a C",
        "call her Sam",
        "my name is spelt Aleks",
        "his name is actually Mateo",
        "you're spelling it wrong",
        "spell it Matteo",
    ]
    no = [
        "let's plan the weekend",
        "what's the weather",
        "spell check the document",
    ]
    for t in yes:
        assert introductions.correction_prefilter(t), t
    for t in no:
        assert not introductions.correction_prefilter(t), t
    assert not introductions.correction_prefilter("")
    assert not introductions.correction_prefilter(None)


def test_parse_correction_verdict_is_defensive():
    parse = introductions.parse_correction_verdict
    assert parse(None) == []
    assert parse("not json") == []
    assert parse(json.dumps({"corrections": "nope"})) == []
    assert parse(json.dumps({"corrections": [42, None, {}]})) == []
    # relationship nouns are never a corrected name
    assert parse(json.dumps({"corrections": [
        {"who": "Sam", "name": "Wife"}]})) == []
    # the owner marker survives; names are cleaned; the list is bounded
    out = parse(json.dumps({"corrections": [
        {"who": "owner", "name": "  aleks  "},
        {"who": "sam!", "name": "Samantha"},
    ]}))
    assert out == [{"who": "owner", "name": "Aleks"},
                   {"who": "Sam", "name": "Samantha"}]
    many = json.dumps({"corrections": [
        {"who": "", "name": f"Name{i}"} for i in range(20)]})
    assert len(parse(many)) == introductions.MAX_NAMES_PER_TURN


def test_resolve_correction_target_rules():
    resolve = introductions.resolve_correction_target
    people = [
        {"name": "Sam", "preferred_name": "Sam", "merged_names": ["Samm"]},
        {"name": "Mateo", "preferred_name": "Matteo", "merged_names": []},
    ]
    # the owner marker and an owner-alias both hit the owner record
    assert resolve("owner", "Alex", people, []) == "Alex"
    assert resolve("Alec", "Alex", people, []) == "Alex"  # one edit: the owner
    # exact known names (identity, preferred, merged) resolve to the identity
    assert resolve("Sam", "Alex", people, []) == "Sam"
    assert resolve("Samm", "Alex", people, []) == "Sam"
    assert resolve("Matteo", "Alex", people, []) == "Mateo"
    # a confident variant resolves; a stranger does not
    assert resolve("Matteoo", "Alex", people, []) == "Mateo"
    assert resolve("Dave", "Alex", people, []) == ""
    # no target: the recent confidently-labelled speaker, else nothing
    assert resolve("", "Alex", people, [], recent_speaker="Sam") == "Sam"
    assert resolve("", "Alex", people, []) == ""
    # a roster-only person (no anchor record yet) resolves too
    assert resolve("Dave", "Alex", people, ["Dave"]) == "Dave"


def test_spoken_correction_sets_an_owner_set_name(app, utility, caplog):
    utility["verdict"] = {"corrections": [{"who": "Sam", "name": "Samantha"}]}
    with TestClient(app, base_url="http://127.0.0.1") as c:
        anchors.store().ensure_person("Sam")
        chat = c.post("/api/chats", json={"participant_ids": []}).json()
        with caplog.at_level("INFO", logger="crossband.introductions"):
            _send(c, chat["id"], "by the way, her name is spelt Samantha")
            assert _wait_for(lambda: anchors.store().find_by_name("Sam")
                             ["preferred_name"] == "Samantha")
        person = anchors.store().find_by_name("Sam")
        assert person["owner_set"] is True
        assert person["name"] == "Sam"  # identity untouched
        verdicts = [r.message for r in caplog.records
                    if "introduction scan verdict" in r.message]
        assert any("outcome=name_corrected" in v for v in verdicts)


def test_ambiguous_correction_does_nothing(app, utility, caplog):
    """No named target, no confidently-labelled recent speaker: the scan
    must do nothing rather than guess - pinned with two remembered people so
    a 'just pick one' bug has something to wrongly pick."""
    utility["verdict"] = {"corrections": [{"who": "", "name": "Aleks"}]}
    with TestClient(app, base_url="http://127.0.0.1") as c:
        anchors.store().ensure_person("Sam")
        anchors.store().ensure_person("Dave")
        chat = c.post("/api/chats", json={"participant_ids": []}).json()
        with caplog.at_level("INFO", logger="crossband.introductions"):
            _send(c, chat["id"], "it's actually spelt with a K")
            _wait_for(lambda: any("outcome=" in r.message
                                  for r in caplog.records))
        for p in anchors.store().people():
            assert p["preferred_name"] == p["name"]
        verdicts = [r.message for r in caplog.records
                    if "introduction scan verdict" in r.message]
        assert any("outcome=correction_unmatched" in v for v in verdicts)


def test_unnamed_correction_uses_the_recent_labelled_speaker(app, utility):
    utility["verdict"] = {"corrections": [{"who": "", "name": "Samantha"}]}
    with TestClient(app, base_url="http://127.0.0.1") as c:
        anchors.store().ensure_person("Sam")
        chat = c.post("/api/chats", json={"participant_ids": []}).json()
        con = db.connect()
        mid = db.insert_message(con, chat["id"], "user", "hello there")["id"]
        db.set_message_voice_labels(con, mid, {"clusters": ["s0"],
                                               "labels": ["Sam"],
                                               "uncertain": []})
        con.close()
        _send(c, chat["id"], "her name is spelt Samantha")
        assert _wait_for(lambda: anchors.store().find_by_name("Sam")
                         ["preferred_name"] == "Samantha")


def test_owner_corrects_their_own_name(tmp_path, utility):
    utility["verdict"] = {"corrections": [{"who": "owner", "name": "Aleks"}]}
    settings = Settings(data_dir=str(tmp_path / "data"),
                        memory_url="http://127.0.0.1:1", user_name="Alex")
    with TestClient(create_app(settings), base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={"participant_ids": []}).json()
        _send(c, chat["id"], "my name is spelt Aleks")
        assert _wait_for(lambda: (anchors.store().find_by_name("Alex") or {})
                         .get("preferred_name") == "Aleks")
        assert anchors.store().find_by_name("Alex")["owner_set"] is True


def test_correction_outcomes_are_allowlisted():
    assert "name_corrected" in introductions.SCAN_OUTCOMES
    assert "correction_unmatched" in introductions.SCAN_OUTCOMES


def test_scan_verdict_line_stays_singular_for_corrections(app, utility,
                                                          caplog):
    """A correction-shaped turn still ends in exactly ONE verdict line."""
    utility["verdict"] = {"corrections": []}
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={"participant_ids": []}).json()
        with caplog.at_level("INFO", logger="crossband.introductions"):
            _send(c, chat["id"], "spell it Matteo")
            _wait_for(lambda: any("introduction scan verdict" in r.message
                                  for r in caplog.records))
            time.sleep(0.2)
        lines = [r for r in caplog.records
                 if "introduction scan verdict" in r.message]
        assert len(lines) == 1


# ── 4. variants merge instead of duplicating ────────────────────────────────

def test_fold_name_strips_case_and_diacritics():
    fold = introductions.fold_name
    assert fold("Matteo") == "matteo"
    assert fold("Matéo") == "mateo"          # é decomposes; mark drops
    assert fold("  Sám  ") == "sam"
    assert fold("") == ""
    assert fold(None) == ""


def test_name_variant_truth_table():
    nv = introductions.name_variant
    confident, close, different = (introductions.VARIANT_CONFIDENT,
                                   introductions.VARIANT_CLOSE, "")
    assert nv("Mateo", "mateo") == confident          # case only
    assert nv("Matéo", "Mateo") == confident     # diacritics only
    assert nv("Matteo", "Mateo") == confident         # one edit, length >= 4
    assert nv("Samanta", "Samantha") == confident     # one edit, long
    assert nv("Samamtha", "Samantha") == confident    # substitution, long
    assert nv("Sam", "Sal") == close                  # one edit, short name
    assert nv("Dave", "Dan") == close                 # two edits, shared head
    assert nv("Sam", "Dan") == different              # two edits from letter 1
    assert nv("Sam", "Samantha") == different         # a short form, not a slip
    assert nv("Alex", "Dave") == different
    assert nv("", "Sam") == different


def test_variant_of_is_conservative_about_ambiguity():
    people = [
        {"name": "Mateo", "preferred_name": "Mateo", "merged_names": []},
        {"name": "Sam", "preferred_name": "Sam", "merged_names": []},
    ]
    person, confidence = introductions.variant_of("Matteo", people)
    assert person["name"] == "Mateo"
    assert confidence == introductions.VARIANT_CONFIDENT
    # two confident candidates: never guess
    twins = people + [{"name": "Matteo", "preferred_name": "Matteo",
                       "merged_names": []}]
    person, confidence = introductions.variant_of("Mateoo", twins)
    assert person is None and confidence == ""
    # the owner is excluded
    person, _ = introductions.variant_of(
        "Alexx", [{"name": "Alex", "preferred_name": "Alex",
                   "merged_names": []}], exclude={"alex"})
    assert person is None


def test_variant_introduction_reidentifies_instead_of_minting(app):
    with TestClient(app, base_url="http://127.0.0.1"):
        pid = _mint_sufficient("Mateo")
        chat_id = _mk_chat()
        introductions.apply_scan(
            chat_id, {"introductions": ["Matteo"], "departures": []}, CFG)
        roster = _roster(chat_id)
        assert [p["name"] for p in roster] == ["Mateo"]  # the existing identity
        assert roster[0]["person_id"] == pid
        # no twin was minted
        assert [p["name"] for p in anchors.store().people()] == ["Mateo"]
        assert _flags(chat_id) == []  # confident: no merge question


def test_close_variant_raises_one_merge_question(app):
    with TestClient(app, base_url="http://127.0.0.1"):
        _mint_sufficient("Sam")
        chat_id = _mk_chat()
        introductions.apply_scan(
            chat_id, {"introductions": ["Sal"], "departures": []}, CFG)
        # the new person joins (never silently folded on a weak match)...
        assert [p["name"] for p in _roster(chat_id)] == ["Sal"]
        # ...and exactly one merge question stands, naming both.
        flags = [f for f in _flags(chat_id) if f["kind"] == "merge_question"]
        assert len(flags) == 1
        assert flags[0]["label"] == "Sal"
        assert flags[0]["suspected"] == "Sam"
        # a repeat introduction does not stack a second question
        introductions.apply_scan(
            chat_id, {"introductions": ["Sal"], "departures": []}, CFG)
        assert len([f for f in _flags(chat_id)
                    if f["kind"] == "merge_question"]) == 1


def test_voice_match_reidentifies_and_never_poisons_the_owner_bank(
        app, monkeypatch):
    """A self-introduction under a name nothing like the remembered one:
    the utterance's voice confidently matches the remembered bank, so the
    person is re-identified - and the stashed guest audio must NOT become
    the owner's anchor."""
    with TestClient(app, base_url="http://127.0.0.1"):
        pid = _mint_sufficient("Samantha")

        def fake_identify(pcm, sample_rate, candidates, cfg):
            return {"status": "match", "person_id": pid, "name": "Samantha",
                    "score": 0.8, "reason": "match"}

        monkeypatch.setattr("backend.voiceid.identify_utterance",
                            fake_identify)
        chat_id = _mk_chat()
        diarize.stash_utterance(chat_id, loud_pcm(3.0), 16000)
        introductions.apply_scan(
            chat_id, {"introductions": ["Dave"], "departures": []}, CFG)
        roster = _roster(chat_id)
        assert [p["name"] for p in roster] == ["Samantha"]
        assert roster[0]["person_id"] == pid
        # no twin, and the owner gained NO record from the guest's audio
        assert [p["name"] for p in anchors.store().people()] == ["Samantha"]
        assert anchors.store().find_by_name("Alex") is None
        # the stash was discarded, not left for a later claim
        assert diarize.take_stashed_utterance(chat_id) is None


# ── 5. the merge endpoint and the rename conflict ───────────────────────────

def _wav_files(store):
    return {f for f in os.listdir(store.root) if f.endswith(".wav")}


def test_rename_onto_another_person_returns_the_conflict(app):
    with TestClient(app, base_url="http://127.0.0.1") as c:
        store = anchors.store()
        a = store.ensure_person("Mateo")
        store.ensure_person("Sam")
        r = c.post(f"/api/voice/people/{a}/name", json={"name": "Sam"}).json()
        assert r["ok"] is False
        assert r["conflict"]["name"] == "Sam"
        # nothing renamed
        assert store.find_by_name("Mateo")["preferred_name"] == "Mateo"
        # diacritics and case hit the conflict too
        r = c.post(f"/api/voice/people/{a}/name",
                   json={"name": "sám"}).json()
        assert r["ok"] is False


def test_merge_folds_banks_keeps_oldest_id_and_repoints_the_roster(app):
    with TestClient(app, base_url="http://127.0.0.1") as c:
        store = anchors.store()
        older = _mint_sufficient("Sam", clips=4)
        time.sleep(0.01)
        newer = _mint_sufficient("Samm", clips=4)
        chat_id = _mk_chat()
        con = db.connect()
        db.add_room_person(con, chat_id, "Samm", person_id=newer)
        db.insert_room_flag(con, chat_id, "merge_question", label="Samm",
                            suspected="Sam")
        con.close()
        r = c.post(f"/api/voice/people/{newer}/merge",
                   json={"into": older, "name": "Sam"}).json()
        assert r == {"ok": True, "person_id": older}
        people = c.get("/api/voice/people").json()["people"]
        assert len(people) == 1
        survivor = people[0]
        assert survivor["person_id"] == older        # oldest id survives
        assert survivor["preferred_name"] == "Sam"
        assert survivor["owner_set"] is True         # the owner chose it
        assert "Samm" in survivor["merged_names"]
        # keep-best-N holds across the union, and evicted files are deleted
        assert survivor["clip_count"] <= anchors.KEEP_CLIPS
        assert len(_wav_files(store)) == survivor["clip_count"]
        # the roster row re-pointed at the survivor
        assert _roster(chat_id)[0]["person_id"] == older
        # the answered merge question resolved
        assert [f for f in _flags(chat_id)
                if f["kind"] == "merge_question"] == []
        # the survivor answers to both names
        assert store.find_by_name("Samm")["person_id"] == older
        # guards
        assert c.post(f"/api/voice/people/{older}/merge",
                      json={"into": older}).status_code == 400
        assert c.post(f"/api/voice/people/{older}/merge",
                      json={"into": "nope"}).status_code == 404


def test_correction_endpoint_with_a_merged_name_never_mints_a_twin(app):
    """Tap-to-correct sending a merged-away name resolves to the survivor."""
    with TestClient(app, base_url="http://127.0.0.1") as c:
        older = _mint_sufficient("Sam")
        time.sleep(0.01)
        newer = _mint_sufficient("Samm")
        assert c.post(f"/api/voice/people/{newer}/merge",
                      json={"into": older}).json()["ok"] is True
        chat = c.post("/api/chats", json={"participant_ids": []}).json()
        con = db.connect()
        mid = db.insert_message(con, chat["id"], "user", "hello")["id"]
        db.set_message_voice_labels(con, mid, {"clusters": ["s0"],
                                               "labels": ["Voice 1"],
                                               "uncertain": ["Voice 1"]})
        con.close()
        assert c.post(f"/api/chats/{chat['id']}/messages/{mid}/speaker",
                      json={"name": "Samm"}).json()["ok"] is True
        assert len(anchors.store().people()) == 1  # no twin under "Samm"


# ── 6. forget stays forgotten ───────────────────────────────────────────────

def test_forgotten_name_reintroduced_is_a_fresh_record(app):
    with TestClient(app, base_url="http://127.0.0.1") as c:
        store = anchors.store()
        old_pid = _mint_sufficient("Dave")
        assert c.delete(f"/api/voice/people/{old_pid}").json() == {"ok": True}
        chat_id = _mk_chat()
        introductions.apply_scan(
            chat_id, {"introductions": ["Dave"], "departures": []}, CFG)
        roster = _roster(chat_id)
        assert [p["name"] for p in roster] == ["Dave"]
        assert roster[0]["person_id"] == ""   # fresh: nothing remembered
        # no resurrected bank, no merge question about a ghost
        assert store.find_by_name("Dave") is None or \
            store.find_by_name("Dave")["clip_count"] == 0
        assert [f for f in _flags(chat_id)
                if f["kind"] == "merge_question"] == []
