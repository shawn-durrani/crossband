"""Am I busy? The answer a deploy reads before it restarts this service
(#343, the fleet decision on workbench#69).

The deploy watcher used to guess from one process-name search, machine
wide: it saw a Claude Code visit for any app and nothing else. A round
still generating, a live microphone, an import, a benchmark, a person
sync pass or a backup mid-copy all looked idle, and a visit for another
app held this one's restart for no reason. Now the watcher asks, and this
module is the answer.

Every check is an in-process read: a registry the work already keeps, a
lock it already holds, or a counter this change adds. No database, no
network, no model. The route must answer inside the watcher's two-second
budget even while the app is under load, and a probe that could stall is
a probe the watcher learns to ignore.

The reasons are FIXED LABELS from `LABELS`, nothing else: never a chat id,
a person's name, a title or a transcript. The route is open on loopback
without a session, so the only thing it may say about the work is that
it exists.
"""

from . import benchmark, db, guestjobs, importer, person_sync, rounds
from .routers import voice as voice_router

PATH = "/api/busy"

ROUND_RUNNING = "round running"
VOICE_CAPTURE_RUNNING = "voice capture running"
GUEST_VISIT_RUNNING = "guest visit running"
PERSON_SYNC_RUNNING = "person sync running"
BENCHMARK_RUNNING = "benchmark running"
IMPORT_RUNNING = "import running"
BACKUP_RUNNING = "backup running"

# The whole vocabulary, in the order the route reports them. The watcher
# matches on these strings; add a label here and in `reasons()` together.
LABELS = (
    ROUND_RUNNING,
    VOICE_CAPTURE_RUNNING,
    GUEST_VISIT_RUNNING,
    PERSON_SYNC_RUNNING,
    BENCHMARK_RUNNING,
    IMPORT_RUNNING,
    BACKUP_RUNNING,
)


def reasons() -> list[str]:
    """Every kind of work in flight right now, as labels. Empty means idle.

    What each one reads, and what a restart would cost without it:

    - a round generating in any chat (`rounds`): the reply is cut off and
      the partial stays without its cut-off marker;
    - a live voice capture (`routers.voice._captures`): the microphone
      relay dies under the speaker mid-utterance;
    - a guest visit (`guestjobs`): a Claude Code build in another chat dies
      with the backend, the case the old process search existed for;
    - a person sync pass (`person_sync._lock`): a clip upload or a
      correction replay to membro stops half way, to be redone next pass;
    - a benchmark (`benchmark._active`): the run's results.json is left
      saying "running" for ever;
    - an import (`importer`): an export lands half way, chats here and
      membro not yet seeded;
    - a backup (`db`): the mirror copy is a plain file copy, so a kill
      mid-copy leaves a truncated snapshot under a finished-looking name.

    Short fire-and-forget work (a voice identity pass, a post-round
    summary, a memory write) is deliberately not here: the graceful stop
    already gives in-flight work fifteen seconds, which covers it."""
    out = []
    if rounds.active_chat_ids():
        out.append(ROUND_RUNNING)
    if voice_router.capture_sessions():
        out.append(VOICE_CAPTURE_RUNNING)
    if guestjobs.running_count():
        out.append(GUEST_VISIT_RUNNING)
    if person_sync._lock.locked():
        out.append(PERSON_SYNC_RUNNING)
    if benchmark._active:
        out.append(BENCHMARK_RUNNING)
    if importer.in_flight():
        out.append(IMPORT_RUNNING)
    if db.backup_running():
        out.append(BACKUP_RUNNING)
    return out


def snapshot() -> dict:
    """The route's body: `{"busy": bool, "reasons": [labels]}`."""
    found = reasons()
    return {"busy": bool(found), "reasons": found}
