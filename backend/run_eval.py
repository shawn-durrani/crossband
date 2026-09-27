"""run_eval: the seat tool that starts one of the Analysis page's
measurements when someone asks for it in a chat, typed or spoken (#407).

The owner decided on 28 September that "run the recall replay" should work
in a chat, and accepted that anyone in the room could ask. These guards
cost the owner nothing, so they stay:

- The tool names a measurement and practice or real, and nothing else. It
  starts the run through `analysis.start`, the page's own runner, so one
  run per measurement at a time, the busy route, the report folder, the
  file modes and the content refusal are the page's own. The recall
  replay never runs with content from here either.
- A real run spends money, so it starts only when the turn the round is
  answering came from the owner: typed, or spoken and labelled with the
  owner's own name. A guest's voice, a voice nobody could name, the TV, or
  a room-mode turn still waiting for its name gets a practice run offer
  instead, and the seat is told why. The memory rule
  (memory_client.ingest_speaker) is the floor: a turn memory wouldn't file
  as the owner's is never the owner's here.
- A round nobody's message started (a hand-back, or a continue) starts
  nothing, practice included, so a seat relaying a result can't chain runs.
- A real run from chat needs an owner password, the page's own rule,
  because before one is set anyone on loopback can type into a chat.
- At most CHAT_PAID_DAILY_CAP real runs start from chat each day, counted
  from the run records, so a loop can't spend without limit. Runs started
  on the page don't count.
- One asking turn starts each measurement once, so a second seat or a
  tool loop in the same round can't start it again.
- The tool's answer carries the measurement's own cost and touches lines
  for the seat to say as it starts. When the run settles, a system line
  goes into the chat with the headline, what it spent and a link to the
  report on the Analysis page, and one seat relays it, the way a guest
  visit's result is handed back. The report never goes into the chat, and
  neither does a failed run's stderr.
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass

from . import analysis, auth, db
from .memory_client import GUEST_UNKNOWN, ingest_speaker

log = logging.getLogger("crossband.run_eval")

TOOL_NAME = "run_eval"
MODES = ("practice", "real")
FIELDS = frozenset({"measurement", "mode"})

# Real runs a seat may start from chat per local day. The page has no cap:
# the owner pressing Run is the owner deciding to spend.
CHAT_PAID_DAILY_CAP = 3

# The Analysis page opens a run from this link (frontend/src/analysisView.js
# runFromHash, held to it by tests/fixtures/backend_contract.json).
REPORT_LINK_PREFIX = "#analysis/"

# Unresolved reason the matcher writes on a voice someone said is a TV.
MEDIA = "media"

DESCRIPTION = (
    "Start one of this app's measurements, the ones on its Analysis page, "
    "when someone in this chat asks for one by name, such as \"run the "
    "recall replay\". It runs in the background, and its result is posted "
    "in this chat when it finishes. measurement is one of: "
    + ", ".join(f"\"{m.id}\" (the {m.title.lower()})"
                for m in analysis.MEASUREMENTS)
    + ". mode is \"practice\" (free, made-up numbers, only shows the "
    "harness works) or \"real\" (spends money on the owner's keys, and the "
    "recall replay reads their chat history). A real run starts only when "
    "the owner asked, typed or in their own recognised voice, and at most "
    f"{CHAT_PAID_DAILY_CAP} a day start from chat. When it can't start, the "
    "answer says why, and you say that and offer a practice run. Call it "
    "only when someone asked for a run in the turn you're answering, never "
    "on your own initiative, and tell the room what the run costs and "
    "touches as it starts."
)


def tool_definition() -> dict:
    """The seats' run_eval. The two enums are the whole input: no flag, path
    or option reaches the runner (the page's own rule)."""
    return {
        "name": TOOL_NAME,
        "description": DESCRIPTION,
        "input_schema": {
            "type": "object",
            "properties": {
                "measurement": {
                    "type": "string",
                    "enum": [m.id for m in analysis.MEASUREMENTS],
                    "description": "Which measurement to run.",
                },
                "mode": {
                    "type": "string",
                    "enum": list(MODES),
                    "description": ("\"practice\" for the free run with "
                                    "made-up numbers, \"real\" for the run "
                                    "that costs money."),
                },
            },
            "required": ["measurement", "mode"],
        },
    }


# ---------- who asked ----------

@dataclass(frozen=True)
class Asker:
    """Who the round's asking turn came from, as the tool needs it: the
    owner or not, and when not, the words that finish "This request came
    from ...". `anyone` is False when no message started the round."""
    owner: bool
    why: str = ""
    anyone: bool = True


OWNER = Asker(True)
NOBODY = Asker(False, "no message: this round started on its own, to relay "
                      "a result or carry on", anyone=False)
PENDING = Asker(False, "a voice the app hasn't named yet")
UNNAMED = Asker(False, "a voice the app couldn't name")
TV = Asker(False, "the TV or radio, not a person")
DOUBTED = Asker(False, "a turn whose speaker is in doubt")
CROSSTALK = Asker(False, "two voices talking at once")
SEVERAL = Asker(False, "several voices at once")


def _labels(msg) -> dict:
    raw = msg.get("voice_labels")
    if not raw or not isinstance(raw, str):
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def classify(msg, *, owner_name: str, room_mode: bool,
             open_flag_ids=frozenset()) -> Asker:
    """Did the owner ask? `msg` is the round's asking turn, or None.

    - typed: the owner, since only a signed-in owner types into a chat once
      a password is set (the caller checks that separately);
    - spoken and labelled with the owner's name alone: the owner;
    - spoken with no label: the owner in a solo chat, where only the owner
      speaks, and a voice not named yet in room mode;
    - anything memory files as a guest (a named guest, an uncertain or
      unresolved voice, crosstalk, a turn with an open doubt): not the
      owner, with the reason."""
    if not msg or msg.get("speaker") != "user":
        return NOBODY
    wire = ingest_speaker(msg, open_flag_ids, owner_name)
    data = _labels(msg)
    if wire == "user":
        if not msg.get("voice_turn_id") or data.get("labels"):
            return OWNER
        return PENDING if room_mode else OWNER
    if wire != GUEST_UNKNOWN:
        name = wire[len("guest:"):]
        return Asker(False, f"{name}, a guest")
    if msg.get("id") in open_flag_ids:
        return DOUBTED
    if data.get("crosstalk") is True:
        return CROSSTALK
    if data.get("unresolved") == MEDIA:
        return TV
    labels = data.get("labels") if isinstance(data.get("labels"), list) else []
    if len({l for l in labels if isinstance(l, str)}) > 1:
        return SEVERAL
    return UNNAMED


def asking_turn_id(messages, is_handback=False) -> int | None:
    """The message a round answers: the newest one, when a person sent it.
    The app's own system lines are skipped, since a spoken "research more"
    can post one just after the turn it came in. A hand-back round relays a
    result and a continue round follows a seat's reply, so neither has one.
    A slash command starts no round, so it isn't one either."""
    if is_handback:
        return None
    for m in reversed(messages or ()):
        if m.get("speaker") == "system":
            continue
        if m.get("speaker") != "user" or \
                (m.get("content") or "").lstrip().startswith("/"):
            return None
        return m.get("id")
    return None


@dataclass(frozen=True)
class _Ask:
    asker: Asker
    enrolled: bool


def _read_ask(chat_id, message_id, owner_name) -> _Ask:
    """The asking turn, the chat's room mode and open doubts, and whether a
    password is set, read fresh: a voice label can land after the round
    loaded the transcript."""
    con = db.connect()
    try:
        enrolled = auth.is_enrolled(con)
        if not message_id:
            return _Ask(NOBODY, enrolled)
        row = con.execute(
            "SELECT id, chat_id, speaker, content, voice_turn_id, voice_labels "
            "FROM messages WHERE id=?", (message_id,)).fetchone()
        chat = con.execute("SELECT room_mode FROM chats WHERE id=?",
                           (chat_id,)).fetchone()
        flags = db.get_room_flags(con, chat_id, open_only=True)
    finally:
        con.close()
    if row is None or chat is None or row["chat_id"] != chat_id:
        return _Ask(NOBODY, enrolled)
    flagged = frozenset(f["message_id"] for f in flags if f.get("message_id"))
    return _Ask(classify(dict(row), owner_name=owner_name,
                         room_mode=bool(chat["room_mode"]),
                         open_flag_ids=flagged), enrolled)


# ---------- counting ----------

def _day(unix) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(unix))


