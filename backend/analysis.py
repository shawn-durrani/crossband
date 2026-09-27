"""The Analysis page's measurements (#407): the eval harnesses that answer a
question with a report, run from the app as background jobs.

The owner split the harnesses in two on 15 September. A guard runs in CI on
every change and passes or fails (the silence eval, the doc-style test). A
measurement runs by hand when a decision needs numbers, produces a report to
read, and can cost money or touch real data. Only measurements live here;
guards stay in CI and never get a button.

How a run works:

- The page names a measurement by its id and nothing else. The command comes
  from the fixed table below, so a request can't point a run at a private
  replay set, ask for content or add a flag. A table entry carrying a content
  flag (`--with-content`, `--show-words`, `--fixtures-dir`) is refused before
  anything starts, which pins the recall replay's rule: it never writes the
  words of your chats from here.
- The harness runs as a child process (`python -m eval_x`) and is never
  imported: the harnesses measure and stay out of the app. The child runs
  under a 077 umask and writes its report twice, the markdown a terminal
  would print and the JSON, into a folder of its own under data/analysis/.
  The folder is 0700 and every file in it is 0600, re-sealed when the run
  settles.
- One run per measurement at a time. A running one counts on the busy route,
  so a deploy waits for it. A stop, a timeout or the app shutting down sends
  SIGINT first, so the harness's own cleanup runs (the voice rig stops its
  second app), and a kill follows the grace.
- While it runs, the record carries a status ping on a guest visit's cadence
  (backend/work_status.py), from a fixed label. When it ends, the record
  carries a one-line headline read from the JSON report.
- The recall replay reads your own chat history. Its report is checked for
  words before it's kept, and a report carrying any is deleted.

Owner-only: routers/analysis.py needs a signed-in session on every route,
even before a password is enrolled, so a Claude Code guest on loopback, a
tailnet caller without a session and the machine side-channel all get 401.
No seat tool and no guest diagnostic reaches this module. A spoken "run the
recall replay" would spend money on whoever said it, so a `run_eval` seat
tool waits for the owner's call.
"""

import asyncio
import collections
import json
import logging
import os
import re
import shlex
import shutil
import signal
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from . import db, work_status
from .config import ROOT

log = logging.getLogger("crossband.analysis")

REPORT_MD = "report.md"
REPORT_JSON = "report.json"
RECORD = "run.json"

RUNNING = "running"
DONE = "done"
FAILED = "failed"
STOPPED = "stopped"
TIMED_OUT = "timed out"
INTERRUPTED = "interrupted"
# Every state a run record can be in. The page's badges mirror these
# (frontend/src/analysisView.js, through tests/fixtures/backend_contract.json).
RUN_STATES = (RUNNING, DONE, FAILED, STOPPED, TIMED_OUT, INTERRUPTED)

# Flags that put words from your own data into a report, or feed a run a
# private replay set. Never on a run started from the page.
CONTENT_FLAGS = ("--with-content", "--show-words", "--fixtures-dir")

# The child sets a private umask, then runs the harness package exactly as
# `python -m <package>` would. `-c` keeps the umask inside the child: no
# preexec_fn in a threaded server, and nothing it writes is ever 0644. It
# also puts back Python's own SIGINT handler, since an app started with
# SIGINT ignored would hand that on and a stop would skip the cleanup.
CHILD = ("import os, runpy, signal, sys; os.umask(0o077); "
         "signal.signal(signal.SIGINT, signal.default_int_handler); "
         "sys.argv = sys.argv[1:]; "
         "runpy.run_module(sys.argv[0], run_name='__main__', alter_sys=True)")

STOP_GRACE_S = 20.0      # the voice rig gives its second app 20 s to stop
SHUTDOWN_GRACE_S = 12.0  # inside the service's own graceful stop
TAIL_LINES = 6           # the end of stderr, kept for a failed run's reason
TAIL_CHARS = 300
REPORT_MAX_BYTES = 2_000_000
LIST_LIMIT = 50

# The status ping's one label: fixed, never a harness's own words.
PING_LABEL = "Still running"

RUN_ID_RE = re.compile(r"^([a-z]+)-\d{8}-\d{6}(-\d+)?$")


