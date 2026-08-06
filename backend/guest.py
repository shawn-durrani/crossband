"""Claude Code as a temporary in-chat guest specialist (phase 1: plan-only).

Any participant (or the user, via a model) can summon Claude Code into the
conversation with the `summon_claude_code` tool. The summons is queued and the
guest takes ONE turn after the normal responder loop finishes, so the regular
speakers keep their rhythm and the guest replies with everyone's round in view.

The guest runs through the Claude Agent SDK, which drives the locally installed
Claude Code CLI — so it authenticates exactly like the user's own Claude Code
sessions and needs no extra API key. Phase 1 is deliberately read-only: the SDK
options restrict it to Read/Grep/Glob (enforced by the harness, not the
prompt), scoped to one configured repository. It investigates and answers, or
produces a plan for a future session to execute — it changes nothing.

Configuration (config.local.json / MMC_ env):
  code_repos: {"crossband": "~/dev/crossband", ...}
      Repositories the guest may be pointed at. Empty (the default) keeps the
      feature dark — the tool is never offered.
  code_mcp: {"membro": {"command": ".../.venv/bin/python",
                        "args": ["-m", "memory_service.mcp_server"]}}
      Optional MCP servers mounted into the guest (e.g. Membro, so the guest
      can recall long-term memory while planning). Read-side only by design:
      Membro quarantines any mcp-origin write anyway ("gate the write").
"""

import asyncio
import logging
import os
import re
import shutil
import time
from pathlib import Path

from . import diag_mcp, provenance

log = logging.getLogger("mmc.guest")

# A ${VAR} reference inside a code_mcp server's env value. This is the ONLY
# interpolation the guest does — deliberately narrow: exact ${NAME} tokens,
# resolved from Crossband's own process environment at launch. It lets an
# authenticated read MCP (e.g. membro-admin) be wired to guests by REFERENCING
# a secret instead of pasting it into config.local.json. Missing vars resolve
# to "" (never the literal ${VAR}), so a mis-set reference fails closed —
# the tool gets no credential rather than a bogus one.
_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _interpolate_env(env: dict | None) -> dict:
    """Resolve ${VAR} references in a code_mcp server's env from os.environ."""
    resolved = {}
    for key, value in (env or {}).items():
        def _sub(m):
            name = m.group(1)
            if name not in os.environ:
                log.warning("code_mcp env %s references unset ${%s} — using ''",
                            key, name)
            return os.environ.get(name, "")
        resolved[key] = _ENV_REF.sub(_sub, str(value))
    return resolved

SLUG = "claude-code"
NAME = "Claude Code"
COLOR = "#c15f3c"  # distinct from the resident Claude participant's orange

# Per-summon model selection. A constrained alias set — NOT free-form model
# strings — is the whole safety story: the caller can only ever name one of
# these, they map to the SDK's structured `model` option (no shell, no
# interpolation), and aliases don't rot when Anthropic bumps model versions.
# "default" (and omission) means: pass nothing, so Claude Code uses whatever
# its own login/CLI default is — identical to the behavior before this option
# existed. Model choice is INDEPENDENT of auth: picking a cheaper alias does
# not change whether the turn bills to the subscription or the API key.
MODEL_ALIASES = ("default", "opus", "sonnet", "haiku")


def resolve_model(alias) -> str | None:
    """Map a model alias to the SDK `model` value. Returns None to omit the
    override entirely (Claude Code's own default). Unknown/blank → None, so a
    stale config value degrades to current behavior rather than erroring at
    runtime — the tool boundary (request) is where bad input is rejected."""
    if alias in ("opus", "sonnet", "haiku"):
        return alias  # the CLI accepts these short aliases directly
    return None  # "default", "", None, or anything unexpected


# Per-summon effort / thinking level. Same safety story as MODEL_ALIASES: a
# constrained alias set, mapped server-side to a thinking-token budget that
# Claude Code reads from the MAX_THINKING_TOKENS env var — a structured value,
# no prompt injection, no shell. The budgets mirror the CLI's own thinking
# keywords ("think" → "megathink"/think-hard → "ultrathink"). "default" (and
# omission) forces nothing: Claude Code's own thinking behavior, byte-for-byte
# identical to before this option existed.
EFFORT_ALIASES = ("default", "think", "think-hard", "ultrathink")
_EFFORT_TOKENS = {"think": 4000, "think-hard": 10000, "ultrathink": 31999}


def resolve_effort(alias) -> int | None:
    """Map an effort alias to a MAX_THINKING_TOKENS budget. Returns None to omit
    the override (Claude Code's own default). Unknown/blank → None, so a stale
    config value degrades to current behavior rather than erroring at runtime —
    the tool boundary (request) is where bad input is rejected."""
    return _EFFORT_TOKENS.get((alias or "").strip())


# Teardown budget: a wall-clock cap on tearing a guest visit down — closing the
# SDK iterator (which drives the Claude Code subprocess) plus removing the git
# worktree. Either step can block on a wedged subprocess (or a stuck `git`
# call), so bounding them means a stalled guest can never latch the round's
# running lock. The abort path force-clears after its own longer wait
# (rounds.ABORT_SETTLE_S) as a second line of defense.
GUEST_TEARDOWN_S = 5.0

# Investigate mode (default): read, never mutate. Enforced by SDK options.
READ_ONLY_TOOLS = ["Read", "Grep", "Glob"]
# names must match the installed CLI's tools exactly — an unknown name is a
# startup error, not a silent no-op (verified against the tool names the CLI
# documents; re-check this list when the CLI adds or renames a tool)
DENIED_TOOLS = ["Bash", "Write", "Edit", "NotebookEdit",
                "WebFetch", "WebSearch", "Task", "TodoWrite", "KillShell"]

# Implement mode (opt-in via code_allow_writes): the guest branches,
# implements, tests, pushes and opens a PR — it NEVER merges. Bash is
# constrained to command prefixes; these are guardrails against accidents,
# not a sandbox — the real gates are the human-reviewed merge and (at the
# public flip) branch protection on main.
#
# Why the allow-list has to match the project's ACTUAL commands: these rules
# auto-run only their exact prefix, and the session is headless — there is no
# human to click "approve", so anything the guest types that ISN'T pre-approved
# is denied outright (permission_mode="dontAsk"). A rule that names
# `.venv/bin/python` does NOT cover the canonical keyless test command from
# CLAUDE.md (`env -u OPENAI_API_KEY -u ANTHROPIC_API_KEY .venv/bin/python -m
# pytest ...`), which starts with `env` — so the guest could never run the
# suite. Every command class the guest legitimately needs is spelled out below.
IMPLEMENT_TOOLS = ["Read", "Grep", "Glob", "Edit", "Write", "Bash", "TodoWrite"]

