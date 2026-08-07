# Changelog

House convention: user-visible change, one line each, newest first.

## Unreleased

## v0.2.0 (2026-08-07)

The rename release: the Sideband-era identifiers are retired.

**Migrating an existing install:** rename the `MMC_` lines in your
`.env` to `CROSSBAND_` (until v0.3 the old names still work and every
use logs the exact rename at startup), and rerun
`bash ops/install-supervisor.sh` if you use the supervisor - it boots
out the old `dev.sideband.server` label and installs
`dev.crossband.server` itself.

- Environment variables moved from `MMC_*` to `CROSSBAND_*`, with a
  one-release fallback and per-variable startup warnings.
- The launchd label is `dev.crossband.server`; the installer migrates a
  pre-rename install automatically.
- The guest diagnostics MCP is `crossband-diag`, so the tool id in the
  tool-activity strip reads `mcp__crossband-diag__get_diagnostic`.
- The app's loggers moved from `mmc.*` to `crossband.*`; if you grep
  `service.log` by logger name, update the pattern.
- Guest worktrees and temporary git refs now use crossband-guest
  namespaces; leftovers from the old names are still cleaned up until
  v0.3.
- The source tag sent to Membro stays `multi-model-chat`, now
  documented as deliberately permanent: Membro keys conversation
  identity on it, and renaming it would fork every open chat's memory
  history.

## v0.1.1 (2026-08-07)

- A chat whose only seat is a trial (unverified-cost) model can now be
  spoken to: explicit addressing reaches trial seats even when it
  covers the whole roster, in both typed and spoken forms. Previously
  such a chat completed rounds with no speakers and no error.
- Source comments and config examples now tell the truth about the
  guest's two modes, and every code_mcp example carries the required
  env key (copying the old example produced a mount that died at
  spawn with nothing telling you why).
- UI and error copy now uses plain punctuation instead of em-dashes,
  matching the docs. Placeholder glyphs (a bare em-dash standing for an
  empty value) are unchanged.
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