@dataclass(frozen=True)
class Measurement:
    """One measurement as the page states it. Every sentence here is shown
    before the Run button, so it has to stay true of what `args` runs."""
    id: str
    module: str
    title: str
    measures: str
    costs: str
    takes: str
    touches: str
    args: tuple = ()
    timeout_s: float = 1800.0
    own_data: bool = False      # reads your own install, not made-up data
    quiet_voice: bool = False   # shares the diariser: waits out a live call
    readme: str = ""


MEASUREMENTS = (
    Measurement(
        id="critic", module="eval_critic", title="Critic eval",
        measures=("Can a cheap critic catch a made-up memory fact in a draft "
                  "reply before it's sent? It scores how many it catches and "
                  "how often it flags a correct reply."),
        costs=("About 2 cents. One claude-haiku-4-5 call for each of the 17 "
               "made-up drafts."),
        takes="About a minute.",
        touches=("Made-up drafts and memories from the repo, sent to "
                 "Anthropic on your key. Nothing of yours is read."),
        args=("--model", "claude-haiku-4-5"),
        timeout_s=900.0, readme="eval_critic/README.md"),
    Measurement(
        id="attribution", module="eval_attribution",
        title="Attribution replay",
        measures=("Which way of laying out the transcript lets a seat say "
                  "who said what, including about itself? It asks 27 "
                  "questions in each of three layouts."),
        costs=("About a dollar, measured on a run in September. Most of it "
               "is GPT-5 thinking before each answer."),
        takes="Ten minutes or more.",
        touches=("Made-up group chats from the repo, sent to Anthropic and "
                 "OpenAI on your keys. Nothing of yours is read."),
        args=("--model", "claude-haiku-4-5", "--model", "gpt-5",
              "--max-tokens", "4000", "--timeout-s", "120"),
        timeout_s=2700.0, readme="eval_attribution/README.md"),
    Measurement(
        id="recall", module="eval_recall", title="Recall replay",
        measures=("Are the facts a round fetches from memory worth the wait? "
                  "It replays your newest 200 turns through memory recall "
                  "and scores what comes back."),
        costs=("Next to nothing. Membro embeds 200 short queries, and no "
               "chat model is called."),
        takes="About two minutes, and membro has to be up.",
        touches=("Your own chat history, read only, and your membro, asked "
                 "200 times under the origin eval. The report keeps ids, "
                 "scores and timings, never the words."),
        timeout_s=1200.0, own_data=True, readme="eval_recall/README.md"),
    Measurement(
        id="intent", module="eval_intent", title="Spoken intent check",
        measures=("Does the one model call that reads every turn hear a "
                  "spoken instruction, like a name or a room command? It's "
                  "scored against a baseline of fixed phrase lists."),
        costs=("About 20 cents. The utility model reads 120 made-up turns, "
               "once for the app's path and up to once more for the "
               "baseline."),
        takes="About five minutes.",
        touches=("Made-up turns from the repo, sent to Anthropic on your "
                 "key. Nothing of yours is read."),
        timeout_s=1800.0, readme="eval_intent/README.md"),
    Measurement(
        id="voice", module="eval_voice", title="Voice rig",
        measures=("Does the app put the right name on each spoken turn, over "
                  "noise and when two people talk at once? It plays made-up "
                  "conversations into a second copy of the app and scores "
                  "every turn."),
        costs=("About 35 cents the first time, for ElevenLabs to speak the "
               "lines, which are then kept. After that about 2 cents a run "
               "to transcribe and read the turns."),
        takes=("About ten minutes, longer the first time while the voice "
               "models download."),
        touches=("A second copy of the app on port 8920 with its own data "
                 "folder and memory switched off, so your chats, people and "
                 "memory aren't touched. It shares the diariser with your "
                 "app, so it waits while a voice chat is live."),
        timeout_s=2700.0, quiet_voice=True, readme="eval_voice/README.md"),
)

_BY_ID = {m.id: m for m in MEASUREMENTS}


class Refused(Exception):
    """The request can't run as asked. The message is for the page."""


class Busy(Exception):
    """Something already running, or live, stands in the way."""


class Job:
    """One live run. The run.json on disk is the durable record; this holds
    the process, the task and the end of stderr."""

    def __init__(self, m: Measurement, record: dict, run_dir: Path):
        self.m = m
        self.record = record
        self.run_dir = run_dir
        self.proc = None
        self.task: asyncio.Task | None = None
        self.stop_reason = ""
        self.stop_note = ""
        self.stopper: asyncio.Task | None = None
        self.tail: collections.deque = collections.deque(maxlen=TAIL_LINES)


