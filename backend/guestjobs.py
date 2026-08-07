"""Async guest execution as a background job, decoupled from the turn lifecycle.

A Claude Code guest visit used to run as the FINAL turn of the round that
summoned it - so a barge-in, a new user message, or a network drop that
cancelled the round could take the guest's investigation/PR down with it, and
"is it still working?" was inferred from whether its last message looked cut off.

Here the guest runs as a DETACHED asyncio task (`GuestJob`), independent of any
round. Once summoned it keeps going while the user and the narrating models keep
talking; its status lives in the durable `guest_jobs` table (db.py) and is pushed
to every client - voice and text/mobile alike - over the ONE events stream
(events.py). On completion the job hands the conversation back through a narrating
model, on one of two paths:

  - a BLOCKER (the guest is asking the group something) interrupts promptly -
    handed back as soon as the chat is between rounds, no extra wait;
  - a routine RESULT waits for a natural pause, then a narrator relays a short
    summary - it never yanks an in-flight conversation.

Second-run semantics: BLOCK. One guest job per chat at a time; a second summon
while one is in flight is refused with a clear message (guest.request). That is
the simplest safe option: no silent queue to forget about, and no two guests
contending for the same worktree. Recorded in DECISIONS.md.

The state store is deliberately generic (status + kind + a message pointer) so
tab/backgrounding persistence for voice playback/connection can reuse it rather
than growing a second, conflicting mechanism.
"""

import asyncio
import json
import logging

from . import db, events, guest, work_status

log = logging.getLogger("crossband.guestjobs")

# How long a routine RESULT hand-back waits for the conversation to settle
# (no active round) before a narrator relays it. A BLOCKER uses 0 - it still
# can't run two rounds at once, but it jumps in the instant the chat is idle.
RESULT_SETTLE_S = 2.0
BLOCKER_SETTLE_S = 0.0
# Bound on how long a completed job waits for a pause before giving up on the
# auto hand-back (the guest reply is already persisted and visible regardless).
HANDBACK_MAX_WAIT_S = 3600.0
_PAUSE_POLL_S = 0.25


class GuestJob:
    """One live, detached guest run. The DB row is the durable record; this
    object holds the asyncio.Task and the in-flight accumulators."""

    def __init__(self, row: dict):
        self.id = row["id"]
        self.chat_id = row["chat_id"]
        self.task_desc = row["task"]
        self.repo = row["repo"]
        self.mode = row["mode"]
        self.status = row["status"]
        self.kind = row["kind"]
        self.content = ""
        self.tools: list[dict] = []
        self.usage: dict | None = None
        self.session_id = ""
        self.step_count = 0
        self.task: asyncio.Task | None = None


# job_id -> GuestJob (live registry; the DB is the source of truth for status)
_jobs: dict[int, GuestJob] = {}


def active_for_chat(chat_id: int) -> GuestJob | None:
    """The chat's in-flight guest job, if any. The block that enforces
    one-job-per-chat reads this - checked at summon time so the model is told
    immediately, not after a silent no-op."""
    for job in _jobs.values():
        if job.chat_id == chat_id and job.status == "running":
            return job
    return None


def get(job_id: int) -> GuestJob | None:
    return _jobs.get(job_id)


def _classify(content: str) -> str:
    """Which completion path this reply takes: 'blocker' (the guest needs
    input, surface promptly) or 'result' (routine, wait for a pause). Heuristic
    and deliberately biased toward 'result' - the non-interrupting path is the
    safe default; a wrong 'blocker' would yank the conversation, a wrong 'result'
    merely waits for a lull. The marker lists below are tunable; the bias and
    the reason for it are documented in DECISIONS.md.

    A reply that shipped something (a PR link / "pull request") is a result even
    if it ends with a question - those are rhetorical wrap-ups, not a request to
    the group to act before the guest can continue."""
    text = (content or "").strip()
    if not text:
        return "result"
    low = text.lower()
    shipped = any(s in low for s in (
        "github.com", "/pull/", "pull request", "opened a pr", "pr link"))
    if shipped:
        return "result"
    tail = text[-240:]
    blocker_markers = ("which ", "should i", "should we", "do you want",
                       "would you like", "let me know", "need you to",
                       "before i proceed", "before i continue", "confirm",
                       "clarify", "?")
    if any(m in tail.lower() for m in blocker_markers):
        return "blocker"
    return "result"


