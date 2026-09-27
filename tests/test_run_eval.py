"""run_eval, the seat tool that starts an Analysis page measurement when
someone asks in a chat (#407).

The contract under test:

- The tool takes a measurement and practice or real, nothing else, and
  starts the run through the page's own runner. A content flag in the table
  is refused before anything starts, so the recall replay never runs with
  content from a chat.
- A real run starts only when the round's asking turn came from the owner:
  typed, or spoken and labelled with the owner's name, or spoken alone in a
  solo chat. A guest, an unnamed voice, the TV, crosstalk, a doubted turn or
  a room-mode turn still waiting for its name gets a practice offer and the
  reason. A round no message started starts nothing at all.
- A real run from chat needs an owner password, and at most
  CHAT_PAID_DAILY_CAP start from chat a day. One ask starts each
  measurement once.
- The tool's answer carries the cost and what the run touches. When the run
  settles, a system line with the headline and a link to the report goes
  into the chat, never the report or stderr, and one seat relays it.
- The round offers the tool and fixes the asking turn at its first read.

The harnesses are stubbed with a tiny module on PYTHONPATH, the way
tests/test_analysis.py does it, so nothing here calls a model, membro or the
diariser.
"""

import asyncio
import json
import textwrap
import time
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from backend import analysis, auth, db, engine, guestjobs, run_eval
from backend import tools as tools_mod
from backend.app import create_app
from backend.config import Settings

OWNER = "Alex"
ASK = "run the recall replay"

STUB = textwrap.dedent('''
    import argparse, json, os, sys
    p = argparse.ArgumentParser()
    for flag in ("--format", "--out", "--json-out", "--db", "--memory-url"):
        p.add_argument(flag)
    p.add_argument("--mock", action="store_true")
    a, _ = p.parse_known_args()
    if os.environ.get("STUB_MODE", "ok") == "fail":
        print("boom: the stub gave up", file=sys.stderr)
        sys.exit(3)
    with open(a.out, "w") as fh:
        fh.write("# Stub report\\n\\nEverything the stub measured.\\n")
    with open(a.json_out, "w") as fh:
        json.dump({"n_turns": 4, "turns_with_hits": 3,
                   "recall_ms": {"p50": 12.0}, "corpus": []}, fh)
''')


@pytest.fixture(autouse=True)
def _clean_live():
    analysis._live.clear()
    yield
    analysis._live.clear()


@pytest.fixture
def data_dir(tmp_path):
    d = tmp_path / "data"
    db.configure(d)
    db.init()
    return d


@pytest.fixture
def stub(tmp_path, monkeypatch):
    pkg = tmp_path / "stubs" / "run_eval_stub"
    pkg.mkdir(parents=True)
    (pkg / "__main__.py").write_text(STUB)
    monkeypatch.setenv("PYTHONPATH", str(tmp_path / "stubs"))
    monkeypatch.setenv("STUB_MODE", "ok")
    table = tuple(replace(m, module="run_eval_stub", args=())
                  for m in analysis.MEASUREMENTS)
    monkeypatch.setattr(analysis, "MEASUREMENTS", table)
    monkeypatch.setattr(analysis, "_BY_ID", {m.id: m for m in table})
    monkeypatch.setattr(guestjobs, "RESULT_SETTLE_S", 0.0)


def _enrol():
    con = db.connect()
    try:
        auth.set_owner_password(con, "a-durable-owner-passphrase")
    finally:
        con.close()


def _chat(room_mode=False):
    con = db.connect()
    try:
        cid = con.execute(
            "INSERT INTO chats(created_at, updated_at, room_mode) VALUES(?,?,?)",
            (db.now(), db.now(), int(room_mode))).lastrowid
        con.commit()
        return cid
    finally:
        con.close()


def _say(chat_id, *, spoken=False, labels=None, speaker="user", text=ASK):
    con = db.connect()
    try:
        return db.insert_message(con, chat_id, speaker, text,
                                 voice_turn_id="turn-1" if spoken else "",
                                 voice_labels=labels)["id"]
    finally:
        con.close()