# measurement id -> its live run. One entry per measurement at most.
_live: dict[str, Job] = {}


def get(measurement_id: str) -> Measurement | None:
    return _BY_ID.get(measurement_id)


def running_count() -> int:
    """How many measurements are running in this process, for the busy
    route. The live registry, never the files: a run.json left saying
    "running" by a crash must not hold every deploy."""
    return len(_live)


def running_ids() -> list[str]:
    return [m.id for m in MEASUREMENTS if m.id in _live]


# ---------- paths and private files ----------

def runs_root() -> Path:
    return Path(db.DATA_DIR) / "analysis"


def _private_dir(path: Path) -> Path:
    """Make `path` and the analysis root, both 0700 whatever the umask."""
    root = runs_root()
    for d in (root, path):
        d.mkdir(parents=True, exist_ok=True)
        os.chmod(d, 0o700)
    return path


def _write_private(path: Path, text: str) -> None:
    """Atomic write of an owner-only file: created 0600, then renamed."""
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(text)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _write_record(run_dir: Path, record: dict) -> None:
    _write_private(run_dir / RECORD, json.dumps(record, indent=1))


def _seal(run_dir: Path) -> None:
    """Every file in the run folder 0600 and the folder 0700, whatever the
    harness left. The child's umask already makes them so; this holds the
    promise for any file it wrote some other way."""
    try:
        os.chmod(run_dir, 0o700)
        for p in run_dir.iterdir():
            if p.is_symlink():
                p.unlink()
            elif p.is_file():
                os.chmod(p, 0o600)
    except OSError:
        log.exception("could not seal %s", run_dir.name)


def _iso_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def new_run_id(measurement_id: str) -> str:
    base = f"{measurement_id}-{time.strftime('%Y%m%d-%H%M%S')}"
    run_id, n = base, 2
    while (runs_root() / run_id).exists():
        run_id = f"{base}-{n}"
        n += 1
    return run_id


# ---------- the command ----------

def display_command(m: Measurement, practice: bool = False) -> str:
    """The command a person would type for the same run."""
    args = ["--mock"] if practice else list(m.args)
    return shlex.join(["python", "-m", m.module, *args])


def context_args(m: Measurement, memory_url: str = "") -> list[str]:
    """Arguments that tie a run to this app rather than to whatever the
    child's own settings would find: the recall replay reads this app's
    database and asks this app's membro."""
    if not m.own_data:
        return []
    args = ["--db", str(db.DB_PATH)]
    return args + (["--memory-url", memory_url] if memory_url else [])


def build_command(m: Measurement, practice: bool, run_dir: Path,
                  memory_url: str = "") -> list[str]:
    """The child's argv. Only the table and this app's own paths reach it."""
    args = ["--mock"] if practice else [*m.args, *context_args(m, memory_url)]
    refused = [a for a in args if a.split("=", 1)[0] in CONTENT_FLAGS]
    if refused:
        raise Refused(f"{m.title} can't run with {', '.join(refused)} from "
                      "the Analysis page. A report with words from your "
                      "chats stays a terminal run you choose to make.")
    return [sys.executable, "-c", CHILD, m.module, *args,
            "--format", "markdown", "--out", str(run_dir / REPORT_MD),
            "--json-out", str(run_dir / REPORT_JSON)]


def _child_env() -> dict:
    """The app's own environment, keys included: a measurement spends on
    the same keys the app does, and never sees anything the app can't."""
    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"
    return env


# ---------- what blocks a start ----------

def blocked(m: Measurement, voice_live: bool = False) -> str:
    """Why this measurement can't start right now, or ''."""
    if m.id in _live:
        return f"The {m.title.lower()} is already running. One at a time."
    if m.quiet_voice and voice_live:
        return ("A voice chat is live. The rig shares the diariser with it, "
                "so it waits until the call ends.")
    return ""


# ---------- the run ----------

