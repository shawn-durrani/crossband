"""Long work an MCP server runs in the background, watched the way a Claude
Code guest is (#604).

A tool on an outside MCP server can start work that runs for minutes, such
as a design app building a piece of furniture. A seat that waited on it would
hold the round, and in a voice chat the room would sit in silence and a
barge-in would cut the seat off. So the server answers at once, and its
result's structured content carries a `background` block:

    {"background": {"job": "<id>", "state": "running" | "waiting" | "done"
                    | "idle", "title": "Dovetail",
                    "progress_tool": "dovetail_progress", "stage": "...",
                    "steps": 48, "elapsed_s": 312,
                    "waiting_for": "question" | "preview" | "plan" | "part"
                    | null, "ask": "...", "reply": "..."}}

When a seat's call comes back running or waiting, the dispatcher
(tools.run_tool) starts a `Watcher` for that chat and server, or refreshes
the one already there. The watcher is a DETACHED task, like a GuestJob
(backend/guestjobs.py): no round owns it, so a barge-in, a new message or
a stopped round never reaches it, and only the app stopping ends it early.

- It polls the server's own progress tool, called with `{}`, every POLL_S.
  A failed poll is tried again on the next tick and never read as the work
  having stopped. Only GIVE_UP_S of nothing but failures, or MAX_WATCH_S in
  all, ends the watch, and then the status says the app lost touch.
- Each good poll is pushed to clients on the events stream as an `mcp_job`
  event, and every seat in the chat gets a background-work note in the
  volatile tail of its prompt (status_note), so "how's it going?" is
  answered without a tool call and nobody waits on the work.
- Hand-backs go through the guest narrator path (engine.make_handback,
  guestjobs.wait_for_pause), with a note telling the one seat why it's on:
  - waiting is a BLOCKER: the question is posted in the chat under the
    server's namespaced speaker and handed back the moment the chat is
    between rounds;
  - done is a RESULT: the reply, trimmed, is posted the same way and
    relayed at a natural pause, and then the watch ends;
  - running gets a spoken progress note at a pause, only when the stage
    changed since anyone last spoke about the work, never in its first
    FIRST_PROGRESS_S, and at most every `mcp_progress_every_s` seconds.
- A new job id (a follow-up request) keeps the same watcher going.

The server's words (its stage, question and reply) are untrusted, like any
tool result. They reach the chat under `ext:<server>`, the namespace every
outside producer uses, and they reach the seat note only quoted and marked
as the server's own words. Crossband still gives none of them a meaning.

One watcher per chat per server, kept in memory. A restart forgets the
watch, and the next call to the server's tools picks it up again.
"""

import asyncio
import itertools
import logging
import re
import time

from . import context_marker, db, events

log = logging.getLogger("crossband.mcpjobs")

# How often a watcher asks the progress tool, and how long one ask may take.
POLL_S = 15.0
POLL_TIMEOUT_S = 10.0
# No spoken progress before the work has run this long.
FIRST_PROGRESS_S = 60.0
# Spoken progress at most this often, unless config says otherwise
# (`mcp_progress_every_s`; 0 turns spoken progress off).
PROGRESS_EVERY_S = 120.0
# How long the chat must be between rounds before a hand-back. A question
# goes the moment it's idle, a result after the guest result's pause, and a
# progress note, the least urgent, waits longest.
BLOCKER_SETTLE_S = 0.0
RESULT_SETTLE_S = 2.0
PROGRESS_SETTLE_S = 6.0
# Nothing but failed polls for this long, or a watch this long in all, ends
# the watch. The status says the app lost touch, never that the work ended.
GIVE_UP_S = 1800.0
MAX_WATCH_S = 6 * 3600.0
# An ended watch's last status stays this long for a client that opens the
# chat late.
KEEP_ENDED_S = 600.0