# Project test commands. `env -u …` only UNSETS variables before the pinned
# interpreter, so the prefix stays as narrow as `.venv/bin/python` itself —
# both orderings of the two keys are spelled out because a model may swap them.
_TEST_ALLOWED = [
    "Bash(.venv/bin/python:*)", "Bash(.venv/bin/pytest:*)",
    "Bash(env -u OPENAI_API_KEY -u ANTHROPIC_API_KEY .venv/bin/python:*)",
    "Bash(env -u ANTHROPIC_API_KEY -u OPENAI_API_KEY .venv/bin/python:*)",
    "Bash(npm run:*)", "Bash(npm test:*)", "Bash(npm ci:*)",
    "Bash(npm --prefix frontend run:*)", "Bash(npm --prefix frontend test:*)",
    "Bash(npm --prefix frontend ci:*)",
]
# Normal branch/commit/push + reading issues and opening PRs (never merging).
_GIT_ALLOWED = [
    "Bash(git status:*)", "Bash(git diff:*)", "Bash(git log:*)",
    "Bash(git add:*)", "Bash(git commit:*)", "Bash(git checkout:*)",
    "Bash(git switch:*)", "Bash(git branch:*)", "Bash(git restore:*)",
    "Bash(git stash:*)", "Bash(git push:*)", "Bash(git rev-parse:*)",
    "Bash(git fetch:*)", "Bash(git ls-remote:*)", "Bash(git show:*)",
    "Bash(git remote -v:*)", "Bash(git remote get-url:*)",
    "Bash(gh pr create:*)", "Bash(gh pr view:*)", "Bash(gh pr checks:*)",
    "Bash(gh pr diff:*)", "Bash(gh pr list:*)", "Bash(gh pr status:*)",
    "Bash(gh issue view:*)", "Bash(gh issue list:*)", "Bash(gh issue comment:*)",
    "Bash(gh auth status:*)",
]
# Read-only SQLite diagnostics against the live DB — ONLY when explicitly asked.
# The `-readonly` / `-ro` flag opens the database read-only, so a stray UPDATE
# or DROP fails at the engine; a bare `sqlite3 <db>` (which CAN write) is NOT
# on the list and is therefore denied. See docs/GUEST_PERMISSIONS.md for the
# residual dot-command caveat (`.shell`/`.import`).
_SQLITE_ALLOWED = ["Bash(sqlite3 -readonly:*)", "Bash(sqlite3 -ro:*)"]

IMPLEMENT_ALLOWED = (
    ["Read", "Grep", "Glob", "Edit", "Write", "TodoWrite"]
    + _TEST_ALLOWED + _GIT_ALLOWED + _SQLITE_ALLOWED
)
IMPLEMENT_DENIED = [
    # No pushing to main (common spellings), no force-push, no merging.
    "Bash(git push origin main:*)", "Bash(git push -u origin main:*)",
    "Bash(git push origin HEAD:main:*)", "Bash(git push -f:*)",
    "Bash(git push --force:*)", "Bash(git push --force-with-lease:*)",
    "Bash(git merge:*)", "Bash(gh pr merge:*)", "Bash(gh repo:*)",
    # No secret access or exfiltration: reading credential/config files, dumping
    # the environment, or reaching the network outside git/gh are all blocked.
    # disallowed_tools OVERRIDES allowed_tools, so these bite even though "Read"
    # is broadly allowed above.
    "Read(.env)", "Read(**/.env)", "Read(**/.env.*)",
    "Read(**/config.local.json)", "Read(**/*.pem)", "Read(**/id_rsa*)",
    "Bash(env)", "Bash(printenv:*)", "Bash(curl:*)", "Bash(wget:*)",
    "Bash(nc:*)", "Bash(ssh:*)", "Bash(scp:*)",
    # No destructive shell.
    "Bash(rm -rf:*)", "Bash(rm -fr:*)", "Bash(sudo:*)", "Bash(git clean:*)",
    # The guest must never edit its own gate: a PR that can change CI can
    # weaken CI (caught live on a guest PR that edited ci.yml — benignly, but
    # only human eyes would know). CI changes are proposed in the reply and
    # made by a trusted session.
    "Edit(.github/**)", "Write(.github/**)", "Edit(**/.github/**)",
    "Write(**/.github/**)",
    "NotebookEdit", "WebFetch", "WebSearch", "Task", "KillShell",
]

# The behavior contract every coding guest speaks under — part of the guest
# FRAMEWORK, not the Claude Code adapter: any future adapter (Codex etc.)
# appends this same block, so all guests are equally brief and equally willing
# to stop and ask. No format braces here — it survives .format().
GUEST_STYLE = (
    "\n\n## Speaking in the chat\n"
    "Replies are read on a phone. Keep them under ~200 words unless "
    "delivering a plan or PR summary that genuinely needs structure. Lead "
    "with the answer or the question. Never narrate which tools you used or "
    "what you read along the way — the chat renders your tool activity "
    "already — and never restate the task back.\n\n"
    "## Ask before you assume\n"
    "You get one turn per summons, but a turn may END IN QUESTIONS: when the "
    "task is ambiguous in a way that would change what you build (scope, "
    "naming, UX, data shape, anything destructive or hard to reverse), ask "
    "up to three crisp questions INSTEAD of building on guesses. The group "
    "answers and re-summons you with your session intact — asking is cheap, "
    "a wrong build is not. Small safe assumptions are fine: state them in "
    "one line at the top so anyone can correct you."
)

GUEST_SYSTEM = (
    "You are Claude Code, joining a group chat as a specialist teammate "
    "on {user_name}'s machine. You have READ-ONLY access to the "
    "\"{repo}\" repository at {path} — in this mode you cannot run commands "
    "or change files, only read, search and reason. That one checkout is your "
    "entire reach: if the task actually targets a different repository, say so "
    "immediately and ask to be re-summoned with that repo instead of trying "
    "to reach it from here.\n\n"
    "Investigate the task you were summoned for and answer the group. If the "
    "task needs code changes, deliver a precise plan instead: the files "
    "involved, the edits, the risks, and how to verify — written so a future "
    "Claude Code session could execute it without re-deriving your analysis.\n\n"
    "Your reply is read by {user_name} and the other AI participants "
    "verbatim. Keep it in plain English and reference files as path:line so "
    "they're easy to find later." + GUEST_STYLE
)