def start(measurement_id: str, *, practice: bool = False,
          voice_live: bool = False, memory_url: str = "") -> dict:
    """Start one run and return its record. Needs the running loop. Raises
    LookupError for an unknown id, Busy when `blocked` says so, and Refused
    when the command would carry content."""
    m = get(measurement_id)
    if m is None:
        raise LookupError(measurement_id)
    why = blocked(m, voice_live)
    if why:
        raise Busy(why)
    run_id = new_run_id(m.id)
    run_dir = runs_root() / run_id
    argv = build_command(m, practice, run_dir, memory_url)
    _private_dir(run_dir)
    now = time.time()
    record = {
        "run_id": run_id,
        "measurement": m.id,
        "title": m.title,
        "practice": bool(practice),
        "state": RUNNING,
        "command": display_command(m, practice),
        "created_at": _iso_now(),
        "created_at_unix": now,
        "status_label": "",
        "status_at": None,
    }
    _write_record(run_dir, record)
    job = Job(m, record, run_dir)
    _live[m.id] = job
    job.task = asyncio.get_running_loop().create_task(_run(job, argv))
    return dict(record)


async def _drain(job: Job) -> None:
    """Keep the end of stderr and nothing else. Reading it all the way is
    what stops a chatty harness blocking on a full pipe."""
    stream = job.proc.stderr
    while True:
        line = await stream.readline()
        if not line:
            return
        text = line.decode("utf-8", "replace").strip()
        if text:
            job.tail.append(text[:TAIL_CHARS])


async def _interrupt(proc, grace: float) -> None:
    """SIGINT, so the harness's own cleanup runs, then a kill after `grace`."""
    if proc is None or proc.returncode is not None:
        return
    try:
        proc.send_signal(signal.SIGINT)
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(proc.wait(), grace)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        await proc.wait()


async def _pinger(job: Job) -> None:
    """Quiet for the first CHECKIN_THRESHOLD_S, then a ping every
    CHECKIN_INTERVAL_S while the run lasts, the guest visit's cadence."""
    try:
        await asyncio.sleep(work_status.CHECKIN_THRESHOLD_S)
        while job.record["state"] == RUNNING:
            job.record["status_label"] = PING_LABEL
            job.record["status_at"] = time.time()
            _write_record(job.run_dir, job.record)
            await asyncio.sleep(work_status.CHECKIN_INTERVAL_S)
    except asyncio.CancelledError:
        pass


async def _run(job: Job, argv: list[str]) -> None:
    pinger = asyncio.create_task(_pinger(job))
    spawn_error = ""
    try:
        try:
            job.proc = await asyncio.create_subprocess_exec(
                *argv, cwd=str(ROOT), env=_child_env(),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE)
        except OSError as e:
            spawn_error = f"could not start it: {e}"
        if job.proc is not None:
            drain = asyncio.create_task(_drain(job))
            if job.stop_reason:  # a stop that landed before the spawn did
                await _interrupt(job.proc, STOP_GRACE_S)
            try:
                await asyncio.wait_for(job.proc.wait(), job.m.timeout_s)
            except asyncio.TimeoutError:
                job.stop_reason = job.stop_reason or TIMED_OUT
                await _interrupt(job.proc, STOP_GRACE_S)
            if job.stopper is not None:
                await job.stopper
            try:
                await asyncio.wait_for(drain, 5.0)
            except asyncio.TimeoutError:
                drain.cancel()
        _settle(job, spawn_error)
    except asyncio.CancelledError:
        # The loop is going without stop_all: ask the harness to clean up
        # on its own rather than leave it running unasked.
        if job.proc is not None and job.proc.returncode is None:
            try:
                job.proc.send_signal(signal.SIGINT)
            except ProcessLookupError:
                pass
        raise
    finally:
        pinger.cancel()
        if _live.get(job.m.id) is job:
            _live.pop(job.m.id, None)


def _read_report_json(run_dir: Path):
    path = run_dir / REPORT_JSON
    try:
        if path.stat().st_size > REPORT_MAX_BYTES:
            return None
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def carries_content(report) -> bool:
    """Does a recall replay report hold words from your chats? The replay
    puts the query and each fact's text in its corpus only when asked for
    content, and the page never asks, so this is the second lock."""
    if not isinstance(report, dict):
        return False
    for row in report.get("corpus") or []:
        if not isinstance(row, dict):
            continue
        if row.get("query"):
            return True
        for hit in row.get("hits") or []:
            if isinstance(hit, dict) and hit.get("content"):
                return True
    return False


def _remove_reports(run_dir: Path) -> None:
    for name in (REPORT_MD, REPORT_JSON):
        try:
            (run_dir / name).unlink()
        except FileNotFoundError:
            pass


