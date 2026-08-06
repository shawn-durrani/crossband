# Crossband documentation — start here

Every document in this repo, what it's for, and the order to read it in —
split by what you're trying to do. Written for humans and LLMs alike: pasting
any file here at an AI assistant should get you walked through the task it
covers.

## I want to run Crossband

1. [README](../README.md) — what this is, quick start, which keys to get. The
   in-app setup wizard opens itself on first run and does the key setup with
   you.
2. [docs/MODELS.md](MODELS.md) — every model you can run: the built-in seats,
   one-click presets (Ollama, LM Studio, Groq, Together, OpenRouter,
   Fireworks), the zero-key local path, and how to price a model so it can
   join every round.
3. [docs/CONFIG.md](CONFIG.md) — every setting, in one place: the four config
   layers, all keys with defaults, and `config.local.json.example` to copy
   from. You only need this once something in the README says "configured in
   `config.local.json`".
4. [docs/REMOTE_ACCESS.md](REMOTE_ACCESS.md) — the whole app, voice included,
   from your phone over Tailscale. Nothing exposed to the internet.
5. [docs/OPERATIONS.md](OPERATIONS.md) — keeping a live instance up: the
   launchd supervisor, logs, restart-on-crash, reboot survival, backups.

Companion memory service (optional, auto-detected):
[Membro](https://github.com/shawn-durrani/membro) — its own README covers
install; Crossband works fully without it and lights up memory features when
it appears.

## I want to let the AIs work on my code

- [README § Build from the chat](../README.md#build-from-the-chat-claude-code--github)
  — the user-facing walkthrough: summoning Claude Code, GitHub tools, the
  chat's `code` toggle.
- [docs/GUEST_PERMISSIONS.md](GUEST_PERMISSIONS.md) — exactly what a summoned
  guest can and cannot do, in both modes, and why the bounds are enforced in
  code rather than prompt. Read before widening anything.

## I want to understand or change the code

1. [ARCHITECTURE.md](../ARCHITECTURE.md) — the map: the projection trick, the
   round loop, provider adapters, the guest, cost provenance, the frontend's
   pure-module rule. Ends with a reading order for the code itself.
2. [CONTRIBUTING.md](../CONTRIBUTING.md) — setup, the keyless test suites, the
   rules that matter, and how work lands.
3. [docs/TESTING.md](TESTING.md) — what every suite guards, backend and
   frontend, and why both run keyless.
4. [docs/COST_TELEMETRY.md](COST_TELEMETRY.md) — operator-grade detail on the
   Claude-chat cache telemetry line and utility-model spend attribution,
   including a before/after sampling workflow you can run yourself.
5. [docs/LATENCY.md](LATENCY.md) — what makes a reply fast or slow: the
   plain-English story of the latency pass, the measured budgets before and
   after, what deliberately wasn't changed, and how to re-measure any of it.
6. [eval_critic/README.md](../eval_critic/README.md) — the offline eval
   harness for the memory-provenance critic: fixtures, scoring, and how to
   run it against a live model.

## Safety, security, history

- [SECURITY.md](../SECURITY.md) — the trust boundary, stated plainly:
  loopback-bound, no auth by design, the tailnet is the authentication
  boundary. Reporting instructions included.
- [CODE_OF_CONDUCT.md](../CODE_OF_CONDUCT.md) — the short version and the
  standard it adopts.
- [CHANGELOG.md](../CHANGELOG.md) — user-facing history, plain English.
- [ACKNOWLEDGEMENTS.md](../ACKNOWLEDGEMENTS.md) — who and what this builds on.

## For an AI session working in this repo

[CLAUDE.md](../CLAUDE.md) is your entry point and is loaded automatically; it
links here. The process rules live in CONTRIBUTING.md — follow them from
there rather than re-deriving. The one meta-rule about this index: **every
document in `docs/` must be listed on this page** — `tests/` enforces it, so
adding a doc without indexing it turns CI red.
