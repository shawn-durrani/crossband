# Changelog

House convention: user-visible change, one line each, newest first.

## Unreleased

- Room mode, phase 2 (#28): voices get names, and the introduction is
  the trigger. Saying "my wife Alex is here" (no toggle needed) flips
  room mode on for the chat, adds Alex to the roster, and starts
  learning her voice; "Alex has left" removes her. Voices are
  remembered: a few seconds of each person's clear speech is stored on
  this computer (owner-only files, deletable from Models -> Remembered
  voices with a Forget button that deletes the audio), so a known
  person is recognised in later sessions with no introduction. Turns
  are labelled with names; below the learning bar a label stays marked
  uncertain, an unrecognised voice raises a "someone new is speaking -
  who?" prompt you answer by just saying the name, and a background
  cross-check can flag a turn whose content reads like someone else -
  it never changes the label; tap the name on a turn to correct it
  (which also teaches the right voice). An "In the room" chip shows who
  the app is telling apart, which is also the cue that multi-voice
  processing (double transcription spend) is on. The live conversation
  still waits on none of this. Roster size is capped (default 6,
  `CROSSBAND_ROOM_ROSTER_MAX`).
- Room mode, phase 1 (#28): a per-session toggle in the voice controls
  for when more than one person is in the room. While on, each spoken
  turn also goes through a second, diarising transcription pass in the
  background; turns where another voice appears get small unnamed
  "Voice 1" / "Voice 2" chips a moment later. The live conversation is
  untouched - nothing waits on the pass, and with the toggle off the
  voice pipeline is exactly what it was. Stated plainly: telling voices
  apart transcribes the audio twice, so voice minutes roughly double
  while the toggle is on. Labels are best effort for now; naming the
  voices is the next phase.
- A browser gate (#25): an owner password (scrypt verifier, recovery
  secret for enrolment/reset, opaque revocable sessions) now protects
  the UI and API. Enrolment-activated: nothing changes until you set a
  password from the app; after that, every surface asks for it and a
  tailnet caller only ever sees the lock screen. Set
  `CROSSBAND_RECOVERY_SECRET` in `.env` so enrolment and reset work
  without terminal access.
- Passkey unlock (#25): enrol a Touch ID / Face ID passkey from the
  Integrations console and the lock screen offers it first, password
  one click behind. Passkeys are per web address (`localhost` and the
  tailnet name enrol separately; an IP address cannot hold one, so
  `127.0.0.1` keeps the password form), and the tailnet passkey syncs
  to your other devices via your keychain.

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
  namespaces. Old-namespace leftovers are still reclaimed until v0.3:
  stale worktree directories are swept (registered with git or not) and
  orphaned refs/mmc-guest refs are deleted at the next visit to that
  repo. One limit: a guest session begun before the upgrade cannot be
  resumed with continue_last, because its transcript is keyed by the
  old working directory; summon a fresh visit instead.
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