def _settle(job: Job, spawn_error: str = "") -> None:
    rec = job.record
    rc = job.proc.returncode if job.proc is not None else None
    rec["finished_at"] = _iso_now()
    rec["finished_at_unix"] = time.time()
    rec["exit_code"] = rc
    rec["status_label"] = ""
    report = _read_report_json(job.run_dir)
    if job.m.own_data and carries_content(report):
        _remove_reports(job.run_dir)
        report = None
        rec["state"] = FAILED
        rec["error"] = ("The report held words from your chats, so it was "
                        "deleted before anyone could read it.")
    elif job.stop_reason:
        rec["state"] = job.stop_reason
        if job.stop_note:
            rec["error"] = job.stop_note
    elif spawn_error:
        rec["state"] = FAILED
        rec["error"] = spawn_error
    elif rc == 0 and report is not None:
        rec["state"] = DONE
    else:
        rec["state"] = FAILED
        tail = "\n".join(job.tail)
        rec["error"] = tail or f"It stopped with exit code {rc}."
    if rec["state"] == DONE:
        rec["summary"] = summarise(job.m.id, report, rec.get("practice"))
    rec["has_report"] = (job.run_dir / REPORT_MD).is_file()
    _seal(job.run_dir)
    _write_record(job.run_dir, rec)


def stop(run_id: str) -> str:
    """Ask a live run to stop. Returns '' or a plain refusal."""
    job = next((j for j in _live.values() if j.record["run_id"] == run_id),
               None)
    if job is None:
        return "That run isn't running."
    job.stop_reason = STOPPED
    job.stop_note = "You stopped it."
    if job.proc is not None and job.stopper is None:
        job.stopper = asyncio.get_running_loop().create_task(
            _interrupt(job.proc, STOP_GRACE_S))
    return ""


async def stop_all(grace: float = SHUTDOWN_GRACE_S) -> None:
    """The app is stopping: interrupt every live run and let each settle,
    so its record says what happened and the voice rig's second app goes
    down with it."""
    jobs = list(_live.values())
    for job in jobs:
        job.stop_reason = STOPPED
        job.stop_note = "The app restarted while it ran."
    await asyncio.gather(*(_interrupt(j.proc, grace) for j in jobs),
                         return_exceptions=True)
    for job in jobs:
        if job.task is None:
            continue
        try:
            await asyncio.wait_for(asyncio.shield(job.task), 5.0)
        except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
            pass


# ---------- the headline ----------

def _pct(x) -> str:
    return "n/a" if x is None else f"{round(float(x) * 100)}%"


def _no_key(n: int) -> str:
    """A run whose calls found no key scores its misses as real ones, so the
    headline says so first."""
    if not n:
        return ""
    return f"{n} call{'s' if n != 1 else ''} had no key, so read this with care. "


def _critic(r: dict):
    results = r.get("results") or []
    spent = sum(float(x.get("cost_usd") or 0) for x in results)
    missing = sum(1 for x in results if x.get("failure_mode") == "missing_key")
    return (_no_key(missing) +
            f"Caught {_pct(r.get('unsafe_draft_recall'))} of the made-up "
            f"facts and flagged {_pct(r.get('control_false_alarm_rate'))} of "
            "the correct replies.", spent)


def _attribution(r: dict):
    acc = r.get("accuracy") or {}
    models = list(dict.fromkeys(k.split("|", 1)[1] for k in acc if "|" in k))
    today = [f"{_pct(acc.get('current|' + m))} on {m}" for m in models
             if f"current|{m}" in acc]
    others = [v for k, v in acc.items() if not k.startswith("current|")]
    line = _no_key(int(r.get("missing_key") or 0)) + \
        f"Today's layout answered {', '.join(today) or 'nothing'}."
    if others:
        line += f" The best other layout reached {_pct(max(others))}."
    spent = sum(float(v or 0) for v in (r.get("cost_usd") or {}).values())
    return line, spent


def _recall(r: dict):
    n = r.get("n_turns") or 0
    got = r.get("turns_with_hits") or 0
    p50 = (r.get("recall_ms") or {}).get("p50")
    line = (f"Replayed {n} turns. {_pct(got / n if n else None)} brought "
            "facts back")
    line += f", in {round(p50)} ms at the median." if p50 is not None else "."
    return line, None


