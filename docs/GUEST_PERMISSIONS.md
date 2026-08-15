# Guest permissions: what a summoned Claude Code guest may and may not do

When you summon Claude Code into a chat (the `summon_claude_code` tool), it
runs **headless**: there is no terminal and no human to click "approve" on a
permission prompt. So the guest's abilities are decided entirely up front,
in code, and injected into every summoned session. The guest never works in
your checkout either: each visit runs in its own throwaway **git worktree**
(one per repo + chat), so it can't collide with you or with another chat's
visit. This page explains that in plain English. Read it before changing
`backend/guest.py`'s allow/deny lists.

## What each mode actually stops

The asymmetry that catches people out is credential files. The `Read` deny
rules exist in implement mode and nowhere else.

| | investigate (default) | implement (`code_allow_writes`) |
|---|---|---|
| Change your repo | no `Write`, `Edit` or `Bash` at all | yes, on a branch; never merges, never pushes `main` |
| `Read` of `.env`, `config.local.json`, `*.pem`, `id_rsa*` | **not blocked**; nothing in the loadout restricts `Read` by path | denied by rule, on the CLI's deny-over-allow precedence |
| `Grep` / `Glob` over those files | **not blocked** | **not blocked**; the rules name `Read` only |
| `env` / `printenv` / `curl` / `wget` | blocked, there is no `Bash` | blocked by name |
| MCP servers in `code_mcp` | mounted whole, write tools included | mounted whole, write tools included |

So a read-only guest is read-only about **your repo**, not about **your
secrets**. What limits the damage in practice is reach rather than rules: each
visit runs in a throwaway worktree at a fresh checkout, and gitignored files
such as `.env` are not in a fresh checkout. That is a property of where the
guest is standing, not a permission.

"Credential files" below has the detail and what to change if you want the
rules in both modes.

## The two modes

- **Investigate (default, read-only).** *Built-in* tools are limited to
  `Read`, `Grep`, `Glob`. No shell, no file writes: it looks and reasons, and
  changes nothing in the repo. It can still open any file it can name, as the
  table above shows. MCP servers listed in `code_mcp` are mounted in **both**
  modes and each is allowed whole (`mcp__<name>`), write-side tools included.
  Membro's `membro` recall server offers `save_memory`, a fact proposal held
  for your review, and the authenticated `membro-admin` exact-fact reader if
  you wire it up.
- **`get_diagnostic` (always mounted, both modes, not a `code_mcp` entry).**
  Every guest visit, with no config needed, also gets one in-process MCP tool,
  `get_diagnostic(name)`, where `name` is a closed enum (`health`, `models`,
  `voice_latency`, `conversation_spend`, `conversation_performance`; see
  `backend/diag_mcp.py`), never a URL, path, or free query. It answers "is the
  memory service reachable", "what model is each participant actually running",
  "recent voice-turn count + content-free latency percentiles", and "this
  conversation's running metered cash total with a dynamic
  party/producer/provider breakdown" from this same process's own data:
  loopback-only, read-only, and unable to return a transcript, message text, a
  credential, or any log line. It is always-on (unlike the opt-in servers
  below) because it carries no secret and reaches no network beyond this
  process. Claude and GPT also get this exact diagnostic natively in normal
  chat (`get_diagnostic` in `backend/tools.py`), sharing the same
  allowlist/schema/dispatch from `backend/diagnostics.py`, so a plain-language
  diagnostics question no longer needs a guest summon at all; a guest visit
  still carries its own copy for when a guest is summoned for other reasons.
- **Implement (opt-in, `code_allow_writes`).** The guest branches, edits, runs
  tests, commits, pushes a branch, and opens a pull request. It **never merges**
  and **never pushes to `main`**. The human reviews and merges every PR.