STATES = ("running", "waiting", "done", "idle")
WAITING_FOR = ("question", "preview", "plan", "part")
TITLE_CHARS = 40
STAGE_CHARS = 160
ASK_CHARS = 600
REPLY_CHARS = 4000
# A finished reply is posted trimmed to this many characters.
RESULT_POST_CHARS = 1500

# Higher wins when two hand-backs are due at once.
_RANK = {"progress": 0, "blocker": 1, "result": 2}
_SETTLE = {"progress": PROGRESS_SETTLE_S, "blocker": BLOCKER_SETTLE_S,
           "result": RESULT_SETTLE_S}

_ids = itertools.count(1)
# (chat_id, server) -> the live Watcher
_watchers: dict[tuple[int, str], "Watcher"] = {}
# watcher id -> its latest status, live or recently ended, for the events
# stream and the chat's snapshot route
_latest: dict[int, dict] = {}
_last_stamp = 0.0


def _clock() -> float:
    return time.monotonic()


def _stamp() -> float:
    """A wall-clock updated_at that never repeats, for the stream cursor."""
    global _last_stamp
    _last_stamp = max(db.now(), _last_stamp + 1e-6)
    return _last_stamp


# ---------- the block ----------

def _line(value, cap) -> str:
    """One plain line of server text: whitespace collapsed, capped."""
    if not isinstance(value, str):
        return ""
    text = " ".join(value.split())
    return text if len(text) <= cap else text[:cap - 1].rstrip() + "…"