def _flag(chat_id, message_id):
    con = db.connect()
    try:
        db.insert_room_flag(con, chat_id, "mismatch", message_id=message_id,
                            label=OWNER, suspected="Sam")
    finally:
        con.close()


def _messages(chat_id):
    con = db.connect()
    try:
        return [dict(r) for r in con.execute(
            "SELECT speaker, content FROM messages WHERE chat_id=? ORDER BY id",
            (chat_id,))]
    finally:
        con.close()


class Handback:
    def __init__(self):
        self.calls = []

    async def __call__(self, chat_id, kind):
        self.calls.append((chat_id, kind))


def _call(chat_id, message_id, measurement="recall", mode="real",
          handback=None, extra=None):
    """One run_eval call the way a seat makes it, then wait for anything it
    started to settle and hand back. Returns (answer, record or None)."""
    cfg = {"chat_id": chat_id, "_round_asker_id": message_id,
           "user_name": OWNER, "memory_url": "", "_handback": handback}
    args = {"measurement": measurement, "mode": mode, **(extra or {})}

    async def go():
        answer = await tools_mod.run_tool(run_eval.TOOL_NAME, args, cfg)
        job = analysis._live.get(measurement)
        if job is None:
            return answer, None
        run_id = job.record["run_id"]
        await job.task
        deadline = time.monotonic() + 5
        while run_eval._relays and time.monotonic() < deadline:
            await asyncio.sleep(0.02)
        return answer, analysis.load_record(run_id)
    return asyncio.run(go())


def _no_runs(data_dir):
    root = data_dir / "analysis"
    return not root.exists() or not any(root.iterdir())


# ── who asked ───────────────────────────────────────────────────────────────

def _msg(spoken=True, labels=None, speaker="user", mid=7):
    return {"id": mid, "speaker": speaker,
            "voice_turn_id": "turn-1" if spoken else "",
            "voice_labels": json.dumps(labels) if labels else ""}


@pytest.mark.parametrize("msg,room_mode,expected", [
    (_msg(spoken=False), True, run_eval.OWNER),
    (_msg(labels={"labels": [OWNER]}), True, run_eval.OWNER),
    (_msg(labels={"labels": ["alex"]}), False, run_eval.OWNER),
    (_msg(labels={"labels": [OWNER], "corrected": True}), True, run_eval.OWNER),
    (_msg(), False, run_eval.OWNER),
    (_msg(), True, run_eval.PENDING),
    (_msg(labels={"labels": [], "unresolved": "new_voice"}), True,
     run_eval.UNNAMED),
    (_msg(labels={"labels": [], "unresolved": "media"}), True, run_eval.TV),
    (_msg(labels={"labels": [OWNER], "uncertain": [OWNER]}), True,
     run_eval.UNNAMED),
    (_msg(labels={"labels": ["Voice 1"]}), True, run_eval.UNNAMED),
    (_msg(labels={"labels": [OWNER], "crosstalk": True}), True,
     run_eval.CROSSTALK),
    (_msg(labels={"labels": [OWNER, "Sam"]}), True, run_eval.SEVERAL),
    (None, False, run_eval.NOBODY),
    (_msg(speaker="system"), False, run_eval.NOBODY),
    (_msg(speaker="claude"), False, run_eval.NOBODY),
])
def test_who_asked(msg, room_mode, expected):
    assert run_eval.classify(msg, owner_name=OWNER,
                             room_mode=room_mode) == expected


def test_a_guest_is_named_as_the_reason():
    who = run_eval.classify(_msg(labels={"labels": ["Sam"]}),
                            owner_name=OWNER, room_mode=True)
    assert not who.owner and who.anyone
    assert "Sam" in who.why


def test_an_open_doubt_on_the_turn_is_never_the_owner():
    msg = _msg(labels={"labels": [OWNER]})
    assert run_eval.classify(msg, owner_name=OWNER, room_mode=True,
                             open_flag_ids={msg["id"]}) == run_eval.DOUBTED