GUEST_SYSTEM_IMPLEMENT = (
    "You are Claude Code, joining a group chat as a specialist teammate "
    "on {user_name}'s machine, with WRITE access to the \"{repo}\" "
    "repository at {path}. The user asked for changes to be implemented and "
    "shipped as a pull request. That one checkout is your entire reach: if "
    "the task actually targets a different repository, say so immediately and "
    "ask to be re-summoned with that repo instead of trying to reach it from "
    "here.\n\n"
    "Work the way this project ships: create a branch (cc/<short-slug>), "
    "make focused changes, run the test suite, commit, push the branch, and "
    "open a pull request with `gh pr create` (reference the issue if one "
    "exists). NEVER push to main and never merge — the human reviews and "
    "merges every PR. You cannot edit anything under .github/ (you must "
    "not modify the pipeline that gates its own PRs) — if the task needs a "
    "CI change, propose the exact diff in your reply instead. If a plan from an earlier visit exists in your session "
    "or the context, execute that plan rather than re-deriving it. If tests "
    "fail and you cannot fix them, say so honestly and still push the branch "
    "WITHOUT opening a PR.\n\n"
    "Your shell is headless: only pre-approved command shapes run, everything "
    "else is denied silently. Run the suite with the project's own command "
    "(check CLAUDE.md — usually the keyless `env -u OPENAI_API_KEY -u "
    "ANTHROPIC_API_KEY .venv/bin/python -m pytest tests/ -q`). If you need to "
    "inspect the live database, use read-only SQLite: `sqlite3 -readonly "
    "<db> \"SELECT …\"` — plain `sqlite3` is blocked so you cannot mutate it.\n\n"
    "Your reply is read by {user_name} and the other AI participants "
    "verbatim. End with: what changed, the test results, and the PR link. "
    "Ambiguity matters MOST in this mode — a wrong build wastes a review "
    "cycle, so ask first (see below) rather than guessing at requirements."
    + GUEST_STYLE
)

# One queued summons per chat; the guest speaks once per round at most.
_pending: dict[int, dict] = {}


def _cli_present() -> bool:
    return bool(shutil.which("claude")
                or (Path.home() / ".local/bin/claude").exists())


def status(cfg) -> dict:
    """Availability + why, for /api/state and the engine's tool gating."""
    repos = cfg.get("code_repos") or {}
    writes = bool(cfg.get("code_allow_writes"))
    # use_api_key mirrors run_guest's real billing decision: guests bill the
    # metered API key ONLY when code_use_api_key is set AND a key exists —
    # otherwise they ride the machine's Claude Code login (subscription). It's a
    # config fact independent of availability, so it rides EVERY return (the UI
    # surfaces it to make a silent drift onto metered billing visible).
    use_api_key = bool(cfg.get("code_use_api_key")
                       and os.environ.get("ANTHROPIC_API_KEY"))
    unavailable = {"available": False, "repos": [], "writes": False,
                   "use_api_key": use_api_key}
    try:
        _sdk()
    except Exception:
        return {**unavailable, "reason": "claude-agent-sdk not installed"}
    if not _cli_present():
        return {**unavailable, "reason": "Claude Code CLI not found on this machine"}
    if not repos:
        return {**unavailable, "reason": "no repositories configured (code_repos)"}
    return {"available": True, "repos": sorted(repos), "writes": writes,
            "reason": "", "use_api_key": use_api_key}


def available(cfg) -> bool:
    return status(cfg)["available"]


def _sdk():
    """Import point kept in one place so tests can monkeypatch it."""
    import claude_agent_sdk
    return claude_agent_sdk


# ---------- the summon tool (handler side) ----------

def request(chat_id, args, cfg, requested_by=None) -> str:
    """Queue a guest turn for this chat. Returns the tool result string the
    summoning model sees; the guest actually speaks after the round."""
    if not chat_id:
        return "Error: summoning is only available inside a chat round"
    task = (args.get("task") or "").strip()
    if len(task) < 8:
        return "Error: describe the task for Claude Code in a sentence or two"
    repos = cfg.get("code_repos") or {}
    repo = (args.get("repo") or "").strip()
    if not repo and len(repos) == 1:
        repo = next(iter(repos))
    if repo not in repos:
        return (f"Error: unknown repo {repo!r} — available: "
                f"{', '.join(sorted(repos)) or '(none configured)'}")
    mode = (args.get("mode") or "investigate").strip()
    if mode not in ("investigate", "implement"):
        return "Error: mode must be \"investigate\" or \"implement\""
    # Optional model alias; omission ("" / absent) means "use the config
    # default, else Claude Code's own default". Reject anything outside the
    # constrained set here, at the boundary — nothing arbitrary reaches the SDK.
    model = (args.get("model") or "").strip()
    if model and model not in MODEL_ALIASES:
        return (f"Error: unknown model {model!r} — choose one of: "
                f"{', '.join(MODEL_ALIASES)}")
    # Optional effort/thinking level; same boundary check as model. Omission
    # falls back to the config default, else Claude Code's own default.
    effort = (args.get("effort") or "").strip()
    if effort and effort not in EFFORT_ALIASES:
        return (f"Error: unknown effort {effort!r} — choose one of: "
                f"{', '.join(EFFORT_ALIASES)}")
    # Optional explicit target: a branch/ref name, or a PR number/URL. The HOST
    # fetches and checks the worktree out at that ref's current commit before the
    # guest starts — so a review never depends on the agent self-fetching.
    # Omission keeps the fresh-origin/main default. Validated at the boundary so
    # nothing ambiguous (an option-looking token, embedded whitespace) reaches
    # git; unresolvable refs fail loudly at preflight, never silently on main.
    ref = (args.get("ref") or "").strip()
    if ref:
        if args.get("continue_last"):
            return ("Error: can't combine continue_last with an explicit ref — "
                    "continue_last resumes the previous visit's own checkout "
                    "and working context, so pointing it at a different branch/"
                    "PR would silently mix two contexts. To review a specific "
                    "ref, summon fresh with ref and WITHOUT continue_last.")
        if ref.startswith("-") or any(c.isspace() for c in ref):
            return (f"Error: invalid ref {ref!r} — give a branch/ref name "
                    f"(e.g. cc/155-worktree-isolation), a PR number (e.g. 154), "
                    f"or a PR URL")
    if mode == "implement" and not cfg.get("code_allow_writes"):
        return ("Error: implement mode is disabled — the user must set "
                "code_allow_writes in Crossband's config to let Claude Code "
                "change code and open pull requests. Investigate mode "
                "(read-only) remains available.")
    if chat_id in _pending:
        return ("Claude Code is already summoned for this round — one summons "
                "per round; fold your request into the existing task next time.")
    # One guest job per chat at a time: a guest run is a detached background
    # job, so a summons can land while a PREVIOUS visit is still working. Block
    # it with a clear message rather than queue silently or run two writers: no
    # hidden queue to forget about, and no two guests contending for the same
    # worktree. The simplest safe semantics, and the reasoning is recorded in
    # DECISIONS.md.
    from . import guestjobs
    active = guestjobs.active_for_chat(chat_id)
    if active:
        return (f"Claude Code is already working on an earlier task in this "
                f"chat (\"{active.task_desc[:80]}\") — wait for it to report "
                f"back before summoning it again; only one guest run at a time.")
    _pending[chat_id] = {"task": task, "repo": repo, "mode": mode,
                         "model": model,  # "" = fall back to config/CLI default
                         "effort": effort,  # "" = fall back to config/CLI default
                         "ref": ref,  # "" = fresh origin/main; else host checks it out
                         "continue_last": bool(args.get("continue_last")),
                         "requested_by": requested_by or "unknown",
                         "ts": time.time()}
    doing = ("implement, test, and open a pull request for"
             if mode == "implement" else "work on")
    bits = [b for b in ((f"model: {model}" if model else ""),
                        (f"effort: {effort}" if effort else ""),
                        (f"ref: {ref}" if ref else "")) if b]
    note = f" ({'; '.join(bits)})" if bits else ""
    return (f"Claude Code will join at the end of this round to {doing}: "
            f"{task} (repo: {repo}){note}. Its reply shows the model it "
            f"ACTUALLY ran (verified from its session metadata) plus the effort "
            f"requested. Finish your reply normally — Claude Code speaks after "
            f"everyone else.")


