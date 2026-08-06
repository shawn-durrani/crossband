# Changelog

House convention: user-visible change, one line each, newest first.

## Unreleased

- `./start.sh` now serves the app on a first run. It creates `.env` from
  the example, warns about whichever keys are missing, and starts the
  server, instead of exiting and asking you to rerun it after the venv
  and the frontend build were already done.

## v0.1.0 (2026-08-06)

First public release, under the name Crossband.

- Group chat with several AI models in one shared transcript: each
  provider sees the conversation projected into its own two-party
  format, so seats can address and disagree with each other.
- Detached rounds: generation runs as a background task writing to a
  replayable buffer, so a dropped connection never kills a reply.
- Prompt-cache-aware prompt assembly, with stable content before the
  breakpoint and per-round content after it.
- Shared tools every seat can call: web search, page and Reddit and
  YouTube fetch, GitHub issues and PRs, memory recall and save, and
  self-diagnostics. Results persist and are visible to every
  participant.
- Claude Code as a summonable guest: its own git worktree, read-only by
  default, opt-in implement mode that branches, tests, pushes and opens
  a PR but can never merge.
- Voice in and out through the backend, so the key stays server-side,
  with content-free latency instrumentation.
- Cost accounting with provenance: metered, subscription-equivalent and
  unknown are never summed, and pricing fails closed rather than
  guessing a rate for an unknown model.
- Optional memory through the Membro HTTP contract; absent, the app
  works and forgets.
- Local models via Ollama and LM Studio presets, with no key required.
