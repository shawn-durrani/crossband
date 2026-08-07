# Crossband

Crossband is a group chat with several AI models at once. Claude and GPT
sit in one shared transcript, see each other's messages, and can agree or
disagree. Everything runs on your machine: a FastAPI backend, a SQLite
file, and a web UI on loopback. Models reach shared tools (web search,
page and Reddit fetch, GitHub issues, memory) and every result is
persisted where every participant can see it.

The name is a radio term. A crossband repeater receives on one band and
retransmits on another, which is roughly what the app does across model
APIs that only understand two-party conversations.

## What it does

- **One transcript, many models.** Each provider sees the same
  conversation projected into its own two-party format: its own turns as
  assistant, everyone else's as labelled user turns.
- **Summon Claude Code as a guest.** It joins for a turn in its own git
  worktree, read-only by default. Opt-in implement mode branches, tests,
  pushes and opens a PR; it can never merge.
- **Voice.** Speech in and out through the backend, so the key stays
  server-side. Works from a phone over your own tailnet.
- **Cost accounting that does not lie.** Metered spend, subscription-
  equivalent, and unknown are tracked separately and never summed. A
  model with no known price stays unpriced rather than inheriting a
  guess.
- **Optional memory.** With [Membro](https://github.com/shawn-durrani/membro)
  running, the room remembers across conversations. Without it,
  everything works and simply forgets.

## Requirements

- Python 3.12+ and Node 20+
- At least one provider key: Anthropic, OpenAI, or a local model through
  Ollama or LM Studio, which need no key at all.

## Quick start

```sh
git clone https://github.com/shawn-durrani/crossband.git
cd crossband
./start.sh
```

The app serves **http://127.0.0.1:8902**. `start.sh` creates the venv,
installs dependencies when they change, builds the frontend if needed,
and refuses to start a second instance on the same port. Set `CROSSBAND_PORT`
if 8902 is taken.

Add keys through the setup wizard in the UI, or put them in `.env`. Keys
are validated before they are saved and never echoed back.

## Configuration

`config.json` holds defaults that ship with the repo.
`config.local.json` holds your machine's settings and is gitignored.
Environment variables starting `CROSSBAND_` override both. Everything is
documented in [docs/CONFIG.md](docs/CONFIG.md), which is generated from
the code and guarded by a test, so it cannot drift.

Pre-v0.2 installs used the `MMC_` prefix; those variables still apply
until v0.3, and every use logs the exact rename at startup. See below.

## Remote access

Loopback only by default. To reach it from a phone, put it on your own
tailnet: name the tailnet hostname in `CROSSBAND_TRUSTED_HOSTS`, then run
`tailscale serve --bg https / http://127.0.0.1:8902`. There is no helper
script; [docs/REMOTE_ACCESS.md](docs/REMOTE_ACCESS.md) writes the
procedure out step by step. HTTPS is required because browsers will not
give a page the microphone otherwise. Never expose the port to the
internet, and never use Tailscale Funnel. Read
[SECURITY.md](SECURITY.md) first: this app has no authentication, so who
can reach the port is the whole security model.

## Documentation

[docs/README.md](docs/README.md) indexes everything by what you are
trying to do. The short version:

- [ARCHITECTURE.md](ARCHITECTURE.md): the settled design decisions.
- [docs/CONFIG.md](docs/CONFIG.md): every setting, with defaults.
- [docs/MODELS.md](docs/MODELS.md): adding a model, including local ones.
- [docs/GUEST_PERMISSIONS.md](docs/GUEST_PERMISSIONS.md): exactly what a
  summoned coding agent may and may not do. Read before enabling
  implement mode.
- [docs/REMOTE_ACCESS.md](docs/REMOTE_ACCESS.md): tailnet setup.
- [docs/OPERATIONS.md](docs/OPERATIONS.md): keeping it running.
- [docs/COST_TELEMETRY.md](docs/COST_TELEMETRY.md): reading your own
  cache and cost numbers.
- [docs/TESTING.md](docs/TESTING.md): what the suites guarantee.

## Upgrading a pre-v0.2 install

The app was called Sideband during development. v0.2 retired the
identifiers that still said so: environment variables moved from `MMC_`
to `CROSSBAND_`, the launchd label from `dev.sideband.server` to
`dev.crossband.server`, and the guest diagnostics MCP from
`sideband-diag` to `crossband-diag`. To migrate an existing install:
rename the `MMC_` lines in your `.env` (the app warns at startup with
the exact renames until you do; old names stop working in v0.3), and
rerun `bash ops/install-supervisor.sh` if you use the supervisor - it
retires the old launchd label itself.

One historical name is deliberate and permanent: the source tag sent to
Membro is `multi-model-chat`. <!-- secret-scan: allow: the legacy source tag is named here on purpose -->
Membro keys conversation identity on it, so renaming it would fork
every open chat's memory history for zero user-visible gain. It shows
up only in Membro's admin facts view, like a database table name.

## Licence

[MIT](LICENSE).
