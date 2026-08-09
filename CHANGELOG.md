# Changelog

House convention: user-visible change, one line each, newest first.

## Unreleased

- Fix a voice-identification deadlock (#28). A two-part "enough voice
  learnt" rule shipped one build earlier also demanded a quota of short
  clips, which instantly marked every already-learnt voice as not-yet-
  learnt - and because an unlearnt voice is never matched, and only a
  match banks more voice, nothing could recover. Learning is back to the
  seconds bar; short-clip readiness is a separate progress hint, and a
  confident match on a longer turn quietly banks a short slice of its own
  audio so quick interjections become recognisable without any ceremony.
- The cloud no longer guesses who is speaking (#28). Voice
  identification is now local or honestly uncertain, full stop: the
  on-device matcher names a turn, or the turn stays unnamed - no cloud
  pass ever assigns a name, so the field-tested failure where a solo
  speaker was mis-named by the cloud fallback is structurally
  impossible. The only cloud transcription left in room mode runs when
  people talk over each other, to untangle who said what - so room
  mode's cloud voice spend is now just that, instead of a second listen
  per uncertain turn. If the local matcher is unavailable, turns simply
  go unnamed and room mode switches on only by hand (introduction,
  spoken command, or the toggle): degraded means manual, never wrong.
- Quick interjections become recognisable (#28). A voice now counts as
  learned only once its stored clips include a couple of SHORT ones
  (a word or two), kept in their own best-N pool so long sentences can
  no longer crowd them out - and the learning progress shown under
  Remembered voices says which half is still missing. Matching also
  accepts shorter utterances than before, where the audio is clearly
  voiced.
- Stored voices now audit themselves (#28). Whenever a voice bank
  changes, every clip is checked against every person: a clip that
  sounds more like someone else is set aside (kept on disk, shown as
  "clips set aside", excluded from matching), and two people whose
  stored voices sit close together are flagged in the voice health
  strip ("Alex and Sam sound close - matching is stricter") with the
  matcher automatically demanding a wider winning margin between
  exactly those two. This is the guard against the field-tested
  cross-contamination that once let one person's turns be confidently
  labelled as another.
- Names now arrive with the words, not after them (#28). The app
  starts identifying a speaker the moment they pause - before the
  transcript is even final - so in the common case the name is attached
  by the time the turn appears, instead of a beat later. The head start
  is a content-free hint; nothing extra is recorded or sent anywhere,
  and a hint that turns out stale (the speaker kept going) is simply
  discarded.
- Voice identification's limits are now documented for strangers
  (docs/VOICE_ID.md): where the trigger phrases were grown, the English
  bias and what degrades, every tuning knob (threshold, margin, the
  two-part sufficiency bar - all configurable now), similar-sounding
  households, and the scale bounds (roster cap 6 by default,
  single-owner by design).
- Naming is law (#28). A name you set - by renaming a remembered voice
  or just saying it ("her name is spelt Samantha") - is now locked and
  wins everywhere a name appears: the labels on spoken turns, the "In
  the room" chip, what the AIs are told, what memory records, and the
  transcriber's spelling hints. No automatic step can change it back.
  Spelling variants of a name you already know are recognised as the
  same person instead of creating a duplicate with a blank voice
  memory; when the app is not sure, it asks ("Is Sal the same person as
  Sam?") instead of guessing. Renaming one person onto another's name
  offers to merge them - their stored voices combine, the best clips
  are kept, and both names keep working. Forgetting someone still
  sticks: the same name heard later starts fresh.
- The voice health strip (#28). The voice dock and the mobile call
  screen now show a compact readout of what voice identification is
  doing: whether the on-device matcher is ready (or fetching its model,
  or falling back to the cloud), whether the room is on, solo, or
  ambient-listening, each remembered voice's learning progress, and how
  the last spoken turn was identified ("local · 227ms", "cloud ·
  1.9s"). The readout is fed by a new content-free endpoint - states,
  counts and milliseconds only, never names or words - and costs the
  live voice path nothing.
- The room-mode toggle is now durable and honest (#28). Switching it on
  acts exactly like saying "group mode": it sticks to the chat, puts you
  on the roster so voices are identified by the fast on-device matcher,
  and the models are told the true state. Previously the toggle only
  affected the current session, silently skipped the fast matcher, and
  the models would say room mode was off right after you turned it on.
- Room mode is now ambient - no trigger needed (#28). In a voice
  session, every turn gets a quiet on-device voice check: your own voice
  changes nothing, a remembered voice switches room mode on and is named
  automatically, and a clear voice the app cannot place asks who is
  speaking. Because known voices are identified locally at no extra cost,
  the second transcription (and its doubled voice spend) now runs only
  for overlapping speech or a voice the local matcher cannot place. Say
  "solo mode" to keep a session private - that preference sticks until
  you turn room mode back on.

- Room mode obeys spoken and typed commands (#28). Saying "group mode,
  please" (or "room mode on", "multi-user mode") now actually switches
  room mode on, and "solo mode" / "room mode off" / "just me now"
  switches it off - previously the AIs would verbally agree while the
  app did nothing. Detection rides the same background check as spoken
  introductions, so live voice latency is untouched, and a cheap
  confirmation step means talking ABOUT the mode ("is group mode on?")
  changes nothing. Switching off also clears the "In the room" chip and
  ends the doubled transcription in a live session. The AIs are now
  told the current room-mode state and roster each round, so "is group
  mode on?" gets a true answer instead of a guess.
- Room mode identifies known voices locally, part 2 (#28). When more
  than one person is in the room, a known voice is now recognised
  on-device in a fraction of a second - so the models see the right name
  on the turn itself, instead of waiting a second or two for the second
  listen and often reading the turn as you in the meantime. The common
  case where one known person is speaking no longer needs a second
  transcription at all; the second listen still runs whenever two voices
  are present or the match is uncertain, so crosstalk and unknown-voice
  handling are unchanged. The small speaker model (~38MB) is fetched once
  to the data directory, verified, and then runs fully offline - nothing
  about a voice ever leaves the machine. Purely additive: with the model
  or its library absent, or with `CROSSBAND_VOICE_ID_ENABLED=false`, room
  mode behaves exactly as before. Live voice latency is untouched -
  everything here happens on the background pass.
- Room mode label latency, part 1 (#28, night test 4). The name check
  runs a second or two behind the words, so the first AI to answer used
  to read a fresh spoken turn before its name existed and assume it was
  you. A turn whose name is still on the way now reads honestly as
  "Identity pending (in the room)" - the models are told the name is
  still being worked out and not to guess - and any AI speaking later in
  the same round picks up the resolved name the moment it lands. The
  check itself lost its avoidable delays too: labels attach the instant
  the second listen returns instead of on a half-second polling step,
  spend bookkeeping happens after the labels rather than in front of
  them, and the remembered-voice samples that preface every check are
  cached instead of re-read from disk on every spoken turn. Live voice
  latency is untouched - everything here happens on the background pass.
- Room mode arming fixes from the third field test (#28). Two spoken
  triggers that silently did nothing now work: a handover with no name
  ("I'm going to hand over to a guest") switches room mode on and asks
  who the guest is - never inventing a name - and a guest introducing
  themselves ("I'm Samantha, Alex's wife, also known as Sam") switches
  it on and adds them under their proper name, keeping the short form
  ("also known as", "call me") as their preferred display name. Every
  introduction check now leaves one plain log line saying what it
  decided, so a silent failure can no longer be mistaken for the check
  not running. And remembered voices can now switch room mode on by
  themselves: when a session starts with room mode off in a household
  with remembered voices, the first couple of spoken turns are also
  transcribed a second time, listening for a known voice or a second
  speaker - stated plainly, those sessions transcribe their first
  couple of utterances twice - and a recognised voice switches room
  mode on and joins the roster with no introduction needed.
- Room mode, phase 4 (#28): honesty about people talking over each
  other, and three fixes from the second field test. When two voices
  land in one spoken turn, the turn now says so - "Two voices at once -
  some words may be missing" - because on a single microphone the
  quieter person's overlapped words are often simply gone; the models
  see the same note so they can ask the quieter person to repeat, and
  such turns are never saved to memory as any one person's words. When
  the two voices took turns cleanly rather than overlapping, a
  best-effort split shows who said which words (shown only when the
  second listen agrees with the live transcript; your message text is
  never rewritten). The models are also told plainly what a voice label
  is - text produced by a second listen, not audio they can hear - so a
  seat can no longer claim it "can tell from the voice". Introductions
  stop storing relationship words as names: "this is me, Sam, Shawn's
  wife" now yields a person named Sam, never "Wife" - a
  relationship-only introduction matches a remembered person if the
  sentence names one, and otherwise the app just asks who it is. A
  still-learning voice now shows its progress (seconds heard toward the
  bar) in the remembered-voices panel and the room chip, so waiting is
  an informed choice. And room-mode sessions now capture the mic with
  the browser's single-voice noise tuning switched off (it can muffle
  the second speaker); solo sessions are untouched, and each session's
  capture profile is logged so the experiment can be judged on field
  data.
- Room mode, phase 3 (#28): attribution lands everywhere it matters.
  Voice labels now attach to exactly the turn that was spoken (a quick
  interjection can no longer be labelled onto a neighbouring turn), and
  the models finally SEE the labels: a turn confidently matched to a
  named person reads as that person "(in the room)" in every model's
  view of the chat, while an uncertain turn reads as an unidentified
  speaker - never guessed, never silently credited to you. Names stop
  drifting: your own name always comes from the `user_name` setting
  (never from what the transcriber heard), each remembered voice gets
  an editable preferred spelling (Models -> Remembered voices, pencil
  icon), and everyone's names are fed to the live transcriber so it
  spells them consistently. When a chat is saved to memory, guests'
  statements are recorded as that guest - and membro quarantines them
  for review - while anything the app is not sure about is marked as an
  unknown guest rather than being filed as a fact about you.
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