def paid_from_chat_today(now: float | None = None) -> int:
    """Real runs started from any chat since local midnight, however they
    ended. Deleting a report on the page drops it from the count."""
    today = _day(time.time() if now is None else now)
    return sum(1 for r in analysis.list_runs(limit=None)
               if r.get("started_from") == "chat" and not r.get("practice")
               and r.get("created_at_unix") is not None
               and _day(r["created_at_unix"]) == today)


def _already_started(chat_id, message_id, measurement_id, practice) -> bool:
    return any(r.get("started_from") == "chat"
               and r.get("chat_id") == chat_id
               and r.get("asked_in") == message_id
               and r.get("measurement") == measurement_id
               and bool(r.get("practice")) == practice
               for r in analysis.list_runs(limit=None))


# ---------- the result in the chat ----------

def report_link(run_id: str) -> str:
    return REPORT_LINK_PREFIX + run_id


def _spent(usd) -> str:
    if usd is None:
        return ""
    if usd < 0.01:
        return " It spent under a cent."
    return f" It spent ${usd:.2f}."


_ENDINGS = {
    analysis.FAILED: "didn't finish. The Analysis page has what went wrong.",
    analysis.STOPPED: "was stopped before it finished.",
    analysis.TIMED_OUT: "ran out of time and was stopped.",
    analysis.INTERRUPTED: "was cut off when the app stopped.",
}