def test_the_asking_turn_is_the_newest_message_a_person_sent():
    user = {"id": 1, "speaker": "user", "content": ASK}
    system = {"id": 2, "speaker": "system", "content": "Research mode on."}
    seat = {"id": 3, "speaker": "claude", "content": "Sure."}
    assert run_eval.asking_turn_id([user]) == 1
    assert run_eval.asking_turn_id([user, system]) == 1
    assert run_eval.asking_turn_id([user, seat]) is None
    assert run_eval.asking_turn_id([user, {"id": 4, "speaker": "claude-code",
                                           "content": "done"}]) is None
    assert run_eval.asking_turn_id([user], is_handback=True) is None
    assert run_eval.asking_turn_id(
        [{"id": 5, "speaker": "user", "content": "/deploy crossband"}]) is None
    assert run_eval.asking_turn_id([]) is None


# ── the tool's shape ────────────────────────────────────────────────────────

def test_the_tool_takes_a_measurement_and_a_mode_and_nothing_else():
    d = tools_mod.eval_tool_definitions()
    assert [x["name"] for x in d] == ["run_eval"]
    schema = d[0]["input_schema"]
    assert set(schema["properties"]) == {"measurement", "mode"}
    assert schema["required"] == ["measurement", "mode"]
    assert schema["properties"]["measurement"]["enum"] == \
        [m.id for m in analysis.MEASUREMENTS]
    assert schema["properties"]["mode"]["enum"] == ["practice", "real"]
    assert f"at most {run_eval.CHAT_PAID_DAILY_CAP} a day" in d[0]["description"]


def test_any_other_field_is_refused_before_anything_starts(data_dir, stub):
    _enrol()
    cid = _chat()
    mid = _say(cid)
    for extra in ({"with_content": True}, {"args": "--with-content"},
                  {"fixtures_dir": "/somewhere"}):
        answer, rec = _call(cid, mid, extra=extra)
        assert answer.startswith("Not started") and "nothing else" in answer
        assert rec is None
    assert _no_runs(data_dir)


@pytest.mark.parametrize("args", [
    {"measurement": "silence", "mode": "real"},
    {"measurement": "recall", "mode": "paid"},
    {"measurement": "recall"},
])
def test_an_unknown_measurement_or_mode_starts_nothing(data_dir, stub, args):
    _enrol()
    cid = _chat()
    mid = _say(cid)
    cfg = {"chat_id": cid, "_round_asker_id": mid, "user_name": OWNER}
    answer = asyncio.run(tools_mod.run_tool("run_eval", args, cfg))
    assert answer.startswith("Not started")
    assert not analysis._live
    assert _no_runs(data_dir)


# ── the owner starts a real run ─────────────────────────────────────────────

def test_the_owner_typing_starts_a_real_run_and_its_result_comes_back(
        data_dir, stub):
    _enrol()
    cid = _chat(room_mode=True)
    mid = _say(cid)
    hb = Handback()
    answer, rec = _call(cid, mid, handback=hb)
    m = analysis.get("recall")
    assert answer.startswith("Started a real run of the recall replay")
    assert m.costs in answer and m.touches in answer
    assert "today: 1 of 3" in answer
    assert rec["state"] == "done"
    assert rec["practice"] is False
    assert rec["started_from"] == "chat"
    assert rec["chat_id"] == cid and rec["asked_in"] == mid
    posted = _messages(cid)[-1]
    assert posted["speaker"] == "system"
    assert "The recall replay asked for in this chat has finished." in \
        posted["content"]
    assert rec["summary"]["headline"] in posted["content"]
    assert f"(#analysis/{rec['run_id']})" in posted["content"]
    assert "Everything the stub measured" not in posted["content"]
    assert hb.calls == [(cid, "result")]


def test_the_owners_own_voice_starts_a_real_run(data_dir, stub):
    _enrol()
    cid = _chat(room_mode=True)
    mid = _say(cid, spoken=True, labels={"labels": [OWNER], "score": 0.9})
    answer, rec = _call(cid, mid, measurement="critic")
    assert answer.startswith("Started a real run of the critic eval")
    assert rec["practice"] is False