def take(chat_id) -> dict | None:
    """Pop the queued summons (the engine consumes it once per round)."""
    return _pending.pop(chat_id, None)


# ---------- delegation claim state ----------
#
# Coordinate specialist-agent delegation before narrating it. `_pending` (a
# summons queued this round) and `guestjobs.active_for_chat` (a job already
# running from an earlier round) are ALREADY the two places a claim on
# summon_claude_code lives — this just gives the round loop and the system
# prompt ONE place to ask "is this specialist action already spoken for?"
# instead of reconstructing the answer twice. Whoever wins the claim (the
# `_pending`/`active_for_chat` guards inside request()/guestjobs.start() are
# the actual enforcement — this is the read side) invokes immediately; every
# OTHER participant, this round or a later one, reads the same claim and
# neither re-offers the tool nor narrates about it (voice mode included —
# the note reaches the system prompt whether or not the round is spoken).

def claimed(chat_id) -> dict | None:
    """The current delegation claim on summon_claude_code for this chat, or
    None when nothing is claimed. 'queued' = summoned earlier THIS round
    (not yet consumed by the engine); 'running' = a detached guest job from
    an earlier round still working. Checked BEFORE the tool is offered for
    a turn, so a claimed action mechanically can't be duplicated — no
    participant ever sees it as an option to begin with."""
    pending = _pending.get(chat_id)
    if pending:
        return {"state": "queued", "task": pending["task"], "repo": pending["repo"],
                "mode": pending["mode"],
                "requested_by": pending.get("requested_by") or "unknown"}
    from . import guestjobs
    job = guestjobs.active_for_chat(chat_id)
    if job:
        return {"state": "running", "task": job.task_desc, "repo": job.repo,
                "mode": job.mode}
    return None


def delegation_note(claim: dict | None) -> str:
    """System-prompt text for an already-claimed specialist action, or '' when
    nothing is claimed. Read by EVERY participant — the one who didn't win the
    claim included — so nobody spends a turn (spoken or text) proposing,
    debating, or re-announcing a summons someone else already committed to; the
    compact activity indicator (the guest_job status chip) already tells the
    user, on both voice and text/mobile."""
    if not claim:
        return ""
    doing = "implement, test, and open a pull request for" if claim["mode"] == "implement" else "investigate"
    if claim["state"] == "queued":
        return (
            f"Claude Code has ALREADY been summoned this round (by "
            f"{claim['requested_by']}) to {doing}: {claim['task']} "
            f"(repo: {claim['repo']}). It is not offered to you as a tool this "
            "turn — don't ask for it again, and don't spend your reply "
            "narrating that it was summoned; the user already sees that. "
            "Answer anything else normally, or pass with a bare \"…\" if "
            "that was all you had to add."
        )
    return (
        f"Claude Code is ALREADY working in the background on: {claim['task']} "
        f"(repo: {claim['repo']}, {doing} mode) — summoned earlier and still "
        "running. It is not offered to you as a tool this turn. Don't summon "
        "it again, and don't spend your reply discussing whether to or "
        "announcing that it's working — the user already sees its status. "
        "Answer anything else normally, or pass with a bare \"…\" if there's "
        "nothing new to add."
    )


# ---------- worktree isolation ----------
#
# Every guest visit runs in its OWN git worktree, keyed by a unique SESSION id
# (the guest job id) as well as repo+chat — so two sessions NEVER share on-disk
# checkout state. An implement run and a review run, or two overlapping visits,
# each get their own path and each tears down ONLY its own. Two writers on one
# checkout was the first live failure (a guest's `git checkout -b` switched the
# branch under the main session mid-commit); a reviewer landing on the PREVIOUS
# session's leftover worktree — a detached, stale HEAD — and reading pre-PR
# code was the second, caught during a live PR review. The main checkout
# belongs to the live app and deploys; guests get their own copies. Worktrees
# share the repo, remotes and branches, so the guest's branch/push/PR flow is
# unchanged.
#
# Freshness: the worktree is (re)created detached at the FRESHEST main — the
# fetched origin/main when a remote has it, else local main — after a
# best-effort `git fetch`. A stale local `main` (the deploy checkout only
# advances when the human pulls) is exactly what made the reviewer read pre-PR
# code; fetching first also lands the PR/branch refs a review session then
# checks out, so it sees the ref's ACTUAL current commit.
#
# continue_last resume rides on the session_key: a resumed visit is handed the
# prior visit's key, so it derives the SAME cwd and the CLI finds that session's
# transcript (which lives under ~/.claude keyed by cwd, not in the worktree).

WORKTREE_LINKS = [".venv", "frontend/node_modules"]  # heavy, uncommitted deps
_FETCH_TIMEOUT_S = 20  # best-effort refresh; never let a slow remote wedge start


