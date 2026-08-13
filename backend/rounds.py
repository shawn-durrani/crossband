"""Detached rounds: generation survives the connection.

A round runs as a background task writing SSE events into a per-chat buffer;
HTTP responses merely TAIL that buffer. A client disconnect (a dead zone on
the highway) no longer cancels generation - the replies finish and persist,
and the client catches up from where it left off when the tunnel returns
(`GET /round/stream?round_id&after=N`). Stopping a round is now an explicit
act (`POST /round/abort`) instead of "the connection closed" - so a network
drop stops masquerading as a user barge-in.
"""

import asyncio
import itertools
import logging

log = logging.getLogger("crossband.rounds")

_ids = itertools.count(1)
_rounds: dict[int, "Round"] = {}  # chat_id -> latest round (kept after done for catch-up)


class Round:
    def __init__(self, chat_id: int):
        self.chat_id = chat_id
        self.round_id = next(_ids)
        self.events: list[str] = []
        self.done = False
        self.cond = asyncio.Condition()
        self.task: asyncio.Task | None = None

    async def _emit(self, chunk: str):
        async with self.cond:
            self.events.append(chunk)
            self.cond.notify_all()

    async def _finish(self):
        async with self.cond:
            self.done = True
            self.cond.notify_all()

    async def tail(self, after: int = 0):
        """Yield events from index `after`; ends when the round is done and
        drained. Cancelling a tail (client gone) never touches the round."""
        seq = after
        while True:
            async with self.cond:
                while seq >= len(self.events) and not self.done:
                    await self.cond.wait()
                if seq >= len(self.events):
                    return
                chunk = self.events[seq]
                seq += 1
            yield chunk


def active(chat_id: int) -> Round | None:
    r = _rounds.get(chat_id)
    return r if r and not r.done else None


def latest(chat_id: int) -> Round | None:
    """The chat's most recent round, DONE OR NOT - its buffer lives until
    the next round starts (#64). This is what lets a voice client speak a
    narration whose round finished before the client could react: a
    single-narrator hand-back persists its reply as the round's LAST act,
    so by the time the new_message event reaches the client, active() is
    already None and a replay of this buffer is the only way to voice it."""
    return _rounds.get(chat_id)


def active_chat_ids() -> list[int]:
    """Chat ids with a round still generating - the source of truth for the
    running-task indicators. Because rounds are DETACHED (they keep
    running after the client navigates away), this reports background activity a
    client-only flag cannot see, and clears the moment the round finishes."""
    return [cid for cid, r in _rounds.items() if not r.done]


def get(chat_id: int, round_id: int) -> Round | None:
    r = _rounds.get(chat_id)
    return r if r and r.round_id == round_id else None


def start(chat_id: int, agen) -> Round:
    """Run `agen` (an SSE-string async generator) to completion in the
    background, buffering every event. One active round per chat - callers
    check active() first (the API returns 409)."""
    r = Round(chat_id)

    async def _run():
        try:
            async for chunk in agen:
                await r._emit(chunk)
        except asyncio.CancelledError:
            # explicit abort: run_round already persisted the partial reply
            # with its cut-off marker; tell any attached clients, then close.
            await r._emit('data: {"type": "aborted"}\n\n')
        except Exception as e:
            log.exception("detached round %s (chat %s) failed", r.round_id, chat_id)
            await r._emit(f'data: {{"type": "error", "speaker": "", '
                          f'"message": {_json_str(str(e))}}}\n\n')
        finally:
            await r._finish()

    r.task = asyncio.create_task(_run())
    _rounds[chat_id] = r  # strong ref: task + buffer live until the next round
    return r


ABORT_SETTLE_S = 10.0  # bound on waiting for a cancelled round to settle


async def abort(chat_id: int) -> bool:
    """Cancel the active round (the deliberate stop: barge-in / Stop button).
    The in-flight reply persists with its cut-off marker via run_round's own
    CancelledError handling. We wait - BOUNDED - for the task to settle so a
    follow-up send can't race the persistence; but a wedged Claude Code guest
    subprocess must never latch the running lock, so if the task refuses
    to die within ABORT_SETTLE_S we force the round DONE anyway. The orphaned
    task keeps trying to unwind in the background; losing a partial reply from a
    genuinely stuck subprocess is the accepted trade - clearing the deadlock (so
    the chat unblocks and the running indicator drops) wins."""
    r = active(chat_id)
    if not r or not r.task:
        return False
    r.task.cancel()
    try:
        # shield: on timeout we stop WAITING but never re-cancel the task (nor
        # let our own caller's cancellation strand it half-torn-down).
        await asyncio.wait_for(asyncio.shield(r.task), timeout=ABORT_SETTLE_S)
    except asyncio.TimeoutError:
        log.warning("round %s (chat %s) did not settle within %.0fs of abort; "
                    "force-clearing the running lock", r.round_id, chat_id,
                    ABORT_SETTLE_S)
        await r._finish()  # done=True → active() clears, the chat unblocks
    except (asyncio.CancelledError, Exception):
        pass
    return True


def _json_str(s: str) -> str:
    import json
    return json.dumps(s[:300])
