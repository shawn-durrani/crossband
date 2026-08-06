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
and refuses to start a second instance on the same port. Set `MMC_PORT`
if 8902 is taken.

Add keys through the setup wizard in the UI, or put them in `.env`. Keys
are validated before they are saved and never echoed back.

## Configuration

`config.json` holds defaults that ship with the repo.
`config.local.json` holds your machine's settings and is gitignored.
Environment variables starting `MMC_` override both. Everything is
documented in [docs/CONFIG.md](docs/CONFIG.md), which is generated from
the code and guarded by a test, so it cannot drift.

Note the `MMC_` prefix: it predates the rename and stays for now, so
existing installs keep working. See below.

## Remote access

Loopback only by default. To reach it from a phone, put it on your own
tailnet with `scripts/tailscale-serve.sh`; HTTPS is required because
browsers will not give a page the microphone otherwise. Never expose the
port to the internet, and never use Tailscale Funnel. Read
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

## Known rough edges in v0.1.0

The app was called Sideband during development, and some internal
identifiers still say so: the `MMC_` environment prefix, the
`dev.sideband.server` launchd label, and the `multi-model-chat` source
tag it sends to Membro. Renaming them breaks existing installs, so they
move together in v0.2 with a migration note. Nothing user-facing says
Sideband.

## Licence

[MIT](LICENSE).