# The temp-dir namespace guest worktrees own. Named once because
# _force_clear_leftover's safety guard is exactly "is this path inside the
# namespace we hand out" — a scope that must not drift from the paths
# _worktree_path actually creates.
_WT_PREFIX = "mmc-guest-"


def _worktree_path(repo_key, chat_id, session_key=None) -> Path:
    """Where a visit's worktree lives. Keyed by repo+chat and — when a
    session_key is given — by that id too, so concurrent/independent sessions
    get DISTINCT paths and clean up only their own. session_key=None yields the
    bare per-chat path (the resume-stable 'home', and the shape the direct unit
    tests assert)."""
    import tempfile
    base = Path(tempfile.gettempdir()) / f"{_WT_PREFIX}{repo_key}-chat{chat_id}"
    if session_key in (None, ""):
        return base
    return base.with_name(f"{base.name}-{session_key}")


def _git(repo, *args, timeout=60):
    import subprocess
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, timeout=timeout)


def _remove_worktree(repo: Path, dest: Path):
    """Best-effort teardown of ONE worktree — scoped to exactly `dest`, so a
    concurrent session's worktree is never touched. Pushed branches survive."""
    try:
        for link in WORKTREE_LINKS:  # never let remove chase the symlinks
            tgt = dest / link
            if tgt.is_symlink():
                tgt.unlink()
        _git(repo, "worktree", "remove", "--force", str(dest))
        _git(repo, "worktree", "prune")
    except Exception:
        log.warning("guest worktree cleanup failed for %s", dest)


def _force_clear_leftover(dest: Path):
    """Delete a leftover guest-worktree DIRECTORY that git will not remove.

    `git worktree remove` only removes a path this repo still has REGISTERED as
    one of its worktrees. A directory whose registration is gone — teardown
    abandoned mid-way at the GUEST_TEARDOWN_S budget, the repo re-cloned or
    moved, a manual `worktree prune` — survives both remove and prune, and
    `worktree add` then refuses the non-empty path. That wedges the repo+chat
    permanently: every later visit fails with "could not join" until a human
    deletes the directory by hand. Deleting it here is what makes run_guest's
    teardown comment true ("the next visit's _add_worktree prunes any leftover
    worktree").

    Scoped to the namespace _worktree_path hands out, and refuses anything
    else — so an upstream bug can never turn this into an arbitrary rmtree."""
    import tempfile
    if dest.parent != Path(tempfile.gettempdir()) or not dest.name.startswith(_WT_PREFIX):
        log.warning("refusing to clear unexpected guest worktree path %s", dest)
        return
    shutil.rmtree(dest, ignore_errors=True)  # a symlink/permission failure
    # falls through to `worktree add`, which then fails with git's own message.


def _sweep_stale_siblings(repo: Path, repo_key, chat_id):
    """Reclaim leftover worktrees for THIS repo+chat from crashed prior visits.
    Per-session paths are never reused, so a hard-killed visit (teardown in
    `finally` skipped) would otherwise leak its directory forever. Safe because
    guest runs are serialized per chat: when a new visit starts, no other
    worktree for the same repo+chat is live — teardown of the prior visit
    completes before its job leaves 'running' — so every sibling on disk belongs
    to a dead session. Other chats' worktrees carry a different chat id and are
    left untouched, so a session running elsewhere is unaffected."""
    import tempfile
    prefix = f"{_WT_PREFIX}{repo_key}-chat{chat_id}"
    tmp = Path(tempfile.gettempdir())
    try:
        siblings = list(tmp.glob(prefix + "*"))
    except Exception:
        siblings = []
    for p in siblings:
        # the exact per-chat home OR a "<home>-<session>" sibling — the '-'
        # separator / exact-name check keeps chat1 from matching chat10.
        if p.name == prefix or p.name.startswith(prefix + "-"):
            _remove_worktree(repo, p)
    _git(repo, "worktree", "prune")


def _fresh_base(repo: Path) -> str:
    """The freshest main to branch from: fetched origin/main when a remote has
    it, else local main. A stale local main is what made a reviewer read pre-PR
    code. Fetch is best-effort and time-bounded — offline dev still works, it
    just bases on local main."""
    _git(repo, "fetch", "--prune", "origin", timeout=_FETCH_TIMEOUT_S)  # best-effort
    probe = _git(repo, "rev-parse", "--verify", "--quiet", "origin/main")
    return "origin/main" if probe.returncode == 0 else "main"


# A PR reference the caller may pass instead of a branch: a full/relative pull
# URL (…/pull/154) or a bare/# -prefixed number. Everything else is treated as a
# branch / tag / ref / SHA.
_PR_URL_RE = re.compile(r"/pull/(\d+)")


def _parse_pr(ref: str):
    """The PR number in `ref` (URL or #N / N), else None (→ treat as a ref)."""
    m = _PR_URL_RE.search(ref)
    if m:
        return int(m.group(1))
    bare = ref.lstrip("#")
    return int(bare) if bare.isdigit() else None


def _unique_resolve_ref(session_key) -> str:
    """A private, per-resolution local ref name to fetch a target INTO. Keyed by
    the job/session id AND a random token, so two concurrent resolutions in the
    same primary repo never collide and each deletes only its OWN ref."""
    import uuid
    safe = re.sub(r"[^A-Za-z0-9_.-]", "-", str(session_key or "anon"))
    return f"refs/mmc-guest/resolve/{safe}-{uuid.uuid4().hex[:12]}"