def result_text(record: dict) -> str:
    """The system line a settled run posts in the chat that asked. Built
    from fixed words, the run's headline and what it spent. Never the
    report, and never a failed run's stderr, which could hold anything."""
    title = (record.get("title") or record.get("measurement") or "run").lower()
    what = (f"The practice run of the {title}" if record.get("practice")
            else f"The {title}")
    link = f"[Open the report on the Analysis page]({report_link(record['run_id'])})"
    if record.get("state") != analysis.DONE:
        ending = _ENDINGS.get(record.get("state"), "didn't finish.")
        return f"{what} asked for in this chat {ending} {link}"
    summary = record.get("summary") or {}
    headline = summary.get("headline") or "The report is saved."
    if record.get("practice"):
        headline = headline.removeprefix("Practice run, made-up numbers. ")
        return (f"{what} asked for in this chat has finished. The numbers "
                f"are made up, so it only shows the harness works. "
                f"{headline} {link}")
    return (f"{what} asked for in this chat has finished. {headline}"
            f"{_spent(summary.get('spent_usd'))} {link}")


_relays: set = set()


def _poster(chat_id, handback):
    """The on_settle callback: post the result line, then hand the chat to
    one seat to relay it after a natural pause, like a guest's result."""
    def post(record):
        try:
            con = db.connect()
            try:
                db.insert_message(con, chat_id, "system", result_text(record))
            finally:
                con.close()
        except Exception:
            log.warning("run_eval result post failed for chat %s", chat_id,
                        exc_info=True)
            return
        # A stopped run needs no relay: the owner stopped it on the page,
        # or the app is going down and a new round would race the stop.
        if handback is not None and record.get("state") != analysis.STOPPED:
            task = asyncio.get_running_loop().create_task(
                _relay(chat_id, handback))
            _relays.add(task)
            task.add_done_callback(_relays.discard)
    return post


async def _relay(chat_id, handback):
    from . import guestjobs
    try:
        if await guestjobs.wait_for_pause(chat_id, guestjobs.RESULT_SETTLE_S):
            await handback(chat_id, "result")
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("run_eval hand-back failed for chat %s", chat_id)


# ---------- the tool ----------

def _voice_live() -> bool:
    from .routers import voice as voice_router
    return bool(voice_router.capture_sessions())