def test_the_owner_speaking_alone_in_a_solo_chat_starts_a_real_run(
        data_dir, stub):
    _enrol()
    cid = _chat(room_mode=False)
    mid = _say(cid, spoken=True)
    answer, rec = _call(cid, mid, measurement="intent")
    assert answer.startswith("Started a real run")
    assert rec["practice"] is False


# ── anyone else gets a practice offer ───────────────────────────────────────

NOT_THE_OWNER = [
    ("guest", True, {"labels": ["Sam"]}, "Sam, a guest"),
    ("unnamed", True, {"labels": [], "unresolved": "new_voice"},
     "a voice the app couldn't name"),
    ("tv", True, {"labels": [], "unresolved": "media"},
     "the TV or radio, not a person"),
    ("pending", True, None, "a voice the app hasn't named yet"),
    ("crosstalk", True, {"labels": [OWNER], "crosstalk": True},
     "two voices talking at once"),
]


@pytest.mark.parametrize("case,room_mode,labels,why", NOT_THE_OWNER,
                         ids=[c[0] for c in NOT_THE_OWNER])
def test_anyone_but_the_owner_gets_a_practice_offer_and_the_reason(
        data_dir, stub, case, room_mode, labels, why):
    _enrol()
    cid = _chat(room_mode=room_mode)
    mid = _say(cid, spoken=True, labels=labels)
    answer, rec = _call(cid, mid)
    assert rec is None and _no_runs(data_dir)
    assert answer.startswith("Not started.")
    assert f"This request came from {why}." in answer
    assert "offer a free practice run" in answer
    answer, rec = _call(cid, mid, mode="practice")
    assert answer.startswith("Started a practice run of the recall replay")
    assert rec["practice"] is True and rec["state"] == "done"
    assert run_eval.paid_from_chat_today() == 0


def test_a_doubted_turn_gets_a_practice_offer(data_dir, stub):
    _enrol()
    cid = _chat(room_mode=True)
    mid = _say(cid, spoken=True, labels={"labels": [OWNER]})
    _flag(cid, mid)
    answer, rec = _call(cid, mid)
    assert rec is None
    assert "a turn whose speaker is in doubt" in answer


def test_a_label_that_lands_after_the_round_began_is_read(data_dir, stub):
    """The turn was still unnamed when the round loaded it. The tool reads
    the row again, so the owner's label that landed since counts."""
    _enrol()
    cid = _chat(room_mode=True)
    mid = _say(cid, spoken=True)
    con = db.connect()
    try:
        db.set_message_voice_labels(con, mid, {"labels": [OWNER]})
    finally:
        con.close()
    answer, rec = _call(cid, mid)
    assert answer.startswith("Started a real run")


def test_a_round_nobody_asked_for_starts_nothing(data_dir, stub):
    """A hand-back relaying a result, or a continue, has no asking turn, so
    a seat can't chain runs from one, not even practice ones."""
    _enrol()
    cid = _chat()
    _say(cid)
    for mode in ("real", "practice"):
        answer, rec = _call(cid, None, mode=mode)
        assert rec is None
        assert "Nobody asked for a run" in answer
    assert _no_runs(data_dir)


def test_a_real_run_from_chat_needs_an_owner_password(data_dir, stub):
    cid = _chat()
    mid = _say(cid)
    answer, rec = _call(cid, mid)
    assert rec is None and _no_runs(data_dir)
    assert "needs an owner password" in answer
    answer, rec = _call(cid, mid, mode="practice")
    assert rec["practice"] is True


# ── limits ──────────────────────────────────────────────────────────────────

def _record(run_id, **fields):
    run_dir = analysis.runs_root() / run_id
    run_dir.mkdir(parents=True)
    (run_dir / analysis.RECORD).write_text(json.dumps({
        "run_id": run_id, "measurement": run_id.split("-")[0],
        "state": "done", "practice": False, "started_from": "chat",
        "created_at_unix": time.time(), **fields}))


