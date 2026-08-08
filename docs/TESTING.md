# What the test suites guarantee

Two suites, both keyless by design:

```sh
env -u OPENAI_API_KEY -u ANTHROPIC_API_KEY .venv/bin/python -m pytest -q
node --test frontend/src/*.test.js
```

CI runs them as separate steps, so a green pytest run is not the whole
gate. Nothing in either suite calls a provider: every network path is
mocked, and a test that needed a key would be a bug in the test.

## Backend

**The transcript projection.** Each provider receives its own turns as
assistant and everyone else's as labelled user turns. This is the trick
the whole app rests on, so it is pinned per provider, including what
happens when a seat has never spoken and when a message carries
attachments.

**Prompt-cache layout.** Stable content sits before the breakpoint and
per-round content after it. Tests assert which block each field lands
in, because putting a volatile field in the stable block silently
re-writes the whole cached prefix every turn and costs money without
changing any visible behaviour.

**Trusted context.** App-assembled context arrives as a system entry
where the provider supports one, and otherwise carries a per-process
marker. Tests forge the marker from inside a transcript and assert the
forgery is rejected.

**One insert path.** A guard test greps the source: any raw insert into
messages outside `db.insert_message` fails the build, with one named
exemption for the history importer. Live inserts must ring the notify
bell after committing, never before.

**Rounds and streaming.** A dropped connection does not cancel
generation; a reconnect replays from a watermark; only a real abort
marks a message as cut off; two rounds for one chat cannot interleave.

**Guest isolation.** Permissions come from this process rather than the
operator's own settings, and each visit gets its own worktree at a
freshly fetched base. On credential files the suite pins the asymmetry
rather than a guarantee: implement mode's options carry the `Read(.env)`
family, investigate mode's options carry no `Read` rule at all, and
neither mode path-restricts `Grep` or `Glob`. Every guest test mocks the
SDK boundary, so what is asserted is which rules are handed to Claude
Code, not that the CLI refuses a read; the allow-versus-deny precedence
is mirrored by a helper in the test file rather than exercised.

**The room-mode tee.** The parallel diarization pass may never cost the
live voice path anything: with the toggle off the realtime relay sends
byte-for-byte the frames it always sent and makes no second call; with
it on, the tee slices on the same commit boundaries, the pass runs as a
never-awaited background task (pinned by wedging the batch call open
and watching the committed transcript arrive anyway), and a failed pass
leaves the turn unlabelled with everything else working.

**Multi-human room mode.** The same law extends to phase 2: a spoken
introduction is confirmed by a fire-and-forget utility call that the
send never awaits (pinned by wedging the confirmation open and watching
the send complete), the anchor store keeps owner-only file permissions
and forgetting a person actually deletes their audio from disk,
identification below the anchor-sufficiency bar stays uncertain, an
unmatched voice raises the ask-fallback instead of guessing, and the
LLM mismatch cross-check can only ever raise a flag - there is no code
path from it to a label write.

**Attribution downstream (phase 3).** Label writes key on the exact
turn: the commit frame carries the client's turn id, the same id the
/send persists on the message, so a diarization pass may only ever
label the message its utterance actually became - a dropped short
interjection labels nothing rather than a neighbour, and the id never
reaches the upstream byte stream. Confidently named turns enter the
model-facing transcript projection as that person "(in the room)";
uncertain turns enter as an unidentified speaker, never the owner and
never the guessed name; and a chat with no labels at all renders
byte-identically to what the builders always produced. The owner's
roster identity is the `user_name` setting - an introduction name that
is plausibly the owner's own transcribed name is dropped rather than
minting a phantom person - and roster display names ride the realtime
STT connection's keyterms parameter (bounded to the documented caps)
so spellings stop drifting. On the memory handoff, each turn's speaker
class is resolved on the way out: owner turns stay `user` exactly as
always, one confident guest ingests as `guest:<preferred name>`, and
uncertain or open-flagged turns ingest as `guest:unknown` - with
`SOURCE_APP` pinned to its permanent historical value.