def _resolve_ref(repo: Path, ref: str, session_key=None) -> tuple[str, str]:
    """Resolve an explicit target — a branch/tag/ref/SHA or a PR number/URL — to
    the CONCRETE commit the worktree should be detached at, fetching from origin
    first so it reflects the target's ACTUAL current head (incl. unmerged PR
    content). Returns (commit_sha, human_label). Raises RuntimeError with a clear
    message if it can't be resolved — the host NEVER silently falls back to
    main. The fetch uses origin's own credentials; no token flows through here.

    Concurrency: the fetch lands the target in a UNIQUE, job-keyed private ref
    (refs/mmc-guest/resolve/…) and we resolve THAT, never the shared FETCH_HEAD —
    so two jobs in different chats resolving different refs against the same
    primary repo can't read each other's result (the FETCH_HEAD race). The temp
    ref is deleted in `finally`, and only ever this call's own ref."""
    if ref.startswith("-"):  # defence in depth against git-option injection
        raise RuntimeError(f"invalid ref {ref!r}")
    pr = _parse_pr(ref)
    # GitHub exposes each PR head at refs/pull/<N>/head — a plain refspec fetch,
    # so PRs work for private repos too and need no gh/token.
    src = f"pull/{pr}/head" if pr is not None else ref
    label = f"PR #{pr}" if pr is not None else f"ref {ref}"
    tmp = _unique_resolve_ref(session_key)
    try:
        # "<src>:<tmp>" writes the fetched tip into OUR private ref, bypassing
        # the process-wide FETCH_HEAD entirely.
        r = _git(repo, "fetch", "--force", "origin", f"{src}:{tmp}",
                 timeout=_FETCH_TIMEOUT_S)
        if r.returncode == 0:
            sha = _git(repo, "rev-parse", "--verify", "--quiet", tmp).stdout.strip()
            if sha:
                return sha, label
        if pr is not None:  # a PR ref must resolve remotely — no local fallback
            raise RuntimeError(
                f"could not fetch PR #{pr} from origin — check it exists and is "
                f"pushed ({r.stderr.strip()[:160]})")
        # A branch/tag/SHA may already be present locally (e.g. a full SHA the
        # server won't serve by-id). rev-parse of a fixed ref is race-free.
        probe = _git(repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
        if probe.returncode == 0 and probe.stdout.strip():
            return probe.stdout.strip(), label
        raise RuntimeError(
            f"could not resolve target ref {ref!r} against origin or locally — "
            f"check the branch/PR exists and is pushed (not falling back to main)")
    finally:
        # Delete ONLY our uniquely-named temp ref — never another job's ref or a
        # real branch. A no-op if the fetch never created it.
        _git(repo, "update-ref", "-d", tmp)


def _add_worktree(repo: Path, repo_key, chat_id, session_key=None,
                  base=None) -> Path:
    """Create this visit's isolated worktree, detached at `base` (a commit-ish).
    Sweeps stale same-repo+chat leftovers first, then adds a clean checkout at
    its OWN session-keyed path. base=None means the freshest main (the default
    for a summon with no explicit ref). Raises RuntimeError with the git error if
    the repo can't do worktrees."""
    _sweep_stale_siblings(repo, repo_key, chat_id)
    dest = _worktree_path(repo_key, chat_id, session_key)
    if dest.exists():  # a leftover the sweep couldn't remove — force it gone
        _git(repo, "worktree", "remove", "--force", str(dest))
        _git(repo, "worktree", "prune")
    if dest.exists():  # still there → git doesn't own it; clear the directory
        _force_clear_leftover(dest)
    if base is None:
        base = _fresh_base(repo)
    r = _git(repo, "worktree", "add", "--detach", str(dest), base)
    if r.returncode != 0:
        raise RuntimeError(f"could not create guest worktree: "
                           f"{r.stderr.strip()[:200]}")
    for link in WORKTREE_LINKS:
        src, tgt = repo / link, dest / link
        if src.exists() and not tgt.exists():
            tgt.symlink_to(src)
    return dest


# ---------- the guest turn ----------

def _result_text(content) -> str:
    """Normalize a ToolResultBlock's content (str | list of blocks) to text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(p.get("text", "") for p in content
                         if isinstance(p, dict))
    return ""


def build_prompt(task, context, mode="investigate", resumed=False) -> str:
    opening = ("The group chat summoned you again — continue from your "
               "previous visit." if resumed else
               "The group chat summoned you for this task:")
    parts = [f"{opening}\n\n{task}"]
    if context:
        parts.append("Recent conversation, for context:\n" + context)
    parts.append("Implement, ship the pull request, and reply to the group."
                 if mode == "implement" else "Investigate and reply to the group.")
    return "\n\n".join(parts)


async def run_guest(task, repo_key, context, cfg, mode="investigate",
                    resume=None, chat_id=0, model="", effort="",
                    session_key=None, ref=""):
    """Async generator yielding the same event vocabulary as
    providers.stream_reply — ('text', delta), ('tool', {...}), ('usage', {...})
    — so the engine streams and persists a guest turn exactly like any other
    speaker's. mode selects the loadout (read-only vs branch/test/push/PR);
    resume continues a previous visit's session by id. Every visit runs in its
    OWN session-keyed git worktree (see the worktree isolation notes above);
    session_key isolates concurrent sessions and, on resume, reuses the prior
    visit's key so the cwd (and thus the CLI's session lookup) matches. `ref`
    (branch/ref or PR number/URL) is resolved and checked out BY THE HOST before
    the guest starts, so a review sees the target's actual files without needing
    git/Bash itself."""
    sdk = _sdk()
    repos = cfg.get("code_repos") or {}
    path = Path(repos[repo_key]).expanduser()
    if not path.is_dir():
        yield ("text", f"[Claude Code could not join: configured path for "
                       f"\"{repo_key}\" does not exist: {path}]")
        return
    ref = (ref or "").strip()
    ref_label = ""
    resolved_base = None
    try:
        if ref:
            # Host-owned checkout: resolve to the ref's current commit (fetching
            # first) — a bad/unpushed ref fails HERE, loudly, never on main. The
            # session_key keys a private resolve ref so concurrent resolutions in
            # the same repo don't cross (the FETCH_HEAD race).
            resolved_base, ref_label = await asyncio.to_thread(
                _resolve_ref, path, ref, session_key)
        work_path = await asyncio.to_thread(
            _add_worktree, path, repo_key, chat_id, session_key, resolved_base)
    except Exception as e:
        yield ("text", f"[Claude Code could not join: {e}]")
        return

    implement = mode == "implement" and bool(cfg.get("code_allow_writes"))
    if implement:
        loadout = list(IMPLEMENT_TOOLS)
        allowed = list(IMPLEMENT_ALLOWED)
        denied = list(IMPLEMENT_DENIED)
        system = GUEST_SYSTEM_IMPLEMENT
        max_turns = int(cfg.get("code_impl_max_turns", 150))
        timeout_s = float(cfg.get("code_impl_timeout_s", 1800))
    else:
        loadout = list(READ_ONLY_TOOLS)
        allowed = list(READ_ONLY_TOOLS)
        denied = list(DENIED_TOOLS)
        system = GUEST_SYSTEM
        max_turns = int(cfg.get("code_max_turns", 50))
        timeout_s = float(cfg.get("code_timeout_s", 600))

    # Model precedence: per-summon choice > config default > CLI default.
    # A blank/absent alias at each level falls through to the next; "default"
    # and unknown values resolve to None (omit the override). Model choice is
    # independent of the auth path built above — it never touches env.
    alias = (model or "").strip() or (cfg.get("code_model") or "").strip() \
        or "default"
    model_override = resolve_model(alias)
    effective_alias = alias if model_override else "default"

    # Effort/thinking level, same precedence: per-summon > config default >
    # CLI default. A resolved budget is injected via MAX_THINKING_TOKENS (set in
    # env_overrides below); "default"/unknown omits it (Claude Code's own
    # behavior). effort_alias is what we report as the level that ran.
    effort_in = (effort or "").strip() or (cfg.get("code_effort") or "").strip() \
        or "default"
    thinking_tokens = resolve_effort(effort_in)
    effort_alias = effort_in if thinking_tokens else "default"

    # get_diagnostic: a scoped, read-only, content-free diagnostics tool
    # mounted for EVERY guest visit, in both modes — unlike code_mcp below,
    # this is not operator opt-in, because it carries no secret and reaches no
    # network beyond this same process's own in-memory/db state (see
    # backend/diag_mcp.py's module docstring for why that makes it safe to
    # always-mount). Built fresh per visit, closing over this visit's cfg — with
    # chat_id threaded in the same way engine.py threads it into round_cfg for
    # the native tool surface, so a guest's own conversation_spend call is
    # scoped to the chat that summoned it instead of always reporting "no
    # active conversation". A copy, not a mutation of the caller's cfg, since
    # cfg is read again below (code_mcp, user_name).
    diag_cfg = {**cfg, "chat_id": chat_id}
    mcp_servers = {diag_mcp.SERVER_NAME: diag_mcp.build_server(diag_cfg)}
    allowed.append(f"mcp__{diag_mcp.SERVER_NAME}")

    for name, server in (cfg.get("code_mcp") or {}).items():
        # ${VAR} in a server's env is resolved from Crossband's environment, so
        # an authenticated read MCP (e.g. membro-admin) can reference its token
        # by name instead of hardcoding it in config.
        if server.get("env"):
            server = {**server, "env": _interpolate_env(server["env"])}
        mcp_servers[name] = server
        allowed.append(f"mcp__{name}")  # whole-server allow (read-side tools)

    # The guest must authenticate like a fresh `claude` in a clean terminal —
    # through the user's own local login. Blank every inherited CLAUDE*/
    # ANTHROPIC* variable: a backend launched from inside a Claude Code
    # session inherits that session's state (which breaks the child's OAuth),
    # and the API keys sourced from .env would silently move the guest off
    # the user's subscription onto metered billing. Blank ("") reads as unset
    # to the CLI; the SDK merges these OVER the inherited process env.
    # CLAUDE_CODE_OAUTH_TOKEN survives — it's the supported headless
    # credential (`claude setup-token`), and .env is exactly where it lives.
    env_overrides = {k: "" for k in os.environ
                     if k.startswith(("CLAUDE", "ANTHROPIC"))
                     and k != "CLAUDE_CODE_OAUTH_TOKEN"}
    # code_use_api_key opts guest turns onto the metered API key instead of
    # the machine's Claude Code login — an explicit, disclosed choice (the
    # real cost lands on every guest message), never an accident.
    used_api_key = bool(cfg.get("code_use_api_key")
                        and os.environ.get("ANTHROPIC_API_KEY"))
    if used_api_key:
        env_overrides["ANTHROPIC_API_KEY"] = os.environ["ANTHROPIC_API_KEY"]
    # Effort → thinking budget: a plain integer the CLI reads structurally, no
    # shell, no interpolation. Omitted entirely on "default" so the guest keeps
    # Claude Code's own thinking behavior.
    if thinking_tokens:
        env_overrides["MAX_THINKING_TOKENS"] = str(thinking_tokens)

    # When the host checked the worktree out at an explicit target, TELL the
    # guest — so a read-only reviewer knows its cwd already IS that ref and never
    # tries (and, in investigate mode, fails) to fetch/switch to it itself.
    system_prompt = system.format(
        user_name=cfg.get("user_name", "the user"), repo=repo_key,
        path=work_path)
    if ref_label:
        system_prompt += (
            f"\n\nThis worktree is already checked out (detached) at "
            f"{ref_label} — the exact commit you were asked to look at, fetched "
            f"for you by the host. The files in your working directory ARE that "
            f"target; you do NOT need to fetch or switch branches to see it.")

    options = sdk.ClaudeAgentOptions(
        env=env_overrides,
        cwd=str(work_path),
        system_prompt=system_prompt,
        tools=loadout,                      # loadout: only these built-ins exist
        allowed_tools=allowed,              # and they run without prompting
        disallowed_tools=denied,
        # Headless: nobody can answer a permission prompt, so pre-approved
        # commands run and everything else is DENIED rather than left hanging
        # (the read/wait that stalled earlier guest sessions).
        permission_mode="dontAsk",
        # Inject the guest's whole permission story from THIS process, not the
        # Mac's global ~/.claude/settings.json — every summoned worktree gets
        # the same allow/deny loadout regardless of the operator's personal
        # settings. "project" is kept so the repo's CLAUDE.md still loads.
        setting_sources=["project"],
        max_turns=max_turns,
        mcp_servers=mcp_servers,
        model=model_override,               # None omits the override (CLI default)
        include_partial_messages=True,      # token-level deltas for the SSE stream
        resume=resume,                      # continue a previous visit's session
    )

    deadline = time.monotonic() + timeout_s
    tools_in_flight: dict[str, dict] = {}
    usage = None
    # Ground truth, straight from Claude Code's own session — NOT the caller's
    # requested tier. AssistantMessage.model is the concrete model that produced
    # the turn (e.g. "claude-sonnet-4-5-…"); ResultMessage.model_usage keys are
    # the model(s) the session actually billed. Either resolves "default" (and
    # any resolved-elsewhere tier) to the real model that ran.
    verified_model = None
    verified_usage_models: list[str] = []
    it = sdk.query(prompt=build_prompt(task, context, mode, resumed=bool(resume)),
                   options=options).__aiter__()
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                yield ("text", "\n\n[Claude Code hit its time limit and "
                               "was cut off]")
                break
            try:
                msg = await asyncio.wait_for(it.__anext__(), timeout=remaining)
            except StopAsyncIteration:
                break
            except asyncio.TimeoutError:
                yield ("text", "\n\n[Claude Code hit its time limit and "
                               "was cut off]")
                break

            if isinstance(msg, sdk.StreamEvent):
                if msg.parent_tool_use_id:  # subagent internals stay internal
                    continue
                ev = msg.event or {}
                delta = ev.get("delta") or {}
                if (ev.get("type") == "content_block_delta"
                        and delta.get("type") == "text_delta"):
                    yield ("text", delta.get("text", ""))
            elif isinstance(msg, sdk.AssistantMessage):
                if msg.parent_tool_use_id:
                    continue
                if getattr(msg, "model", None):
                    verified_model = msg.model  # concrete id, last turn wins
                for block in msg.content:
                    if isinstance(block, sdk.ToolUseBlock):
                        tools_in_flight[block.id] = {
                            "tool": block.name, "input": block.input, "output": ""}
            elif isinstance(msg, sdk.UserMessage):
                content = msg.content if isinstance(msg.content, list) else []
                for block in content:
                    if isinstance(block, sdk.ToolResultBlock):
                        rec = tools_in_flight.pop(block.tool_use_id, None)
                        if rec:
                            rec["output"] = _result_text(block.content)[:2000]
                            yield ("tool", rec)
            elif isinstance(msg, sdk.ResultMessage):
                u = msg.usage or {}
                if getattr(msg, "model_usage", None):
                    verified_usage_models = list(msg.model_usage.keys())
                usage = {
                    "input": u.get("input_tokens", 0),
                    "output": u.get("output_tokens", 0),
                    "cache_read": u.get("cache_read_input_tokens", 0),
                    "cache_creation": u.get("cache_creation_input_tokens", 0),
                    "cost": msg.total_cost_usd,
                    # Billing provenance recorded AT TURN TIME so the cost
                    # surfaces never guess: "api_key" = this turn ran on the
                    # metered ANTHROPIC_API_KEY (real cash); "subscription" =
                    # it ran on a subscription or OAuth login instead of that
                    # key, so total_cost_usd is an API-equivalent estimate
                    # covered by that subscription, not incremental spend.
                    "auth": "api_key" if used_api_key else "subscription",
                    # Cost provenance recorded AT TURN TIME, immutable to later
                    # config changes. A metered API-key turn's total_cost_usd
                    # is a provider_reported (billed) figure; a subscription
                    # turn's is subscription_equivalent (informational, never
                    # incremental cash).
                    "cost_provenance": provenance.record(
                        provenance.PROVIDER_REPORTED if used_api_key
                        else provenance.SUBSCRIPTION_EQUIVALENT),
                    # provenance + the resume handle (continue_last re-enters
                    # this session, in this repo)
                    "session_id": msg.session_id,
                    "repo": repo_key,
                    # The worktree the session ran in, so a later continue_last
                    # reuses the SAME cwd — the CLI's session lookup is keyed by
                    # working directory. None on a direct/legacy call.
                    "worktree_key": session_key,
                    # Audit trail for the host-owned checkout: what target was
                    # requested and the concrete commit it resolved to. Both
                    # are public repo refs — no secrets. "" / None when the
                    # visit used the default fresh origin/main.
                    "target_ref": ref,
                    "target_commit": resolved_base or "",
                    # What was REQUESTED, not what's verified: model_alias is
                    # the tier asked for; effort_alias is the thinking level we
                    # APPLIED (via MAX_THINKING_TOKENS) — the SDK never reports
                    # thinking-token usage back, so effort is not read-back
                    # verified the way verified_model (below) is. "default" =
                    # Claude Code's own default; no override was sent.
                    "model_alias": effective_alias,
                    "effort_alias": effort_alias,
                }
    finally:
        # Bounded teardown: a wedged Claude Code subprocess must never wedge
        # the round. Both closing the SDK iterator AND removing the worktree
        # can block on that subprocess (or a stuck `git`), so cap the whole
        # teardown at GUEST_TEARDOWN_S. If a step overruns we abandon it —
        # the orphaned transport dies on its own and the next visit's
        # _add_worktree prunes any leftover worktree — and let the round finish
        # so the running indicator clears. (asyncio.CancelledError is a
        # BaseException, so it is NOT caught here: a fresh cancellation still
        # propagates up to run_round's own persist-and-reraise path.)
        teardown_deadline = time.monotonic() + GUEST_TEARDOWN_S
        aclose = getattr(it, "aclose", None)
        if aclose:
            try:
                await asyncio.wait_for(
                    aclose(),
                    timeout=max(0.1, teardown_deadline - time.monotonic()))
            except (asyncio.TimeoutError, Exception):
                log.warning("Claude Code iterator teardown overran %.1fs "
                            "(chat %s) — abandoning it", GUEST_TEARDOWN_S,
                            chat_id)
        # teardown AFTER the CLI is closed; pushed branches survive removal
        try:
            await asyncio.wait_for(
                asyncio.to_thread(_remove_worktree, path, work_path),
                timeout=max(0.1, teardown_deadline - time.monotonic()))
        except (asyncio.TimeoutError, Exception):
            log.warning("guest worktree cleanup overran %.1fs (chat %s) — "
                        "leaving it for the next visit to prune",
                        GUEST_TEARDOWN_S, chat_id)
    for rec in tools_in_flight.values():  # calls that never got a result back
        yield ("tool", rec)

    # Run readout, appended to the reply so it's always visible in-chat (not
    # hover-gated) and written HERE by the harness — the guest LLM never writes
    # it, so it can't be misreported. Two DIFFERENT provenances, labelled as
    # such so we don't overclaim:
    #   - MODEL is *verified* — read back from Claude Code's own session
    #     (AssistantMessage.model, else ResultMessage.model_usage), so it
    #     resolves a requested "default" to the concrete model that actually ran.
    #   - EFFORT is only *requested/applied* — we set MAX_THINKING_TOKENS, but
    #     the SDK doesn't expose thinking-token usage in ResultMessage, so
    #     nothing reads the budget back out to confirm it. Calling effort
    #     "verified" would be an overclaim.
    # usage carries the machine-readable fields for later accounting.
    concrete = verified_model or (verified_usage_models[0]
                                  if verified_usage_models else None)
    if usage is not None:
        usage["verified_model"] = concrete or ""
        # `model` is the field the message header renders — show the concrete
        # verified model that ran, falling back to the requested tier.
        usage["model"] = concrete or effective_alias
    if usage is not None and (concrete or effort_alias != "default"
                              or effective_alias != "default"):
        req = (effective_alias if effective_alias != "default"
               else "default (Claude Code's own)")
        ran = (f"ran on `{concrete}` (verified from Claude Code's session)"
               if concrete else
               "ran on its default model (session reported none)")
        effort_part = ("effort **default** (Claude Code's own)"
                       if effort_alias == "default"
                       else f"effort **{effort_alias}** (requested — applied via "
                            f"the thinking budget, not read back from the session)")
        footer = f"\n\n—\n*Requested tier **{req}** — {ran}; {effort_part}.*"
        yield ("text", footer)
    if usage:
        yield ("usage", usage)
