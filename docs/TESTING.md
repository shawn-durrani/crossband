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

**Arming (third field test onward).** Room mode must be reachable by
every spoken door, and its failures must be visible. The two field
phrasings that silently never armed - an unnamed handover ("hand over
to a guest") and a guest's self-introduction with a proper name and a
short form - are pinned end to end: the first arms room mode and raises
the ask-fallback without ever minting a placeholder person, the second
arms it, adds the person under their proper name, and keeps the spoken
short form ("also known as", "call me") as their preferred display name
at creation only - a later re-introduction never overwrites what may
have been corrected by hand. Every introduction scan now ends in
exactly one content-free INFO verdict line with an allowlisted outcome,
so "the model said no" can never again be confused with "the scan never
ran". The session-start EL sniff that once closed the remembered-voices
gap is RETIRED (#28 PR-B, below); the ambient local check owns that job,
and the old sniff suite now pins the retirement itself.

**The cloud identity retirement (#28 PR-B, eighth field test).**
Identity is local or honestly uncertain: the on-device matcher names a
turn or the turn stays unresolved, and NO ElevenLabs call ever fires
because the matcher deferred - a solo utterance can never trigger one,
every defer reason takes the same silent exit, and with the matcher
disabled or unavailable nothing automatic happens at all (the manual
doors - introductions, commands, the toggle - still arm, pinned).
The batch diarize call keeps exactly one trigger: the matcher's window
analysis returning "multi" (genuinely overlapping speech), which runs
the anchored crosstalk split - pinned to be the only metered EL spend,
with the wedge/latency pins re-pinned on both the matcher and that one
surviving call. The eighth field test's false split/arm/name is pinned
structurally impossible: the sniff's functions no longer exist, and its
exact conditions (fresh chat, remembered voices, undecidable first
utterances) fire zero EL calls and arm nothing. Sufficiency is a
TWO-PART bar - the seconds target and a minimum of short (~1-2s) clips,
kept best-N per length class so long clips can no longer starve the
short class - and the hygiene guard audits every bank change: a clip
closer to another person's centroid than its own quarantines (kept on
disk, excluded from matching, surfaced as "set aside"), close centroid
pairs surface in the health strip and automatically widen the match
margin for exactly that pair. Speculative identity at silence-start:
the client's content-free hint frame sends no byte upstream (pinned),
the local check runs on the buffered utterance while the silence window
counts down, a fresh verdict labels the commit with no re-embed (one
matcher call per turn, pinned), resumed speech discards the stale
verdict, and a cached match on someone outside the consuming pass's
candidates re-checks instead of smuggling the name through. Since
remembered-first matching (fourteenth field test, below) the armed
pass's candidates are the hint's own list - every sufficient
remembered person - so a cached match on a remembered but not yet
rostered person is trusted and seats them (that pin is deliberately
rewritten from its old opposite), and the narrowing case that still
re-checks (a person no longer among the candidates) is pinned at the
unit seam.

**Room-mode commands (chat 198).** "Group mode, please" used to do
nothing: it is not an introduction, so the scan (correctly) logged
no_prefilter_match while the seats verbally agreed to a switch no code
was performing. A command lexicon now rides the same post-commit
fire-and-forget scan - the send-never-waits law is pinned by wedging
the command confirmation open - with a deterministic prefilter gating
one utility call and the model judging direction, so a question about
the mode confirms as none and changes nothing. A confirmed arm flips
the durable flag and diarize's live mirror through the existing
control plumbing and rosters the owner (linked to remembered anchors
when they exist, anchor-seeded from the stashed utterance when the
command was spoken); a confirmed disarm flips it off, marks everyone
still present left (the cap frees, the chip disappears) and resolves
the open unknown-voice ask, while mismatch flags survive - they doubt
past turns, which going solo answers nothing about. The verdict-line
allowlist grows armed_by_command / disarmed_by_command, still exactly
one content-free line per scan even when a turn is both a command and
an introduction. Seats stop guessing about the mode: the engine hands
every seat the chat's current room mode and present roster names,
rendered as one short line in the UNCACHED volatile tail - the
projection pins the line to the volatile block, roster names are
pinned out of the transcript turns, and the cache-split pins prove
that flipping the mode or the roster moves no byte of the cached
prefix. On the client, a live session follows a server-side disarm
only when its session flag came from the server, never overriding the
user's own session-only toggle.

**Label latency, part 1 (night test 4).** The identity pass loses the
race to the round's first responder by arithmetic (the batch reply takes
1.0-1.9s; the first seat reads at roughly commit plus 0.3-0.9s), so this
phase is honesty plus free trims, and both halves are pinned. Honesty: a
room-mode user turn committed with a voice turn id and still unlabelled
projects "Identity pending (in the room)" while younger than the pending
window - never the owner's name - and outside that narrow gate (past the
window, solo chats, text sends, any label write landing, room mode off)
the historical rendering returns byte for byte; the stable-block
explainer tells seats a pending name is still being worked out and must
not be guessed. Each later seat's boundary folds label writes since the
round's cursor into the rows it already holds, so the second and later
speakers project the resolved name with no extra full transcript read.
Trims: with an exact turn id the labels attach by direct lookup the
moment the batch reply parses - a short fast retry covers only the
row-not-yet-persisted /send race, and the probe cadence survives only
for id-less commits - the meter write books after the label write and
still books when labelling fails, and the anchor prefix is cached per
roster snapshot with two hard pins: a cache hit reads zero clip files,
and any anchor mutation invalidates, whichever store instance wrote it.