def test_the_daily_cap_counts_real_chat_runs_since_midnight(data_dir):
    yesterday = time.time() - 86400
    _record("critic-20260101-000001")
    _record("critic-20260101-000002", state="failed")
    _record("critic-20260101-000003", practice=True)
    _record("critic-20260101-000004", started_from="page")
    _record("critic-20260101-000005", created_at_unix=yesterday)
    _record("recall-20260101-000006", state="stopped")
    assert run_eval.paid_from_chat_today() == 3


def test_a_fourth_real_run_from_chat_in_a_day_is_refused(data_dir, stub):
    _enrol()
    cid = _chat()
    mid = _say(cid)
    for measurement in ("critic", "attribution", "intent"):
        answer, rec = _call(cid, mid, measurement=measurement)
        assert answer.startswith("Started a real run"), answer
    assert run_eval.paid_from_chat_today() == run_eval.CHAT_PAID_DAILY_CAP
    answer, rec = _call(cid, mid, measurement="recall")
    assert rec is None
    assert "capped at 3 a day" in answer and "practice run" in answer
    answer, rec = _call(cid, mid, measurement="recall", mode="practice")
    assert rec["practice"] is True


def test_one_ask_starts_a_measurement_once(data_dir, stub):
    _enrol()
    cid = _chat()
    mid = _say(cid)
    answer, first = _call(cid, mid)
    assert answer.startswith("Started")
    answer, again = _call(cid, mid)
    assert again is None
    assert answer.startswith("Not started again.")
    assert run_eval.paid_from_chat_today() == 1
    answer, _ = _call(cid, _say(cid))
    assert answer.startswith("Started")


def test_a_running_measurement_is_not_started_twice(data_dir, stub):
    _enrol()
    cid = _chat()
    mid = _say(cid)
    analysis._live["recall"] = object()  # a run the page started
    cfg = {"chat_id": cid, "_round_asker_id": mid, "user_name": OWNER}
    answer = asyncio.run(tools_mod.run_tool(
        "run_eval", {"measurement": "recall", "mode": "real"}, cfg))
    assert answer == ("Not started. The recall replay is already running. "
                      "One at a time.")
    assert _no_runs(data_dir)


@pytest.mark.parametrize("flag", ["--with-content", "--show-words",
                                  "--fixtures-dir=/somewhere"])
def test_the_recall_replay_never_runs_with_content_from_chat(
        data_dir, monkeypatch, flag):
    _enrol()
    m = replace(analysis.get("recall"), args=(flag,))
    monkeypatch.setattr(analysis, "_BY_ID", {**analysis._BY_ID, "recall": m})
    cid = _chat()
    mid = _say(cid)
    answer, rec = _call(cid, mid)
    assert rec is None and not analysis._live
    assert answer.startswith("Not started.")
    assert "never starts from a chat" in answer
    assert _no_runs(data_dir)


# ── what goes into the chat ─────────────────────────────────────────────────

def test_a_failed_run_says_so_without_its_stderr(data_dir, stub, monkeypatch):
    monkeypatch.setenv("STUB_MODE", "fail")
    _enrol()
    cid = _chat()
    mid = _say(cid)
    _, rec = _call(cid, mid)
    assert rec["state"] == "failed" and "boom" in rec["error"]
    posted = _messages(cid)[-1]
    assert posted["speaker"] == "system"
    assert "boom" not in posted["content"]
    assert "didn't finish" in posted["content"]
    assert f"(#analysis/{rec['run_id']})" in posted["content"]


def test_a_stopped_run_says_so_and_no_seat_relays_it(data_dir, stub):
    """Stopped on the page, or by the app going down: the line still lands,
    and no hand-back round races the stop."""
    cid = _chat()
    hb = Handback()

    async def go():
        run_eval._poster(cid, hb)({
            "run_id": "voice-20260928-101500", "title": "Voice rig",
            "practice": False, "state": "stopped"})
        await asyncio.sleep(0.05)
    asyncio.run(go())
    posted = _messages(cid)[-1]
    assert posted["content"].startswith(
        "The voice rig asked for in this chat was stopped before it finished.")
    assert hb.calls == []


