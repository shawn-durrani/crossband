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
- (`test_memory_client.py`) Memory client degradation
- (`test_models_status.py`) GET /api/models/status
- (`test_openai_client.py`) Keyless-local edge for the OpenAI-compatible adapter
- (`test_prewarm.py`) Ambient recall fires at speech-end, the round adopts it only on a match
- (`test_pricing_api.py`) Operator-editable rate cards
- (`test_projection.py`) Characterization tests for the transcript projection, covering the load-bearing invariants
- (`test_prompt_guardrail.py`) High-salience personal-claim guardrail
- (`test_reasoning_policy.py`) The reasoning-effort policy must be AUTHORITATIVE at the actual request-kwargs level, not just in the translation helpers (tests/test_effort.py covers those in isolation)
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
- (`runningState.test.js`) Tests for per-chat running-task state
- (`spendView.test.js`) Tests for the Spend page: it must lead with an honest answer, not a table
- (`streamGuard.test.js`) Invariant test for the cross-chat write-guard
- (`textQueue.test.js`) Invariant tests for per-chat text batching + pre-ingestion cancel
- (`voiceErrors.test.js`) Playback failure messages are user-readable and name the recovery
- (`voiceGate.test.js`) Pure-function tests for the voice playback gate (a regression guard)
- (`voiceRecovery.test.js`) Tests for voice session recovery: a session must repair itself when the tab
- (`voiceTrace.test.js`) Pure-function tests for the client-side voice latency trace
- (`voiceView.test.js`) Tests for the voice call screen's state rules