**The owner's identity is shown, not hidden (#28 PR-C).** The ambient
check voice-verifies the owner constantly (it is how solo sessions stay
solo), and the projection used to hide that on purpose: a confident
owner match rendered byte-identically to an unlabelled turn. That pin
is deliberately replaced. A room-off confident owner match now writes
an owner-marked confident label on the turn (never arming, never
rostering, never calling ElevenLabs, anchor top-up unchanged), the
projection renders a confident owner-alone label as the owner's name
plus the voice-confirmed marker in every mode - room on, ambient, or
solo - the chips show a quiet voice-confirmed tick, and the
stable-block explainer tells seats what the marker means, so "who is
speaking?" is answerable in a solo chat. What survives, still pinned: a
chat with NO labels renders byte-identically to history, uncertain
labels still never become the owner or a guessed name, and the ambient
no-arm and local-only guarantees hold unchanged.

**Naming is law (#28).** Owner-set display names are locked: the rename
UI and a spoken correction ("her name is spelt ...") both set a
preferred name owner-set, and from then on no automated path - alias
capture, introductions, anything - may change it. The preferred name is
pinned to win on every surface a name renders or ships: the voice
chips, the roster snapshot, the model-facing projection heads and
crosstalk splits (the OWNER's head is the pinned exception: since PR-C
it renders the bare configured name plus the voice-confirmed marker,
never the preferred map, and uncertain labels still never leak a
name), memory ingest's `guest:<preferred name>`, and the STT keyterm
hints. Corrections match conservatively - the named target must resolve
to exactly one known person, an unnamed correction falls back to the
most recent confidently-labelled speaker, and ambiguity does nothing
rather than guessing - with the verdict-line allowlist grown by the
correction outcomes. A two-form declaration ("X is the spelling but
it's pronounced Y", fourteenth field test) is an alias statement, not a
rename: its own prefilter shapes are pinned, both forms land on ONE
person as merged names, the declared spelling becomes the display name
only while no owner-set name exists (an owner's chosen name is never
fought), two known people named in one declaration do nothing, and the
verdict line says alias_recorded. Variants merge instead of
duplicating: an introduction name that is confidently a folded-form
variant (case, diacritics, edit distance scaled by name length) of a
remembered person re-identifies them, a confident voice match on the
introduction utterance re-identifies even an unalike name (and never
seeds the owner's anchor from the guest's audio) and records the
introduced spelling as a merged name - so keyterms, find_by_name and
future introductions resolve the new spelling directly, and merged
spellings ride the STT keyterm hints. In an ARMED room the voice-match
arm judges the introduction turn's own remembered audio, never the
stale pre-arm stash (which no longer seeds the owner's anchor once the
room is armed - the fourteenth field test's log shows a guest's
pre-arm utterance being banked as the owner's). A
close-but-not-confident name raises one merge-question flag, and the
rename endpoint returns the conflict so the UI can offer the merge -
banks folding under keep-best-N, the oldest person_id surviving,
roster rows re-pointing, and the survivor answering to both names. A
forgotten person's name reappearing creates a fresh record: forget
stays forgotten.

**Cold-start enrolment (#28).** Someone whose voice bank is empty used
to be stuck: a confident match needs clips, the introduction scan needs
an introduction-shaped sentence, and tap-to-correct needs a label to
tap - so the matcher deferred on every turn, nothing accumulated, and
the seats were told "identity pending" over and over about the only
person in the room. The way out is elimination rather than
recognition, and the guards are what the tests pin. In an armed room
where exactly ONE present person's bank cannot identify them yet -
and, since remembered-first (fourteenth field test), every OTHER
present person's can, so the owner being present and identified no
longer blocks a new guest from learning - a non-multi defer banks the
utterance to that person (`source='cold-start'`, subject to the
ordinary quality gate), links their roster row, labels the turn as
them and stamps a local pulse - with no ElevenLabs call, because
elimination is free. Four shapes must never qualify and each has its
own pin: an overlapping-speech "multi" verdict, two or more
unidentifiable people present, a confident match, and a matcher that
failed outright. The learning state is pinned end to end: the
payload carries `learning` beside a name that also rides `uncertain`
(so consumers written before the marker keep treating it as a guess),
the projection heads the turn "<name> (learning this voice)" rather
than the pending head or the voice-confirmed one, a marker on anything
but a single label falls through to the ordinary uncertain path, the
chip says "· learning" and explains that the name came from
elimination, and the stable-block explainer tells seats to use the
name but treat it as likely rather than certain.

**Remembered-first matching (#28, fourteenth field test).** An armed
room must recognise everyone the store remembers, not just the present
roster. The live failure: two fully-sufficient remembered voices, and
every one of the guest's turns deferred below_threshold - the armed
fast path built its candidates from the roster, and the guest could
not be rostered without being recognised nor recognised without being
rostered. The pins: a sufficient remembered NON-rostered person
speaking in an armed room is named and seated on that first utterance
(certain local label, linked present row, no ElevenLabs call - this
test fails against the roster-as-candidates code by construction); the
armed pass, the ambient check and the speculative check share ONE
candidate construction (every sufficient remembered person) so the
paths cannot drift apart again; past the roster cap the turn is still
named while the roster holds; an already-seated match adds no
duplicate row; and a remembered match answers the open
who-is-speaking ask exactly as a naming introduction does. The EL
crosstalk prefix stays roster-scoped - presence, not memory.

**The voice health strip (#28).** GET /api/voice/health is pinned
content-free - states, counts and milliseconds, never a name and never
transcript text - and the per-chat last-decision record (which path
identified the last turn, and how fast) is bounded and written only
from inside the never-awaited background passes, so the zero-latency
law is untouched by construction. The strip's readouts (matcher state,
room/ambient/solo mode, the live pulse, per-voice learning progress)
derive in a pure frontend module - as does the two-tier dock's status
row: one chip per person in the three states the app can honestly claim
(remembered, part-learned, nothing banked yet), ordered
best-known-first, bounded so the row wraps instead of overflowing, with
the remainder collapsing into a "+N" that still names them, and the
mobile call screen's one-line summary ("Listening · Alex ✓ +1") built
from the same chips.

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
- (`test_passkeys.py`) Passkey unlock: per-origin enrolment, discoverable-credential ceremonies, replay/clone refusal, and #87/#88: the session names where a passkey DOES exist when this address has none, labels are owner-editable (bounded, session-gated), and a successful unlock stamps last-used
- (`test_boundary.py`) Localhost trust boundary
- (`test_cache_split.py`) Claude-chat prompt-cache layout
- (`test_chat_memory.py`) Auto-title refresh triggers, and #22: the attribution floor - a tag-free summary is refused and never replaces the turns it summarised, the truth table for the two-distinct-voices rule, and fold labels using display names
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
- (`test_command_acks.py`) Slash-command dead-man (#58): the producer ack contract is strict (user message, same chat), an unacked command warns exactly once, an acked one never warns, a restart inside the window re-arms, 0 disables
- (`test_machine_surface.py`) Machine side-channel vs the browser gate (#62): the ingest token authenticates `/api/ingest` and the deploy-notice route past an enrolled gate, wrong/missing bearers stay 401, the token buys nothing else, unconfigured installs keep the historical posture
- (`test_mcp_client.py`) MCP client layer
- (`test_mcp_servers_api.py`) Connections-page MCP management
- (`test_mcp_servers_auth.py`) Host-level routes stay on the host
- (`test_memory_attribution.py`) Guest speaker classes on memory ingest (#28 phase 3): owner turns stay `user`, confident guests go `guest:<preferred name>`, uncertain and open-flagged turns go `guest:unknown`, `SOURCE_APP` untouched
- (`test_naming_law.py`) Naming is law (#28): owner-set preferred names are locked against every automated path and win on every surface (projection heads, ingest, chips via the store shape - the owner's own head excepted since PR-C: bare cfg name plus the voice-confirmed marker), spoken corrections set them conservatively, two-form declarations ("X is the spelling but it's pronounced Y") record both names on one person without fighting an owner-set name, introduction variants re-identify instead of minting twins (a voice match recording the introduced spelling as a merged name, judged in an armed room against the introduction turn's own audio and never the stale pre-arm stash - which no longer seeds the owner's anchor once armed), the merge endpoint folds banks under keep-best-N with the oldest id surviving, and forget stays forgotten
- (`test_memory_client.py`) Memory client degradation
- (`test_models_status.py`) GET /api/models/status
- (`test_openai_client.py`) Keyless-local edge for the OpenAI-compatible adapter
- (`test_prewarm.py`) Ambient recall fires at speech-end, the round adopts it only on a match
- (`test_pricing_api.py`) Operator-editable rate cards
- (`test_projection.py`) Characterization tests for the transcript projection, covering the load-bearing invariants, including room-mode voice labels (#28 phase 3), the unlabelled-chat byte-identity gate, and the owner's voice-confirmed head (#28 PR-C)
- (`test_prompt_guardrail.py`) High-salience personal-claim guardrail
- (`test_reasoning_policy.py`) The reasoning-effort policy must be AUTHORITATIVE at the actual request-kwargs level, not just in the translation helpers (tests/test_effort.py covers those in isolation)
- (`test_room_ambient.py`) Ambient room detection (#28): the local matcher runs on every committed utterance while room mode is off - the owner's turn is labelled voice-confirmed without arming anything (PR-C), a remembered voice arms and names, a clear stranger (only when the owner is enrolled) arms and asks, undecidable defers - it never calls ElevenLabs, and "solo mode" sets a sacred durable disarm until an explicit re-enable clears it; since PR-B ambient is the ONLY automatic arming door
- (`test_room_anchors.py`) The durable voice-anchor store (#28 phase 2): owner-only file permissions, the clip quality gate and keep-best-N-per-length-class refresh, the two-part sufficiency bar, forget-deletes-audio, the prefix builder, the hygiene guard's quarantine/close-pair storage, the tap-to-correct audio cache
- (`test_room_cold_start.py`) Cold-start enrolment (#28): the by-elimination decision table (every non-multi defer banks when exactly one present person cannot be identified yet; a "multi" verdict, a second unidentifiable person in the room, a confident match and a failed matcher never do), the roster derivation and its exit condition once the bank is sufficient, run_pass banking under `source='cold-start'` with a local pulse and no ElevenLabs call, the quality gate still deciding what lands in the bank, and the learning state reaching the seats as "<name> (learning this voice)" with its stable-block explainer Also pins the insert-time label handoff: a finished verdict rides the message row at INSERT, so the round that renders the transcript in the same breath cannot read a stale pending head.
- (`test_room_commands.py`) Room-mode commands (#28, chat 198): "group mode, please" arms and "solo mode" disarms through the same never-awaited scan, the verdict-line allowlist grows the command outcomes, talk ABOUT the mode changes nothing, and the engine feeds seats the per-round room state
- (`test_room_identify.py`) Anchored identification (#28 phase 2): anchor-prefix requests with the roster+1 hint, name labels, cross-session re-identification, elimination and anchor accumulation, the unknown-voice ask-fallback, the mismatch flag that never mutates a label, tap-to-correct, roster/flag live events
- (`test_room_intro.py`) Introduction detection (#28 phase 2): the send-never-waits pin, the lexical prefilter gating utility spend, confirmed introductions/departures driving room mode and the roster, the cap, owner-anchor seeding, the third-field-test arming phrasings, alias capture, the per-scan verdict line, and the participant boundary (#65: an AI's name - spelt-by-ear variants included - never reaches the roster, and the seat writer refuses the exact names)
- (`test_room_mode.py`) Room mode's identity passes (#28): toggle-off byte-for-byte identity on the realtime relay, the commit-boundary tee, the never-awaited pass, the PR-B retirement pins (no EL call on solo or deferred turns; the crosstalk split alone is metered), exact turn-id labelling, and the live-events push
- (`test_room_remembered_first.py`) Remembered-first matching (#28, fourteenth field test): a sufficient remembered non-rostered person is named and rostered on their first armed-room utterance, the armed/ambient/speculative candidate lists share one construction, the roster cap still holds with the turn named anyway, a remembered match answers the open ask, and the generalised cold start banks to the one unidentifiable present person while everyone else is sufficient
- (`test_room_sniff.py`) The EL sniff's retirement (#28 PR-B): the sniff machinery is structurally gone, the eighth field test's false split/arm/name conditions fire zero EL calls, matcher-unavailable means manual-only arming, and the sniff's old job (a remembered voice arming a fresh chat) is covered by the ambient local check
- (`test_room_speculative.py`) Speculative identity at silence-start (#28 PR-B): the content-free hint sends no byte upstream, the check is never awaited, a fresh verdict labels the commit with one matcher call, a stale verdict re-checks, a cached match on a remembered non-rostered person is trusted and seats them (remembered-first, fourteenth field test), and a disarmed chat schedules nothing
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
- (`test_voice_health.py`) The voice health strip's backend (#28): GET /api/voice/health is content-free (states, counts, ms - never names), the matcher-state readout never triggers the warm, and the bounded per-chat last-decision record is written only inside the never-awaited passes
- (`test_voice_id.py`) Local speaker identification (#28): the pure identify/open-set/ambiguity/two-voice decision seam with synthetic vectors (close pairs widening the margin), the pairwise hygiene rules and bank audit, enrolment averaging cached by clip set with a mocked extractor, the pinned-model SHA-256 fetch-and-verify with no network, and the PR-B run_pass wiring (fast match and every defer skip the batch call; only "multi" runs the crosstalk split; disabled does nothing automatic). An integration test builds the real extractor when the model is present and skips cleanly when it is not
- (`test_voice_rounds.py`) Voice playback regression guards for the change that decoupled Claude Code guest execution from the turn lifecycle, and #64: a finished hand-back stays voiceable - active_round names the last round and its buffer replays in full then ends
- (`test_voice_trace.py`) Per-turn voice latency instrumentation
- (`test_voice_anchor_backup.py`) Learned voices ride the backup cycle (#33): every DB snapshot carries a voices tar that restores to a working store, owner-only permissions, family retention, and an empty store adds nothing
- (`test_voice_clips.py`) Clip audition and reassignment (#68/#90): metadata-only list newest first, audio served only for the person's own file tokens (traversal and cross-person refused), playback read-only for anchor state, single-clip delete recomputes sufficiency, last clip's deletion leaves the person known but unlearnt, owner-created people start anchor-pending (participant names refused, conflicts offer the existing person), a moved clip changes hands without touching disk (quarantine cleared, provenance stamped), and an alias joins identity names without renaming
- (`test_work_status.py`) Announce pending external work before the chat goes silent, using a STRUCTURED status event (a trusted activity label) rather than hardcoded filler text, and NEVER persisting it into a chat message or the model's own reply content

**Frontend** (`frontend/src/`)

- (`cacheHealth.test.js`) Tests for the Spend page's cache-health block: it must surface a per-seat
- (`captureProfile.test.js`) The mic capture-profile decision (#28 phase 4): solo constraints untouched, room mode drops the single-voice tuning, profile names match the relay's log allowlist
- (`commitLedger.test.js`) Only one transcription wins per committed utterance (#85/#104): a salvaged commit drops its late realtime final (the doubled-turn race), a final that beats the timer stands the salvage down, exact id matching with FIFO fallback for id-less finals, continuation commits carry their buffer dispatch, the pending window is bounded, reset clears the socket's flight
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
- (`roomState.test.js`) Room-mode roster/flag state (#28 phase 2): the "In the room" chip names exactly the present people, ask/mismatch copy never claims a label changed, the correction menu's options, remembered-voice summaries with the two-part learning bar and set-aside clips (PR-B), preferred display names (#28 phase 3), the dock's per-person chips - three honest states (remembered/part-learned/nothing banked), a tick beside the name rather than swapped label text, best-known-first order, and the room as the source when there is one - and the tray pin for the room button's retirement (#28): the tray renders no room on/off control, only the passive indicator, with the two demoted manual switches surviving in the settings tier
- (`runningState.test.js`) Tests for per-chat running-task state
- (`speculative.test.js`) The silence-start hint's firing rule (#28 PR-B): once per silence gap after the confirmation window, re-armed by resumed speech, never outside an utterance
- (`spendView.test.js`) Tests for the Spend page: it must lead with an honest answer, not a table
- (`streamGuard.test.js`) Invariant test for the cross-chat write-guard
- (`textQueue.test.js`) Invariant tests for per-chat text batching + pre-ingestion cancel
- (`turnPolicy.test.js`) Bounded active-turn endpointing (#60): a normal-length turn never forces regardless of voicing, an unvoiced frame past the soft cap forces now (the continuous-noise fix), a voiced frame past the soft cap holds to avoid premature truncation, and the hard cap forces unconditionally either way; plus #104's additions - commit patience scales with committed audio (floor/ceiling pinned) and the total-turn bound outlasts the per-segment caps
- (`voiceChips.test.js`) Room-mode voice chips: user turns only, malformed label data renders nothing, ordinal assignment is first-seen and stable, named chips carry per-label uncertainty and correction state, and the owner's voice-confirmed marker (#28 PR-C) and the cold-start learning marker (#28) are additive-only so old payloads render unchanged - one shared rule decides the tick, the "· learning" tag and the bare "?"
- (`voiceClips.test.js`) Clip audition rows (#68/#90): every capture path has a plain-English label, unknown sources render verbatim rather than guessed, only elimination-earned clips get the closer-listen flag (owner-reassigned ones drop it and say so), move targets are every other person by display name, row derivation keeps the file token and formats duration, and the delete explainer says what actually happens
- (`voiceErrors.test.js`) Playback failure messages are user-readable and name the recovery
- (`voiceGate.test.js`) Pure-function tests for the voice playback gate (a regression guard)
- (`voiceHealth.test.js`) The voice health strip's derivations (#28): matcher-state readouts (degraded states say turns stay unnamed and arming is manual - never a cloud-fallback promise, which retired in PR-B), the mode readout that IS the tray's room indicator since the room button became one (#28) - "room on · N" / "listening" / "solo", each with its one-sentence automation tooltip, and a degraded matcher making the listening tooltip honest about arming - the live pulse's exact "local · 227ms" formatting with 'pending' only during a live session, known-voice progress lines, close-pair warnings, the bounded chip row with its "+N" overflow, and the mobile call screen's collapsed one-liner ("Listening · Alex ✓ +1")
- (`voiceRecovery.test.js`) Tests for voice session recovery: a session must repair itself when the tab
- (`voiceTrace.test.js`) Pure-function tests for the client-side voice latency trace
- (`voiceView.test.js`) Tests for the voice call screen's state rules