def test_a_practice_result_says_its_numbers_are_made_up():
    text = run_eval.result_text({
        "run_id": "recall-20260928-101500", "title": "Recall replay",
        "practice": True, "state": "done",
        "summary": {"headline": "Practice run, made-up numbers. Replayed 4 "
                                "turns.", "spent_usd": None}})
    assert text.startswith("The practice run of the recall replay asked for "
                           "in this chat has finished. The numbers are made up")
    assert text.count("made-up") == 0
    assert text.endswith("(#analysis/recall-20260928-101500)")


def test_a_real_result_says_what_it_spent():
    base = {"run_id": "critic-20260928-101500", "title": "Critic eval",
            "practice": False, "state": "done"}
    cheap = run_eval.result_text({**base, "summary": {
        "headline": "Caught 90%.", "spent_usd": 0.004}})
    assert "Caught 90%. It spent under a cent." in cheap
    dear = run_eval.result_text({**base, "summary": {
        "headline": "Caught 90%.", "spent_usd": 1.234}})
    assert "It spent $1.23." in dear


# ── the round offers it and knows who asked ─────────────────────────────────

@pytest.fixture
def app(tmp_path):
    return create_app(Settings(data_dir=str(tmp_path / "data"),
                               memory_url="http://127.0.0.1:1",
                               user_name=OWNER))


def test_every_round_offers_it_and_knows_the_turn_it_answers(app, monkeypatch):
    seen = []

    async def stream_reply(participant, roster, transcript, names, cfg, project,
                           chat_summary, voice_mode, tools=None, memory=None):
        seen.append(([t["name"] for t in (tools or [])],
                     cfg.get("_round_asker_id"), cfg.get("_handback")))
        yield ("text", "ok")

    monkeypatch.setattr(engine.providers, "stream_reply", stream_reply)
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={}).json()
        with c.stream("POST", f"/api/chats/{chat['id']}/send",
                      json={"text": ASK}) as r:
            "".join(r.iter_text())
        asked = [m for m in c.get(f"/api/chats/{chat['id']}").json()["messages"]
                 if m["speaker"] == "user"][0]["id"]
        sent = list(seen)
        seen.clear()
        with c.stream("POST", f"/api/chats/{chat['id']}/continue") as r:
            "".join(r.iter_text())
    assert sent and all("run_eval" in tools for tools, _, _ in sent)
    assert all(asker == asked for _, asker, _ in sent)
    assert all(callable(hb) for _, _, hb in sent)
    assert seen and all(asker is None for _, asker, _ in seen)


def test_a_seat_starts_a_practice_run_and_one_seat_relays_its_result(
        app, stub, monkeypatch):
    """End to end through the real round and dispatch: the first seat calls
    run_eval, the run settles, its line lands in the chat and a hand-back
    round follows with no asking turn."""
    calls = []

    async def stream_reply(participant, roster, transcript, names, cfg, project,
                           chat_summary, voice_mode, tools=None, memory=None):
        calls.append(cfg.get("_round_asker_id"))
        if len(calls) == 1:
            out = await tools_mod.run_tool(
                "run_eval", {"measurement": "recall", "mode": "practice"}, cfg,
                origin_agent=participant["slug"], memory=memory)
            yield ("tool", {"tool": "run_eval", "input": {
                "measurement": "recall", "mode": "practice"}, "output": out})
        yield ("text", "ok")

    monkeypatch.setattr(engine.providers, "stream_reply", stream_reply)
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={}).json()
        with c.stream("POST", f"/api/chats/{chat['id']}/send",
                      json={"text": "do a practice run of the recall replay"}) as r:
            "".join(r.iter_text())
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            msgs = c.get(f"/api/chats/{chat['id']}").json()["messages"]
            posted = [m for m in msgs if m["speaker"] == "system"]
            if posted and len(calls) >= 3:
                break
            time.sleep(0.1)
        seat = [m for m in msgs if m["tool_events"]][0]
    assert seat["tool_events"][0]["output_text"].startswith(
        "Started a practice run of the recall replay")
    assert posted and "#analysis/recall-" in posted[0]["content"]
    assert calls[0] is not None and calls[1] == calls[0]
    assert calls[2] is None  # the relaying seat answers no one's ask