def _number(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if value >= 0 else None


def background_block(structured) -> dict | None:
    """The `background` block of a result's structured content, checked and
    tidied, or None when there isn't a usable one. Every field is the
    server's, so each is typed and capped here, once."""
    if not isinstance(structured, dict):
        return None
    raw = structured.get("background")
    if not isinstance(raw, dict):
        return None
    state = raw.get("state")
    job = raw.get("job")
    if state not in STATES or not isinstance(job, (str, int)) \
            or isinstance(job, bool):
        return None
    tool = raw.get("progress_tool")
    steps = _number(raw.get("steps"))
    waiting_for = raw.get("waiting_for")
    reply = raw.get("reply") if isinstance(raw.get("reply"), str) else ""
    return {
        "job": str(job)[:200],
        "state": state,
        "title": _line(raw.get("title"), TITLE_CHARS),
        "progress_tool": tool if isinstance(tool, str) and tool.strip()
        else "",
        "stage": _line(raw.get("stage"), STAGE_CHARS),
        "steps": int(steps) if steps is not None else None,
        "elapsed_s": _number(raw.get("elapsed_s")),
        "waiting_for": waiting_for if waiting_for in WAITING_FOR else None,
        "ask": _line(raw.get("ask"), ASK_CHARS),
        "reply": reply.strip()[:REPLY_CHARS],
    }


def _source(server: str) -> str:
    """The server's name as an ext: speaker, in the ingest route's alphabet."""
    s = re.sub(r"[^a-z0-9_-]+", "-", (server or "").lower()).strip("-")
    return (s or "mcp")[:32]


def _trim(text: str, cap: int) -> str:
    """Cut at a sentence or a word near the cap, never mid-word."""
    text = (text or "").strip()
    if len(text) <= cap:
        return text
    cut = text[:cap]
    stop = max(cut.rfind(". "), cut.rfind("\n"))
    if stop >= cap // 2:
        return cut[:stop + 1].rstrip()
    space = cut.rfind(" ")
    return (cut[:space] if space > 0 else cut).rstrip() + "…"


def duration(seconds) -> str:
    """How long work has run, as a person would say it."""
    s = int(seconds or 0)
    if s < 60:
        return "under a minute"
    m = s // 60
    if m < 90:
        return f"{m} min"
    return f"{m // 60} h {m % 60} min"


# ---------- the watcher ----------

class Watcher:
    """One chat's watch on one server's background work. Plain state plus
    two tasks: the poll loop and, while one is due, a hand-back relay."""

    def __init__(self, chat_id, server, mgr, *, progress_tool, now,
                 handback=None, ask_tool=None,
                 progress_every=PROGRESS_EVERY_S):
        self.id = next(_ids)
        self.chat_id = chat_id
        self.server = server
        self.mgr = mgr
        self.progress_tool = progress_tool
        self.handback = handback
        self.ask_tool = ask_tool
        self.progress_every = progress_every
        self.title = server
        self.job = None
        self.state = "running"
        self.stage = ""
        self.steps = None
        self.elapsed_s = None
        self.waiting_for = None
        self.ask = ""
        self.reply = ""
        self.started_at = now
        self.job_seen_at = now
        self.seen_at = now
        self.last_ok_at = now
        # The seat whose call started the watch just spoke about the work.
        self.last_spoken_at = now
        self.last_spoken_stage = ""
        self.asked: set = set()
        self.done_jobs: set = set()
        self.finished = False
        self.ended = ""  # why the watch ended: done, idle, lost or stopped
        self.task: asyncio.Task | None = None
        self._relay: asyncio.Task | None = None
        self._relay_kind = ""

    # ----- folding a block in -----

    def work_elapsed(self, now) -> float:
        """How long the current job has run, as of `now`: the server's own
        count moved on by the time since it said so, else our own clock."""
        if self.elapsed_s is not None:
            if self.state == "running":
                return self.elapsed_s + max(0.0, now - self.seen_at)
            return self.elapsed_s
        return max(0.0, now - self.job_seen_at)

    def apply(self, block, now, *, from_seat=False, tool=None):
        """Fold one block in and say which hand-back it makes due:
        "blocker", "result", "progress" or None. A block from a seat's own
        call is never handed back, because that seat has it in front of it
        and is talking about it now."""
        new_job = block["job"] != self.job
        if new_job:
            self.job = block["job"]
            self.job_seen_at = now
            self.reply = ""
        self.state = block["state"]
        if self.state in ("running", "waiting"):
            # A follow-up request, or work going again, keeps this watch.
            self.finished = False
            self.ended = ""
        redact = lambda v: context_marker.redact(v, self.chat_id)  # noqa: E731
        if block["title"]:
            self.title = redact(block["title"])
        # Only a tool the server lists is ever polled.
        if block["progress_tool"] and self.mgr_qualified(block["progress_tool"]):
            self.progress_tool = block["progress_tool"]
        self.stage = redact(block["stage"])
        self.steps = block["steps"]
        self.elapsed_s = block["elapsed_s"]
        self.waiting_for = block["waiting_for"]
        self.ask = redact(block["ask"])
        if block["reply"]:
            self.reply = redact(block["reply"])
        self.seen_at = now
        if from_seat:
            # A new job id means a new request, so the tool that started it
            # is the one a change goes through. The progress tool starts
            # nothing: a build begun in the server's own window is watched
            # with no ask tool known.
            if new_job and tool and \
                    tool != self.mgr_qualified(self.progress_tool):
                self.ask_tool = tool
            self.last_spoken_at = now
            self.last_spoken_stage = self.stage
            if self.state == "waiting":
                self.asked.add((self.job, self.ask))
            if self.state in ("done", "idle"):
                self.done_jobs.add(self.job)
                self.finished = True
                self.ended = self.state
            return None
        if self.state == "idle":
            self.finished = True
            self.ended = "idle"
            return None
        if self.state == "done":
            self.finished = True
            self.ended = "done"
            if self.job in self.done_jobs:
                return None
            self.done_jobs.add(self.job)
            return "result"
        if self.state == "waiting":
            key = (self.job, self.ask)
            if key in self.asked:
                return None
            self.asked.add(key)
            return "blocker"
        return "progress" if self.progress_due(now) else None

    def progress_due(self, now) -> bool:
        """A spoken progress note is due: the stage moved on since anyone
        last spoke about the work, the work is past its first minute, and
        the last word about it is at least progress_every old."""
        if self.progress_every <= 0 or self.state != "running":
            return False
        if not self.stage or self.stage == self.last_spoken_stage:
            return False
        if self.work_elapsed(now) < FIRST_PROGRESS_S:
            return False
        return now - self.last_spoken_at >= self.progress_every

    def mgr_qualified(self, tool) -> str | None:
        if not tool or self.mgr is None:
            return None
        return self.mgr.qualified(self.server, tool)

    # ----- what clients and seats see -----

    def snapshot(self, now) -> dict:
        return {"type": "mcp_job", "chat_id": self.chat_id, "id": self.id,
                "server": self.server, "title": self.title,
                "state": self.state, "stage": self.stage,
                "steps": self.steps,
                "elapsed_s": round(self.work_elapsed(now)),
                "waiting_for": self.waiting_for,
                "watching": not self.ended, "ended": self.ended,
                "updated_at": _stamp()}

    def touch(self):
        """Record the latest status and wake every client."""
        _latest[self.id] = self.snapshot(_clock())
        _prune()
        events.notify_mcp_job()

    def note(self, now) -> str:
        """This watch's lines for the seats' background-work note."""
        title = self.title
        progress_q = self.mgr_qualified(self.progress_tool)
        if self.finished:
            if self.relay_pending() and self._relay_kind == "result":
                return (f"{title} has finished its background work, and "
                        f"what it said is posted in the chat under {title}'s "
                        "name. One of you passes it on at the next pause, "
                        "so don't call its tools to check on it.")
            return ""
        lines = [f"{title} is working in the background on a request from "
                 "this chat. It carries on whatever happens here, through "
                 "new messages, someone talking over you and a stop, so "
                 "never wait on it."]
        if self.state == "waiting":
            what = (f": \"{self.ask}\" ({title}'s own words)" if self.ask
                    else "")
            lines.append(f"Right now it's waiting on the room for an "
                         f"answer{what}.")
        else:
            bits = []
            if self.stage:
                bits.append(f"\"{self.stage}\" ({title}'s own words)")
            if self.steps:
                bits.append(f"step {self.steps}")
            bits.append(f"{duration(self.work_elapsed(now))} in")
            lines.append("Right now: " + ", ".join(bits) + ".")
        if progress_q:
            lines.append("If someone asks how it's going, answer from this "
                         f"note, or call {progress_q} once, which answers at "
                         "once.")
        else:
            lines.append("If someone asks how it's going, answer from this "
                         "note.")
        ask_tool = self.ask_tool if self.ask_tool and \
            self.ask_tool != progress_q else None
        if ask_tool:
            lines.append("To change or redirect the work, pass the change "
                         f"on with {ask_tool}.")
        else:
            lines.append("To change or redirect the work, pass the change "
                         f"on with the {title} tool you'd ask it something "
                         "with.")
        lines.append("The app brings its questions, a progress note now and "
                     "then, and its result back to the chat on its own, so "
                     "never call a tool again and again to watch it.")
        return " ".join(lines)

    # ----- hand-backs -----

    def queue(self, kind):
        """Make a hand-back due. A higher-ranked one already waiting wins,
        and a progress note already waiting stands, since it reads the
        newest stage when it fires."""
        pending = self._relay is not None and not self._relay.done()
        if pending:
            if _RANK[self._relay_kind] > _RANK[kind]:
                return
            if kind == self._relay_kind == "progress":
                return
            self._relay.cancel()
        if kind in ("blocker", "result"):
            self._post(kind)
        self._relay_kind = kind
        self._relay = asyncio.get_running_loop().create_task(
            self._relay_run(kind))

    def _post(self, kind):
        """Post the server's question or reply in the chat, under its
        namespaced speaker, so it's in view at once and on the record."""
        title = self.title
        if kind == "result":
            head = f"**{title} finished**"
            body = _trim(self.reply, RESULT_POST_CHARS)
        else:
            head = {
                "question": f"**{title} is asking**",
                "preview": f"**{title} has a preview waiting**",
                "plan": f"**{title} has a plan waiting for a yes**",
                "part": f"**{title} is waiting on a part**",
            }.get(self.waiting_for, f"**{title} is waiting on you**")
            body = self.ask
        text = f"{head}\n{body}" if body else head
        try:
            con = db.connect()
            try:
                db.insert_message(con, self.chat_id,
                                  f"ext:{_source(self.server)}", text)
            finally:
                con.close()
        except Exception:
            log.warning("mcp job %s: posting its %s failed", self.id, kind,
                        exc_info=True)

    def handback_note(self, kind) -> str:
        title = self.title
        if kind == "blocker":
            ask_tool = self.ask_tool or f"the {title} tool you'd ask it with"
            return (f"{title}'s background work is waiting on the room, and "
                    f"its question is posted in the chat under {title}'s "
                    "name. Put it to the room now, briefly, in your own "
                    "words, and don't answer it yourself. When someone "
                    f"answers, pass the answer on with {ask_tool}.")
        if kind == "result":
            return (f"{title}'s background work has finished, and what it "
                    f"said is posted in the chat under {title}'s name. Tell "
                    "the room in a sentence or two what it did. Don't start "
                    "new work, and don't call its tools to check.")
        return (f"{title} is still working in the background, and nobody "
                "needs to do anything. Give the room one short progress "
                "line from the background-work note, then stop. Don't call "
                "any tool.")

    async def _relay_run(self, kind):
        from . import guestjobs  # the narrator's pause, shared
        try:
            if not await guestjobs.wait_for_pause(self.chat_id, _SETTLE[kind]):
                return
            if kind == "progress" and not self.progress_still_wanted():
                return
            if self.handback is None:
                return
            self.last_spoken_at = _clock()
            self.last_spoken_stage = self.stage
            await self.handback(self.chat_id, kind,
                                note=self.handback_note(kind))
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("mcp job %s: %s hand-back failed", self.id, kind)

    def progress_still_wanted(self) -> bool:
        """At fire time: still running, and nobody has spoken this stage
        while the note waited for its pause."""
        return (self.state == "running" and not self.finished
                and bool(self.stage)
                and self.stage != self.last_spoken_stage)

    def relay_pending(self) -> bool:
        return self._relay is not None and not self._relay.done()

    # ----- the poll loop -----

    async def run(self):
        try:
            while True:
                if self.finished and not self.relay_pending():
                    break
                await asyncio.sleep(POLL_S)
                if self.finished:
                    continue  # a result is still on its way to the room
                now = _clock()
                if now - self.started_at >= MAX_WATCH_S:
                    self.ended = "lost"
                    break
                try:
                    block = background_block(await self.mgr.poll(
                        self.server, self.progress_tool,
                        timeout=POLL_TIMEOUT_S))
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.warning("mcp job %s: poll failed", self.id,
                                exc_info=True)
                    block = None
                if block is None:
                    # Retried on the next tick. Failure says nothing about
                    # the work, so nothing is handed back for it.
                    if _clock() - self.last_ok_at >= GIVE_UP_S:
                        self.ended = "lost"
                        break
                    continue
                now = _clock()
                self.last_ok_at = now
                kind = self.apply(block, now)
                self.touch()
                if kind:
                    self.queue(kind)
        except asyncio.CancelledError:
            self.ended = self.ended or "stopped"
            if self.relay_pending():
                self._relay.cancel()
            raise
        finally:
            if _watchers.get((self.chat_id, self.server)) is self:
                _watchers.pop((self.chat_id, self.server), None)
            if not self.ended:
                self.ended = "lost"
            self.touch()


# ---------- the surface the rest of the app uses ----------

def on_result(cfg: dict, mgr, qualified: str, structured) -> str:
    """Called by the dispatcher for every MCP tool result in a chat. Starts
    or refreshes the chat's watcher when the result says work is running or
    waiting, and returns a line to add to what the seat reads ('' when
    there's nothing to say). Never raises: watching is a courtesy, and a
    fault here must not cost the seat its tool result."""
    try:
        return _on_result(cfg, mgr, qualified, structured)
    except Exception:
        log.exception("mcp background check failed for %s", qualified)
        return ""


def _on_result(cfg, mgr, qualified, structured) -> str:
    block = background_block(structured)
    chat_id = cfg.get("chat_id")
    if block is None or not chat_id or mgr is None:
        return ""
    where = mgr.server_of(qualified)
    if where is None:
        return ""
    server = where[0]
    now = _clock()
    watcher = _watchers.get((chat_id, server))
    if watcher is not None:
        watcher.handback = cfg.get("_handback") or watcher.handback
        watcher.progress_every = float(
            cfg.get("mcp_progress_every_s", watcher.progress_every))
        watcher.apply(block, now, from_seat=True, tool=qualified)
        watcher.touch()
        return _seat_line(watcher) if block["state"] in ("running",
                                                         "waiting") else ""
    if block["state"] not in ("running", "waiting"):
        return ""
    progress_tool = block["progress_tool"]
    if not progress_tool or mgr.qualified(server, progress_tool) is None:
        log.info("mcp server %s reported background work with no progress "
                 "tool it lists; not watching", server)
        return ""
    watcher = Watcher(chat_id, server, mgr, progress_tool=progress_tool,
                      now=now, handback=cfg.get("_handback"),
                      progress_every=float(cfg.get("mcp_progress_every_s",
                                                   PROGRESS_EVERY_S)))
    watcher.apply(block, now, from_seat=True, tool=qualified)
    _watchers[(chat_id, server)] = watcher
    watcher.task = asyncio.get_running_loop().create_task(watcher.run())
    watcher.touch()
    log.info("mcp job %s: watching %s in chat %s", watcher.id, server,
             chat_id)
    return _seat_line(watcher)


def _seat_line(watcher) -> str:
    return (f"\n\n(From the app: this work carries on in the background "
            f"and the app is watching it. Don't wait on it, and don't call "
            f"{watcher.title}'s tools again to watch it. Its questions, a "
            "progress note now and then, and its result come back to the "
            "chat on their own.)")


def active_for_chat(chat_id) -> list:
    return [w for (cid, _), w in _watchers.items() if cid == chat_id]


def status_note(chat_id) -> str:
    """The background-work note every seat in the chat reads, one paragraph
    per watched server, or '' when nothing is watched."""
    now = _clock()
    notes = (w.note(now) for w in active_for_chat(chat_id))
    return "\n\n".join(n for n in notes if n)


def snapshots(chat_id) -> list[dict]:
    """This chat's watches, live and recently ended, oldest first."""
    _prune()
    return sorted((s for s in _latest.values() if s["chat_id"] == chat_id),
                  key=lambda s: s["id"])


def updates_after(ts: float) -> list[dict]:
    """Every status written after `ts`, for the events stream's cursor."""
    return sorted((s for s in _latest.values() if s["updated_at"] > ts),
                  key=lambda s: s["updated_at"])


def _prune():
    cutoff = db.now() - KEEP_ENDED_S
    for wid in [wid for wid, s in _latest.items()
                if not s["watching"] and s["updated_at"] < cutoff]:
        _latest.pop(wid, None)


async def stop_all():
    """Stop every watch cleanly, for the app's shutdown."""
    watchers = list(_watchers.values())
    for w in watchers:
        if w.task is not None and not w.task.done():
            w.task.cancel()
    for w in watchers:
        if w.task is not None:
            try:
                await w.task
            except asyncio.CancelledError:
                pass
            except Exception:
                log.exception("mcp watcher failed while stopping")
        if w.relay_pending():
            w._relay.cancel()
        # A task cancelled before its first step never ran its own cleanup.
        if not w.ended:
            w.ended = "stopped"
            w.touch()
        if _watchers.get((w.chat_id, w.server)) is w:
            _watchers.pop((w.chat_id, w.server), None)


def reset():
    """Forget every watch. For tests, which build many apps in one
    process."""
    _watchers.clear()
    _latest.clear()