def _intent(r: dict):
    s = r.get("strategies") or {}
    merged = (s.get("merged") or {}).get("all_axes_right")
    today = (s.get("today") or {}).get("all_axes_right")
    parts = []
    if merged is not None:
        parts.append(f"The one call heard {_pct(merged)} of turns right on "
                     "every axis")
    if today is not None:
        parts.append(f"the phrase lists {_pct(today)}")
    spent = sum(float(v.get("cost_per_turn_usd") or 0) * (v.get("turns") or 0)
                for v in s.values() if isinstance(v, dict))
    missing = sum(int(v.get("missing_key") or 0) for v in s.values()
                  if isinstance(v, dict))
    line = (", ".join(parts) + ".") if parts else "The report is saved."
    return _no_key(missing) + line, spent


def _voice(r: dict):
    right = ((r.get("summary") or {}).get("right") or {}).get("share")
    wrong = (r.get("targets") or {}).get("wrong_per_100")
    line = f"Named {_pct(right)} of {r.get('turns') or 0} turns right"
    line += (f", with {wrong:g} wrong names per 100 turns."
             if wrong is not None else ".")
    cost = r.get("cost") or {}
    spent = float((cost.get("tts") or {}).get("usd") or 0) + \
        float(cost.get("stt_usd") or 0)
    return line, spent


_SUMMARIES = {"critic": _critic, "attribution": _attribution,
              "recall": _recall, "intent": _intent, "voice": _voice}


def summarise(measurement_id: str, report, practice: bool = False) -> dict:
    """A run's one-line headline and what its report says it spent, from the
    JSON report. A report of a shape this doesn't know still gets a line."""
    try:
        headline, spent = _SUMMARIES[measurement_id](report)
    except Exception:
        headline, spent = "The report is saved.", None
    if practice:
        headline = "Practice run, made-up numbers. " + headline
        spent = None
    return {"headline": headline,
            "spent_usd": round(spent, 4) if spent else None}


# ---------- stored runs ----------

def load_record(run_id: str):
    """One run's record, live first. A run the disk says is running with no
    live job died with an earlier process, and says so."""
    m = RUN_ID_RE.match(run_id or "")
    if not m or m.group(1) not in _BY_ID:
        return None
    job = _live.get(m.group(1))
    if job is not None and job.record["run_id"] == run_id:
        return dict(job.record)
    try:
        rec = json.loads((runs_root() / run_id / RECORD).read_text())
    except (OSError, ValueError):
        return None
    if rec.get("state") == RUNNING:
        rec["state"] = INTERRUPTED
        rec["error"] = "The app stopped while it ran."
    return rec


def list_runs(limit: int = LIST_LIMIT) -> list[dict]:
    root = runs_root()
    if not root.is_dir():
        return []
    out = []
    for d in root.iterdir():
        if d.is_dir() and RUN_ID_RE.match(d.name):
            rec = load_record(d.name)
            if rec:
                out.append(rec)
    out.sort(key=lambda r: r.get("created_at_unix") or 0, reverse=True)
    return out[:limit]


def report_text(run_id: str):
    """(record, markdown) for one run, or None. The markdown is '' when the
    run left none."""
    rec = load_record(run_id)
    if rec is None:
        return None
    path = runs_root() / run_id / REPORT_MD
    text = ""
    try:
        if path.is_file() and path.stat().st_size <= REPORT_MAX_BYTES:
            text = path.read_text(errors="replace")
    except OSError:
        text = ""
    return rec, text


def report_json_path(run_id: str):
    if load_record(run_id) is None:
        return None
    path = runs_root() / run_id / REPORT_JSON
    return path if path.is_file() and not path.is_symlink() else None


def delete_run(run_id: str) -> str:
    """Remove one stored run. Returns '' or a plain refusal."""
    rec = load_record(run_id)
    if rec is None:
        return "No such run."
    if rec.get("state") == RUNNING:
        return "That run is still going. Stop it first."
    run_dir = runs_root() / run_id
    if run_dir.is_symlink() or not run_dir.is_dir():
        return "No such run."
    shutil.rmtree(run_dir)
    return ""


# ---------- the page's view ----------

def catalogue(voice_live: bool = False) -> list[dict]:
    out = []
    for m in MEASUREMENTS:
        job = _live.get(m.id)
        out.append({
            "id": m.id, "title": m.title, "measures": m.measures,
            "costs": m.costs, "takes": m.takes, "touches": m.touches,
            "own_data": m.own_data, "readme": m.readme,
            "command": display_command(m),
            "blocked": blocked(m, voice_live),
            "running": dict(job.record) if job else None,
        })
    return out