def _offer(m, why) -> str:
    return (f"Not started. A real run of the {m.title.lower()} spends money, "
            f"so from a chat it starts only when the owner asks. This "
            f"request came from {why}. Say so plainly, and offer a free "
            "practice run instead: it uses made-up numbers and only shows "
            "the harness works. If they want it, call run_eval again with "
            "mode \"practice\". The owner can start a real run by asking "
            "themselves, or from the Analysis page.")


def _started(m, practice, record, paid_today) -> str:
    if practice:
        return (f"Started a practice run of the {m.title.lower()} "
                f"({record['run_id']}). Tell the room it's free and reads "
                "nothing of theirs: the harness runs with stand-ins and "
                "made-up numbers, so it only shows the harness works. When "
                "it finishes, its result and a link to the report are "
                "posted in this chat, and one seat passes it on.")
    return (f"Started a real run of the {m.title.lower()} "
            f"({record['run_id']}). Tell the room now, in your own words, "
            f"what it costs and what it touches. It costs: {m.costs} It "
            f"touches: {m.touches} It takes: {m.takes} When it finishes, "
            "its result and a link to the report are posted in this chat, "
            "and one seat passes it on. Real runs started from chat today: "
            f"{paid_today} of {CHAT_PAID_DAILY_CAP}.")


async def run(args: dict, cfg: dict) -> str:
    """Dispatch one run_eval call. Returns the words the seat reads back:
    what started, or why nothing did and what to offer."""
    extra = sorted(set(args) - FIELDS)
    if extra:
        return ("Not started. run_eval takes a measurement and a mode, "
                f"nothing else ({', '.join(extra)}).")
    m = analysis.get(str(args.get("measurement") or ""))
    if m is None:
        return ("Not started. There's no measurement by that name. Pick one "
                "of: " + ", ".join(x.id for x in analysis.MEASUREMENTS) + ".")
    mode = args.get("mode")
    if mode not in MODES:
        return "Not started. mode is \"practice\" or \"real\"."
    practice = mode == "practice"
    chat_id = cfg.get("chat_id")
    if not chat_id:
        return "Not started. run_eval only works inside a chat."
    message_id = cfg.get("_round_asker_id")
    ask = await asyncio.to_thread(_read_ask, chat_id, message_id,
                                  cfg.get("user_name") or "")
    if not ask.asker.anyone:
        return ("Not started. Nobody asked for a run in the turn you're "
                "answering: this round started on its own. Start a run only "
                "when someone asks for one.")
    if not practice:
        if not ask.asker.owner:
            return _offer(m, ask.asker.why)
        if not ask.enrolled:
            return ("Not started. A real run from a chat needs an owner "
                    "password set first, the same rule as the Analysis "
                    "page, since until then anyone on this Mac can type "
                    "into a chat. Say so, and offer a free practice run.")
    # From here to the start nothing awaits, so two calls in one batch
    # can't both pass the count.
    if _already_started(chat_id, message_id, m.id, practice):
        return (f"Not started again. This request already started the "
                f"{m.title.lower()}, and its result will be posted in this "
                "chat when it finishes.")
    paid_today = paid_from_chat_today()
    if not practice and paid_today >= CHAT_PAID_DAILY_CAP:
        return (f"Not started. Real runs started from a chat are capped at "
                f"{CHAT_PAID_DAILY_CAP} a day, and today's have all "
                "started. Say so, and offer a free practice run instead. "
                "The owner can still run it from the Analysis page.")
    try:
        record = analysis.start(
            m.id, practice=practice, voice_live=_voice_live(),
            memory_url=cfg.get("memory_url") or "",
            chat={"chat_id": chat_id, "message_id": message_id},
            on_settle=_poster(chat_id, cfg.get("_handback")))
    except analysis.Busy as e:
        return f"Not started. {e}"
    except analysis.Refused:
        return (f"Not started. The {m.title.lower()}'s command would put "
                "words from the owner's chats in its report, and a run like "
                "that never starts from a chat.")
    return _started(m, practice, record,
                    paid_today + (0 if practice else 1))