**What a summoner can vary.** Besides the mode, a summons may name a **model**
tier (`default`/`opus`/`sonnet`/`haiku`) and an **effort** level
(`default`/`think`/`think-hard`/`ultrathink`). Both are fixed alias sets
checked at the tool boundary, so nothing free-form ever reaches the SDK, and
each layers per-summon choice over the `code_model`/`code_effort` config
default over Claude Code's own default. A guest reply ends with a readout of
both, labelled by how well each is known: the **model** is read back from
Claude Code's own session, so it names what actually ran rather than the tier
that was asked for; the **effort** is only what was requested and applied (as
a thinking budget), because the SDK never reports thinking tokens back, so nothing
confirms it. See `README.md` for how to ask for them.

## How permission works (headless = deny-by-default)

Every guest session sets `permission_mode="dontAsk"`. That means:

> A command runs **only** if it is explicitly pre-approved. Anything else is
> denied immediately, not queued for approval, not run.

Because there is no human in the loop, "ask" is not an option; the only choices
are *pre-approved* or *denied*. This is why the pre-approved list has to name the
project's **actual** commands. A rule is a command **prefix**: `Bash(git
push:*)` auto-runs anything starting with `git push`. `disallowed_tools`
winning over `allowed_tools` is Claude Code's own behaviour, which this
repository relies on but does not verify: every guest test mocks the SDK
boundary, so the suite pins which rules are handed over, never that the CLI
refused a call.

Permissions are injected from the running Crossband process, **not** from the
operator's personal `~/.claude/settings.json`. The SDK is told
`setting_sources=["project"]`, meaning read only the repo's own settings and
never your personal `~/.claude` ones, so every summoned worktree gets the same loadout
on any machine; the repo's `CLAUDE.md` still loads (that is the `"project"`
source).

## What implement mode may do (auto-approved)

| Class | Examples |
|-------|----------|
| Run the test suite | `env -u OPENAI_API_KEY -u ANTHROPIC_API_KEY .venv/bin/python -m pytest tests/ -q`, `.venv/bin/pytest`, `npm test`, `npm run build`, `npm ci`, `npm --prefix frontend test`/`run`/`ci` |
| Normal git flow | `git checkout -b …`, `git switch`/`branch`/`restore`/`stash`, `git add`, `git commit`, `git push` (feature branches), `git fetch`, `git ls-remote`, `git status`/`diff`/`log`/`show`/`rev-parse`, `git remote -v`/`get-url` |
| Read issues, open PRs | `gh issue view`/`list`/`comment`, `gh pr create`/`view`/`diff`/`checks`/`list`/`status`, `gh auth status` |
| Read-only DB diagnostics | `sqlite3 -readonly <db> "SELECT …"`, available on every implement-mode visit; the guest uses it when it needs to inspect live data |
| Files and task list | `Read`, `Grep`, `Glob`, `Edit`, `Write` (except under `.github/`), plus `TodoWrite` for the guest's own checklist |

> The keyless test command from `CLAUDE.md` starts with `env …`; both key
> orderings are pre-approved. `env -u NAME` only *unsets* a variable before the
> pinned interpreter, so the rule stays as narrow as `.venv/bin/python` itself.

## What implement mode blocks (denied)

These are `IMPLEMENT_DENIED` in `backend/guest.py`. Everything in this list
applies to implement mode; the final bullet is the one that also holds in
investigate mode, and it says so. Read the next section before assuming any of
the others cover a read-only visit.

- **Merging / main:** `gh pr merge`, `git merge`, `git push origin main` (and
  `-u origin main`, `HEAD:main`), force-push (`git push -f`/`--force`/
  `--force-with-lease`), `gh repo …`.
- **Its own gate:** editing `.github/**` (a PR must not be able to weaken the CI
  that reviews it; propose CI changes in the reply instead).
- **Secrets / exfiltration:** reading `.env`, `.env.*`, `config.local.json`,
  `*.pem`, `id_rsa*`; dumping the environment (`env`, `printenv`); network
  egress outside git/`gh` (`curl`, `wget`, `nc`, `ssh`, `scp`). **Implement
  mode only**, see below.
- **Destructive shell:** `rm -rf`/`rm -fr`, `sudo`, `git clean`.
- **No web, no subagents:** `WebFetch`, `WebSearch`, `Task` (spawning
  subagents), `NotebookEdit` and `KillShell` are switched off entirely, in
  both modes. So it is not only `curl` that is blocked: the guest has no web
  search and no web fetch at all. The network reach left to it is the
  deliberate kind: `git` and `gh`, plus `npm` fetching packages when it
  installs or builds. (Investigate mode denies `TodoWrite` as well, since it has
  nothing to build, so it has nothing to track.)

Read-only SQLite is enforced by pre-approving **only** the `-readonly`/`-ro`
form. A bare `sqlite3 <db>` (which can write) is never approved. Residual
caveat: SQLite's `-readonly` still allows meta dot-commands such as `.shell` and
`.import`, which are out of scope for the automatic allow and rely on the
human PR review (these rules are guardrails against accidents, not a sandbox).

The same caveat applies to the pinned interpreter: `.venv/bin/python …` is
pre-approved, so any Python the guest writes auto-runs. A rule checks the
**shape** of a command, never what the command goes on to do, which is why
the real gate is the merge, not this list. Make that gate enforced rather than
conventional: protect `main` on your host so the test and audit checks must
pass before anything the guest wrote can land, and block force-pushes and
branch deletion outright. Check who the protection actually binds while you
are there, since these settings commonly exempt the repository owner while
still applying to anyone pushing as a collaborator, which is the account a
guest pushes under.

## Credential files: where the rules actually live

The table at the top of this page states the asymmetry. This is where it
comes from in the code.

`Read(.env)`, `Read(**/.env)`, `Read(**/.env.*)`,
`Read(**/config.local.json)`, `Read(**/*.pem)` and `Read(**/id_rsa*)` live in
**`IMPLEMENT_DENIED` and nowhere else**. Investigate mode's deny list is
`DENIED_TOOLS`, which names whole tools (`Bash`, `Write`, `Edit`,
`NotebookEdit`, `WebFetch`, `WebSearch`, `Task`, `TodoWrite`, `KillShell`)
and contains **no `Read` rule of any kind**. Its allow list is the bare
`Read`, `Grep`, `Glob`.

Two limits on what those rules buy, even in implement mode. They bind `Read`
alone, so a `Grep` over a credential file is not covered. And no test here
watches a read being refused: the suite pins which rules are handed to Claude
Code, and relies on the CLI's documented deny-over-allow precedence.

If you need the file rules in both modes, add them to `DENIED_TOOLS` as well
as `IMPLEMENT_DENIED`, and update this page and `SECURITY.md` in the same
change. `tests/test_guest.py` pins the current behaviour of both modes, and
separately pins that the rule list quoted above matches the one in
`SECURITY.md` and both match the code, so that edit turns a test red until
the documentation catches up.

## Credentials are a separate layer

These rules fix the **approval** gate, not the **credential** gate. `git push`
over an HTTPS remote and `gh` both need a GitHub credential. If the host blocks
Keychain access (common when Crossband runs in a restricted environment), they
fail with a TLS error (`OSStatus -26276`) **no matter how the permissions are
set**.

**One-time operator fix** (either one):

1. Export a GitHub token in Crossband's environment (e.g. add `GH_TOKEN=…` to
   `.env`). The guest inherits `GH_TOKEN`/`GITHUB_TOKEN`, which are *not*
   blanked, unlike `CLAUDE*`/`ANTHROPIC*`, so `git` and `gh` authenticate
   without touching the Keychain. `gh auth login` with `--with-token` also
   works.
2. Or switch the repo remote to SSH: `git remote set-url origin
   git@github.com:<owner>/<repo>.git`, so push uses your SSH key/agent.

The model login (`CLAUDE_CODE_OAUTH_TOKEN`) authenticates the guest to Claude;
it does **not** authenticate to GitHub.

**Two exceptions to the `CLAUDE*`/`ANTHROPIC*` blanking**, both deliberate:

- `CLAUDE_CODE_OAUTH_TOKEN` survives. It is the supported headless model login
  (`claude setup-token`) and `.env` is exactly where it lives; blanking it
  would leave the guest unable to authenticate to Claude at all.
- If you set `code_use_api_key` **and** an `ANTHROPIC_API_KEY` exists, that key
  is put back into the guest's environment on purpose. Guest turns then bill
  your metered API key instead of your Claude Code subscription, and each reply
  reports the real cost. It is a disclosed choice, never an accident.

(`MAX_THINKING_TOKENS` is also injected, when a summons or the config asks for
an effort level above `default`; see "What a summoner can vary", above.)

## Giving a guest scoped read access to memory (or any authed service)

A summoned guest gets whatever MCP servers are listed in `code_mcp`, and each is
allowed whole, meaning **every** tool that server exposes, including any
write-side tool such as `save_memory`. **`code_mcp` is empty by default: out
of the box a guest is mounted with no MCP servers at all.** The usual first
entry is Membro's semantic `membro` recall server; add it in
`config.local.json`, so personal paths never enter the public repo. To also
let a guest do an **exact-fact memory audit** (list fact IDs, statuses, the
review queue), wire in Membro's authenticated read server (`membro-admin`,
HTTP-based, so it sidesteps the stdio DB-open fragility):

```jsonc
// config.local.json  (<repo> = Membro's checkout, e.g. /Users/you/dev/membro)
"code_mcp": {
  "membro": {
    "command": "<repo>/.venv/bin/python",
    "args": ["-m", "memory_service.mcp_server"],
    "env": { "PYTHONPATH": "<repo>" }              // required, see below
  },
  "membro-admin": {
    "command": "<repo>/.venv/bin/python",
    "args": ["-m", "memory_service.mcp_admin_server"],
    "env": {
      "PYTHONPATH": "<repo>",
      "MEMORY_AUTH_TOKEN": "${MEMORY_AUTH_TOKEN}",   // resolved from Crossband's env
      "MEMORY_API_URL": "http://127.0.0.1:8901/v1"
    }
  }
}
```

**Why `PYTHONPATH` is not optional here.** Membro is run from its checkout and
is never pip-installed, so `memory_service` is not on the interpreter's path
just because you named that interpreter. Without it the server exits with
`ModuleNotFoundError: No module named 'memory_service'` at spawn, and the guest
simply arrives with the tool missing. `-e PYTHONPATH=<repo>` is the form
Membro's own README documents for `claude mcp add`; this is that form written
as config.

**`${VAR}` interpolation**: any `${NAME}` token inside a
`code_mcp` server's **`env`** values is replaced with that variable's value
from Crossband's *own* process environment at guest launch, so `"Bearer
${TOK}"` works as well as a bare `"${TOK}"`, which lets a secret be
**referenced by name, never pasted into config**. This applies to `env` only:
a `${VAR}` in `command` or `args` is passed through literally and will not
resolve. Put the real value in Crossband's `.env` (`MEMORY_AUTH_TOKEN=…`). A
missing variable resolves to `""`, so the tool gets no credential rather than a
bogus one, so a typo fails closed instead of leaking the literal `${VAR}`.

**Security tradeoff, a conscious choice rather than a default.** Wiring an
authenticated read server means every summoned guest carries that token and can
read exact rows, including personal facts, which then appear in the guest's
transcript. If that guest later opens a PR, those facts can ride along, the
same PII-into-artifact risk the write-tool rules guard against. Enable it when
you want guests doing memory audits; leave it off otherwise. It never grants
write/approve/dismiss; those stay owner-only, by the server's design.