def _persist_reply(job: GuestJob) -> int | None:
    """Write the guest's reply as a message, exactly like a roster turn - same
    speaker, tool_events and self-reported cost - so it renders and accounts
    identically. Returns the new message id (or None when the guest produced
    nothing)."""
    if not job.content:
        return None
    usage_json = None
    if job.usage:
        u = dict(job.usage)
        # Guests report their own real cost; stamp the speaker like persist_live
        # does so accounting groups guest spend under the guest slug.
        u["model"] = guest.SLUG
        usage_json = json.dumps(u)
    con = db.connect()
    msg = db.insert_message(con, job.chat_id, guest.SLUG, job.content,
                            usage_json=usage_json, tool_events=job.tools)
    con.close()
    return msg["id"]


def _broadcast(job_id: int, **fields):
    """Patch the job row and wake every connected client over the events bus."""
    con = db.connect()
    row = db.update_guest_job(con, job_id, **fields)
    con.close()
    events.notify_guest_job()
    return row


def _ping(job: GuestJob):
    """One ongoing-work check-in for a still-running job - fully ephemeral:
    patches this job's row (status_label/status_at - db.update_guest_job) and
    broadcasts over the SAME events channel every other guest-job status
    change uses (_broadcast/events.notify_guest_job).
    Never a chat message: a reconnecting client re-reads current state from
    GET /api/chats/{id}/guest_jobs (or the live guest_job SSE event) instead
    of replaying a message that never actually happened in the conversation.
    No round, no voice: a spoken check-in for this path is a separate product
    decision, deliberately deferred (see DECISIONS.md)."""
    if job.status != "running":
        return  # settled between the sleep and this wake - say nothing
    label = work_status.guest_job_label(job.mode)
    _broadcast(job.id, status_label=label, status_at=db.now())


async def _status_pinger(job: GuestJob):
    """While `job` stays "running": say nothing for the first
    CHECKIN_THRESHOLD_S (no chatter for work that finishes fast enough), then
    check in, then repeat no more often than every CHECKIN_INTERVAL_S.
    Cancelled by `_run`'s `finally` the instant the job settles, however it
    settles (completed/failed/cancelled) - this loop itself also re-checks
    job.status on every wake as a second line of defense against a ping racing
    right after settlement."""
    try:
        await asyncio.sleep(work_status.CHECKIN_THRESHOLD_S)
        while job.status == "running":
            _ping(job)
            await asyncio.sleep(work_status.CHECKIN_INTERVAL_S)
    except asyncio.CancelledError:
        pass


async def _wait_for_pause(chat_id: int, settle_s: float):
    """Block until the chat has been round-idle for `settle_s` continuous
    seconds (a 'natural pause'), bounded by HANDBACK_MAX_WAIT_S. Imported lazily
    to keep guestjobs off engine's heavier import graph."""
    from . import rounds
    waited = 0.0
    idle_for = 0.0
    while waited < HANDBACK_MAX_WAIT_S:
        if rounds.active(chat_id) is None:
            idle_for += _PAUSE_POLL_S
            if idle_for >= settle_s:
                return True
        else:
            idle_for = 0.0
        await asyncio.sleep(_PAUSE_POLL_S)
        waited += _PAUSE_POLL_S
    return False