**Crosstalk (phase 4).** People talking over each other is marked, never
papered over: the batch pass's per-word speaker map is kept instead of
being reduced away, a turn whose words carry two or more voices gets a
crosstalk marker in its label metadata (message content is never
rewritten), and the marker follows the turn everywhere - a plain-English
note in the UI, an appended note in the model-facing projection so a
seat can ask the quieter person to repeat, and an unconditional
`guest:unknown` on memory ingest, surviving even a human correction of
who spoke. The best-effort split attributes cleanly-alternating words
per voice, but only persists when the batch words align with the
realtime transcript the message actually carries; simultaneous speech
(overlapping word intervals), misalignment, or missing labels all fall
back to the marker alone, and an uncertain voice's segment renders as an
unidentified speaker, never as the guessed name. The seat-facing
room-labels explainer (labels are text, seats hear no audio, uncertain
means unknown) is pinned to the STABLE prompt block, and the cache
layout pins prove it cannot bust the prefix. The capture experiment is
pinned at both ends: the pure module asks the mic for the untouched solo
constraints outside room mode and drops noise suppression and auto gain
inside it, and the relay logs an allowlisted profile name (nothing else)
while the upstream byte stream stays byte-for-byte identical. A
relationship noun (wife, mate, boss) is never stored as a person's name:
the proper name in the same verdict wins, a relationship-only
introduction re-identifies a remembered person named in the utterance or
raises the ask-fallback, and the field-test case - "this is me, Sam,
the owner's wife" - is pinned to yield Sam, never Wife.