async def _run(job: GuestJob, task, repo, context, cfg, mode, resume,
               model, effort, narrate, handback, session_key, ref):
    # A delegated job can run for minutes with no active round to carry
    # any acknowledgement - the pinger is the equivalent mechanism for that
    # case (backend/work_status.py's same thresholds/wording), cancelled the
    # moment this job settles, however it settles.
    pinger = asyncio.create_task(_status_pinger(job))
    try:
        try:
            async for kind, payload in guest.run_guest(
                    task, repo, context, cfg, mode=mode, resume=resume,
                    chat_id=job.chat_id, model=model, effort=effort,
                    session_key=session_key, ref=ref):
                if kind == "text":
                    job.content += payload
                elif kind == "usage":
                    job.usage = payload
                    if payload.get("session_id"):
                        job.session_id = payload["session_id"]
                elif kind == "tool":
                    job.tools.append(payload)
                    job.step_count += 1
                    _broadcast(job.id, step_count=job.step_count)
        except asyncio.CancelledError:
            # App shutdown (not a turn abort - the whole point of the detached
            # job is that turn aborts DON'T reach here). Persist whatever was
            # gathered, marked.
            job.content += "\n\n[guest run stopped]" if job.content else ""
            mid = _persist_reply(job)
            job.status = "cancelled"
            _broadcast(job.id, status="cancelled",
                       **({"message_id": mid} if mid else {}))
            raise
        except Exception as e:
            log.exception("guest job %s (chat %s) failed", job.id, job.chat_id)
            job.status = "failed"
            _broadcast(job.id, status="failed", error=str(e)[:500])
            return
    finally:
        pinger.cancel()
        try:
            await pinger
        except asyncio.CancelledError:
            pass

    mid = _persist_reply(job)
    kind = _classify(job.content) if mid else "result"
    job.kind = kind
    job.status = "completed"
    _broadcast(job.id, status="completed", kind=kind, session_id=job.session_id,
               **({"message_id": mid} if mid else {}))

    # Hand the conversation back through a narrating model. Blockers interrupt
    # promptly (no settle); results wait for a natural pause. A guest summoned
    # from WITHIN a hand-back round does not narrate (narrate=False) - that
    # bounds auto-rounds exactly like the older single-follow-up rule.
    if narrate and handback and mid:
        settle = BLOCKER_SETTLE_S if kind == "blocker" else RESULT_SETTLE_S
        try:
            if await _wait_for_pause(job.chat_id, settle):
                await handback(job.chat_id, kind)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("guest job %s hand-back failed", job.id)


def start(chat_id, task, repo, context, cfg, *, mode="investigate", resume=None,
          model="", effort="", requested_by="unknown", narrate=True,
          handback=None, prefix="", session_key=None, ref="") -> GuestJob | None:
    """Spawn a detached guest run for `chat_id` and return its GuestJob. Returns
    None (a no-op) if a job is already in flight for the chat - the block is
    normally enforced earlier at summon time, this is the last-line guard.

    `handback(chat_id, kind)` is an async callable the caller supplies (engine
    owns the roster/round machinery) that relays the finished guest reply back
    into the conversation. `narrate=False` suppresses it.

    `session_key` keys this visit's isolated worktree. None means a fresh
    session, keyed by the job's own id so it never shares a checkout with
    another; a resume passes the prior visit's key so the cwd - and the CLI's
    session lookup - matches."""
    if active_for_chat(chat_id):
        log.info("guest job refused for chat %s - one already running", chat_id)
        return None
    con = db.connect()
    row = db.insert_guest_job(con, chat_id, task, repo, mode, requested_by)
    con.close()
    job = GuestJob(row)
    job.content = prefix or ""  # e.g. a continue_last repo-mismatch note
    _jobs[job.id] = job
    # A fresh visit is isolated by its own job id; a resume reuses the stored key.
    key = session_key or job.id
    events.notify_guest_job()  # initial 'running' push
    job.task = asyncio.create_task(
        _run(job, task, repo, context, cfg, mode, resume, model, effort,
             narrate, handback, key, ref))
    job.task.add_done_callback(lambda t: _reap(job.id))
    return job


def _reap(job_id: int):
    """Drop a settled job from the live registry. The DB row (durable status)
    stays - a client that connects later still reads the final state."""
    _jobs.pop(job_id, None)


async def drain_for_test(chat_id: int | None = None):
    """Await all in-flight guest tasks (their hand-backs included). Test-only:
    detached tasks otherwise outlive the request that spawned them."""
    tasks = [j.task for j in list(_jobs.values())
             if j.task and (chat_id is None or j.chat_id == chat_id)]
    for t in tasks:
        try:
            await t
        except Exception:
            pass