**Arming and the session-start sniff (third field test).** Room mode
must be reachable by every spoken door, and its failures must be
visible. The two field phrasings that silently never armed - an unnamed
handover ("hand over to a guest") and a guest's self-introduction with
a proper name and a short form - are pinned end to end: the first arms
room mode and raises the ask-fallback without ever minting a
placeholder person, the second arms it, adds the person under their
proper name, and keeps the spoken short form ("also known as", "call
me") as their preferred display name at creation only - a later
re-introduction never overwrites what may have been corrected by hand.
Every introduction scan now ends in exactly one content-free INFO
verdict line with an allowlisted outcome, so "the model said no" can
never again be confused with "the scan never ran". The session-start
sniff closes the structural gap that remembered voices could not arm a
fresh chat: with room mode off and sufficient remembered non-owner
voices, the first two committed utterances also run the existing
diarization pass - the committed transcript is pinned to arrive while
the sniff's batch call is wedged open, a match arms room mode and seeds
the roster linked to the matched anchors, two anchorless clusters arm
with ordinals but seed and ask nothing, two negative passes end the
sniff at exactly two metered batch calls, and the sniff is pinned off
when nobody (or only the owner) is remembered.

**Cost and provenance.** Metered, subscription-equivalent and unknown
never merge; provenance is stamped at write time and cannot be
backfilled; an unknown model id stays unpriced instead of inheriting a
family rate; the usage endpoint exposes no combined total.

**Boundaries.** Non-loopback hosts are refused unless explicitly
trusted; cross-site API requests are rejected; websocket routes check
Origin as well as Host; routes that spawn a process from request data
refuse a remote caller; read limits are bounded.

**The leak scanner.** It rejects real-shaped keys, infrastructure
identifiers and deny-listed personal content, passes documented
placeholders, and the committed tree must scan clean. A separate test
plants a leak in a throwaway repository to prove the tree walk can
actually detect one, so the gate cannot rot into a scan of nothing.

## Frontend

Rules live in pure `.js` modules; the suites cover cost formatting and
the subscription-versus-metered split, cache-health verdicts, the event
and round streams including reconnect and lost-wakeup handling, voice
gating and recovery, rate-card layering, and the header and spend views'
arithmetic.

React components have no test infrastructure, deliberately. The
consequence is stated rather than hidden: anything that exists only
inside JSX has no automated guard, so running the app is part of
changing it.

## What neither suite covers

Whether the models say anything useful. Conversation quality, tool
choice and answer accuracy are judged by the eval harnesses in
`eval_critic/` and `eval_silence/` and by use, not by unit tests. Green
CI means the machinery keeps its promises.

## Suite index

One line per suite, both halves. A test asserts this list stays
complete in both directions, because it drifted three times in one
week when it was maintained by hand.

**Backend** (`tests/`)

- (`test_accounting.py`) Shared cost accounting
- (`test_app.py`) Boot smoke test
- (`test_attribution_audit.py`) The non-blocking DIAGNOSTIC half of the source-provenance fix
- (`test_auth_gate.py`) The browser gate: enrolment-activated sessions, recovery-gated setup/reset, websocket guard
- (`test_passkeys.py`) Passkey unlock: per-origin enrolment, discoverable-credential ceremonies, replay/clone refusal
- (`test_boundary.py`) Localhost trust boundary
- (`test_cache_split.py`) Claude-chat prompt-cache layout
- (`test_chat_memory.py`) Auto-title refresh triggers
- (`test_config.py`) Config layering
- (`test_context_provenance.py`) A live incident where a participant treated a legitimate Crossband context-refresh block (including the voice-mode delivery constraints) as untrusted/injected content, refused to follow it, and later denied being in a live voice call at all
- (`test_context_weight.py`) Conversation weight counts attachments, not just text
- (`test_crosstalk.py`) Crosstalk detect-and-mark, the alignment-gated best-effort split, and the capture-profile log (#28 phase 4)
- (`test_delegation.py`) Explicit shared delegation/claim state for specialist actions
- (`test_diag_mcp.py`) get_diagnostic
- (`test_diagnostics_tool.py`) get_diagnostic on the NATIVE tool-calling surface
- (`test_effort.py`) Reasoning-effort gating table, with semantics ported from the predecessor
- (`test_engine.py`) Round-loop characterization
- (`test_eval_critic.py`) Tests for the offline critic eval harness itself (fixture loading, prompt isolation, verdict parsing, scoring math) -- no live API calls; the model call is always faked
- (`test_events.py`) Global live-events bus
- (`test_github_tools.py`) GitHub issue tools
- (`test_guest.py`) summon_claude_code, in both investigate and implement modes
- (`test_guestjobs.py`) Async guest execution as a decoupled background job
- (`test_images.py`) Image downscaling on upload
- (`test_importer.py`) Provider-export importer
- (`test_ingest.py`) External event ingestion
- (`test_insert_message_guard.py`) Guardrail
- (`test_integrations.py`) Unified integration status
- (`test_lifecycle.py`) Model onboarding lifecycle
- (`test_llm_util.py`) backend.llm_util
- (`test_mcp_client.py`) MCP client layer
- (`test_mcp_servers_api.py`) Connections-page MCP management
- (`test_mcp_servers_auth.py`) Host-level routes stay on the host
- (`test_memory_attribution.py`) Guest speaker classes on memory ingest (#28 phase 3): owner turns stay `user`, confident guests go `guest:<preferred name>`, uncertain and open-flagged turns go `guest:unknown`, `SOURCE_APP` untouched
- (`test_memory_client.py`) Memory client degradation
- (`test_models_status.py`) GET /api/models/status
- (`test_openai_client.py`) Keyless-local edge for the OpenAI-compatible adapter
- (`test_prewarm.py`) Ambient recall fires at speech-end, the round adopts it only on a match
- (`test_pricing_api.py`) Operator-editable rate cards
- (`test_projection.py`) Characterization tests for the transcript projection, covering the load-bearing invariants, including room-mode voice labels (#28 phase 3) and the unlabelled-chat byte-identity gate
- (`test_prompt_guardrail.py`) High-salience personal-claim guardrail
- (`test_reasoning_policy.py`) The reasoning-effort policy must be AUTHORITATIVE at the actual request-kwargs level, not just in the translation helpers (tests/test_effort.py covers those in isolation)
- (`test_room_anchors.py`) The durable voice-anchor store (#28 phase 2): owner-only file permissions, the clip quality gate and keep-best-N refresh, the sufficiency bar, forget-deletes-audio, the prefix builder, the tap-to-correct audio cache
- (`test_room_identify.py`) Anchored identification (#28 phase 2): anchor-prefix requests with the roster+1 hint, name labels, cross-session re-identification, elimination and anchor accumulation, the unknown-voice ask-fallback, the mismatch flag that never mutates a label, tap-to-correct, roster/flag live events
- (`test_room_intro.py`) Introduction detection (#28 phase 2): the send-never-waits pin, the lexical prefilter gating utility spend, confirmed introductions/departures driving room mode and the roster, the cap, owner-anchor seeding, the third-field-test arming phrasings, alias capture, the per-scan verdict line
- (`test_room_mode.py`) Room mode's parallel diarization (#28 phase 1): toggle-off byte-for-byte identity on the realtime relay, the commit-boundary tee, the never-awaited pass, retro labels and their live-events push
- (`test_room_sniff.py`) Session-start sniff (#28, third field test): a remembered voice arms a fresh chat, bounded at two never-awaited metered passes, pinned off without remembered non-owner voices
- (`test_rounds.py`) Detached rounds
- (`test_secret_scan.py`) Guardrail for scripts/secret-scan.sh, the ONE scanner that runs both as the local pre-commit hook and, through this test, in CI
- (`test_setup.py`) Setup router
- (`test_shutdown.py`) Stopping the app must actually stop it
- (`test_silence_default.py`) Follow-up to the silence rule
- (`test_silence_eval.py`) Tests for the silence/speak-vs-pass fixture eval (`eval_silence/`) -- no live API calls, no model judgment
- (`test_silence_rule.py`) The "don't pile on" rule was reworded away from judging redundancy by informational content alone (which wrongly let a model pass with a bare "…" on a plain group-directed greeting) to a general relational-cost-of- silence principle
- (`test_single_primary_human.py`) Single-primary-human invariant guard
- (`test_slash_commands.py`) Slash commands
- (`test_ssrf.py`) SSRF guard characterization
- (`test_stt_relay.py`) The realtime STT relay, driven end to end with a faked ElevenLabs socket
- (`test_supervisor_plist.py`) The launchd supervisor is a placeholder template rendered by ops/install-supervisor.sh at install time
- (`test_tool_concurrency.py`) One assistant turn's tool calls run concurrently, in-order
- (`test_tool_dispatch.py`) Tool dispatch
- (`test_utility_usage.py`) Utility-model (Haiku) spend attribution
- (`test_voice_rounds.py`) Voice playback regression guards for the change that decoupled Claude Code guest execution from the turn lifecycle
- (`test_voice_trace.py`) Per-turn voice latency instrumentation
- (`test_work_status.py`) Announce pending external work before the chat goes silent, using a STRUCTURED status event (a trusted activity label) rather than hardcoded filler text, and NEVER persisting it into a chat message or the model's own reply content

**Frontend** (`frontend/src/`)

- (`cacheHealth.test.js`) Tests for the Spend page's cache-health block: it must surface a per-seat
- (`captureProfile.test.js`) The mic capture-profile decision (#28 phase 4): solo constraints untouched, room mode drops the single-voice tuning, profile names match the relay's log allowlist
- (`eventStream.test.js`) Pure-function tests for the global live-events stream
- (`guestJobs.test.js`) Pure-function tests for the guest job status chip
- (`headerState.test.js`) Tests for the header title/badge: it must track the active chat_id, so the
- (`headerView.test.js`) Tests for the chat header's derivations (extracted from App.jsx)
- (`integrationsView.test.js`) Tests for the Integrations console's pure presentation layer. These pin the
- (`lifecycle.test.js`) Tests for the Participants UI: it must SHOW trial vs onboarded, make a trial
- (`lockState.test.js`) The gate's view rule: which lock-screen face shows for which session state
- (`webauthnCodec.test.js`) WebAuthn wire plumbing: base64url round-trips, option decoding, credential serialisation, cancellation copy
- (`mcpServersView.test.js`) Tests for the MCP panel's derivations
- (`messageCost.test.js`) Tests for the chat surface's cost labelling: it must not present
- (`modelReadout.test.js`) Tests for the per-seat model-status readout: it must not misread on a narrow
- (`rateCards.test.js`) Tests for owner-entered rate cards: an owner must be able to price a model
- (`roomState.test.js`) Room-mode roster/flag state (#28 phase 2): the "In the room" chip names exactly the present people, ask/mismatch copy never claims a label changed, the correction menu's options, remembered-voice summaries, preferred display names (#28 phase 3)
- (`runningState.test.js`) Tests for per-chat running-task state
- (`spendView.test.js`) Tests for the Spend page: it must lead with an honest answer, not a table
- (`streamGuard.test.js`) Invariant test for the cross-chat write-guard
- (`textQueue.test.js`) Invariant tests for per-chat text batching + pre-ingestion cancel
- (`voiceChips.test.js`) Room-mode voice chips: user turns only, malformed label data renders nothing, ordinal assignment is first-seen and stable, named chips carry per-label uncertainty and correction state
- (`voiceErrors.test.js`) Playback failure messages are user-readable and name the recovery
- (`voiceGate.test.js`) Pure-function tests for the voice playback gate (a regression guard)
- (`voiceRecovery.test.js`) Tests for voice session recovery: a session must repair itself when the tab
- (`voiceTrace.test.js`) Pure-function tests for the client-side voice latency trace
- (`voiceView.test.js`) Tests for the voice call screen's state rules
