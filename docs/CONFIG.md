# Configuration: every setting, in one place

Crossband is configured in **four layers**, each overriding the last
(`backend/config.py::load_settings`):

```
defaults (in code)  ←  config.json (committed)  ←  config.local.json (gitignored)  ←  CROSSBAND_* environment
```

- **`config.json`**: committed, shared defaults. Only put values here that
  make sense for everyone.
- **`config.local.json`**: yours, next to `config.json` in the repo root,
  gitignored. Machine paths, repo names, private MCP servers, anything the
  public repo must never learn. Copy
  [`config.local.json.example`](../config.local.json.example) to start.
- **Environment**: any setting can be overridden as `CROSSBAND_<NAME>` (upper-case
  the key: `CROSSBAND_PORT=9000`, `CROSSBAND_USER_NAME=Alex`). Dict-valued settings take
  JSON. Unparseable values are ignored rather than crashing startup.
- **API keys are NOT settings.** They live in `.env` only and never reach
  the browser: `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `ELEVENLABS_API_KEY`,
  `TAVILY_API_KEY`, `BRAVE_API_KEY`, `REDDIT_CLIENT_ID`,
  `REDDIT_CLIENT_SECRET`, `GITHUB_TOKEN` or `GH_TOKEN`, and
  `CLAUDE_CODE_OAUTH_TOKEN`. A seat added from a one-click preset takes its
  own provider key, listed with that preset in
  [MODELS.md](MODELS.md).

A malformed config file never bricks startup. It reads as empty and the
layer is skipped. Unknown keys are ignored.

Restart the service after changing a file (`./start.sh`, or
`launchctl kickstart -k gui/$(id -u)/dev.crossband.server` under the
supervisor, see [OPERATIONS.md](OPERATIONS.md)).

## Server

| key | default | what it does |
|---|---|---|
| `host` | `127.0.0.1` | Bind address. The process **refuses to start** on any non-loopback host; see [SECURITY.md](../SECURITY.md). |
| `port` | `8902` | The one port everything is served on. |
| `data_dir` | `""` | Where SQLite + backups + logs live. Empty → `<repo>/data`. |
| `trusted_hosts` | `""` | Extra Host headers to accept (comma-separated), for Tailscale serve: `my-mac.my-tailnet.ts.net`. Empty = loopback only. [REMOTE_ACCESS.md](REMOTE_ACCESS.md). |
| `recovery_secret` | `""` | Gates first-run password enrolment and reset, never the everyday login. Empty mints a fresh random secret each start, printed only while no password is enrolled. Set it in `.env` so reset works without terminal access. [SECURITY.md](../SECURITY.md). |
| `log_level` | `""` | Verbosity for the app's own `crossband.*` loggers. Empty = WARNING+. Set `INFO` for a deliberate cache-telemetry sampling session ([COST_TELEMETRY.md](COST_TELEMETRY.md)), then unset. |
| `shutdown_timeout_s` | `15` | Seconds a stop waits on work in flight before exiting anyway. Live-events connections end at once regardless. Raise it so long rounds finish, lower it for a snappier deploy loop. [OPERATIONS.md](OPERATIONS.md). |

## Models

| key | default | what it does |
|---|---|---|
| `anthropic_model` | `claude-opus-4-8` | Model for the default Claude seat (first-run seed; after that, edit seats in-app on the Models page). |
| `openai_model` | `gpt-5.1` | Model for the default GPT seat (same seed rule). |
| `utility_model` | `claude-haiku-4-5` | The cheap model behind rolling summaries, auto-titles and project distillation. `gpt-*` values route to OpenAI. |
| `pricing` | built-in rate card | Per-model `{input, output}` $/1M prices with provenance. Matched by exact model id, then an entry's `aliases`, then a date-stamped reissue of the same model. No family fallback: an unknown model stays unpriced. See below. |

### Pricing a model

Set prices on the Models page rather than by hand. It validates what a text
editor cannot: an estimate needs a checkable source and a real date, rates are
bounded so a misplaced decimal is refused, and aliases must be exact model ids.
It writes the block for you and keeps a `.bak`.

Hand edits still work and skip every check, so the figures are yours to stand
behind. Entries layer over the built-in table one model at a time, so pricing
your own model leaves every other card intact. [MODELS.md](MODELS.md) covers
the whole procedure, including the local `$0` case.

## Identity / display

| key | default | what it does |
|---|---|---|
| `user_name` | `User` | How the models address you, and the name in `[cut off by …]` markers. |
| `claude_display_name` | `Claude` | Seeds the default roster **on first run only**; afterwards names live in the participants table (edit in-app). |
| `gpt_display_name` | `GPT` | Same. |

## Conversation shaping

| key | default | what it does |
|---|---|---|
| `max_response_tokens` | `16000` | Per-reply output cap. |
| `summary_threshold_chars` | `60000` | Conversation weight that triggers the rolling summary. Counts message text **plus attachments** (images priced at the resolution providers actually tokenise, files by length), expressed in character-equivalents so this number keeps its original meaning. It measured text only until 2026-08-02, so photo-heavy chats never folded. |
| `keep_recent_messages` | `12` | Messages always kept verbatim below the summary. |
| `max_attachment_mb` | `20` | Upload size cap, applied to the file as you send it. Photos are downscaled to ~1568px on arrival, so what gets stored, and re-sent to every participant on every turn, is typically a tenth of this. |
| `attribution_audit` | `true` | Flags when a model's "you said…" or "Claude said…" claim has no word-for-word match in that speaker's turns in the current window. Shows a quiet chip on the reply and writes one content-free log line; never blocks or edits a reply. See below. |
| `echo_guard` | `true` | Drops a text reply that mostly restates the seat's own previous message or a reply already given this round: one retry with the reason stated, then suppression. Quotes, short agreements, repeat requests, tool replies and paraphrase are exempt; voice rounds only log. |
| `citation_check` | `true` | Flags a "the docs say…" claim in a reply that ran no tools, on the audit's chip surface. The claim may still be right from memory, so the chip says unverified. Never a retry or a block. |

### Reading an attribution-audit flag

A "no verbatim match" is a signal for review, not a verdict that the model
made something up. It also fires when the moment was summarised or
paraphrased. Claims are checked against the named speaker's own turns: "you
said…" against yours, "GPT said…" against GPT's. A flagged claim renders as
an amber chip under the reply, quoting the claim, since your own transcript
already holds the text. The log line stays content-free: a one-way
fingerprint of the claim plus lengths and offsets, at WARNING so a default
deploy records it.

## Voice

| key | default | what it does |
|---|---|---|
| `voice_provider` | `auto` | STT/TTS engine selection. `auto` (default) is exactly today: ElevenLabs when `ELEVENLABS_API_KEY` is set, no voice when it is not; `elevenlabs` makes that choice explicit. `local` is reserved for a local engine (no cloud egress) and, until one lands, selects nothing. |
| `tts_model` | `eleven_flash_v2_5` | ElevenLabs streaming TTS model. |
| `tts_speed` | `1.0` | Speaking speed, 0.7–1.2. (Playback speed also has a live slider in the voice dock.) |
| `stt_model` | `scribe_v2` | Transcription model; realtime variant is used automatically when available. |
| `voice_pricing` | built-in | ElevenLabs rate card used to price TTS/STT usage. |
| `room_roster_max` | `6` | Room mode: how many people the roster may hold at once (the cap frees as people leave). A product choice, not a technical limit. An explicit 0 seats no guests; the owner's tap-correction still seats. |
| `voice_id_enabled` | `true` | Room mode's offline local speaker matcher, and the only identity path. Off, or with `sherpa-onnx` or the model file absent, turns are not named and the room never arms automatically. See below. [VOICE_ID.md](VOICE_ID.md). |
| `voice_id_threshold` | `0.5` | Cosine similarity a voice must reach to be named. Calibrated for the bundled model (same-speaker ≈0.63–0.73 vs a stranger ≈0.12–0.31, so 0.5 sits in the gap). Raise it to name fewer, more certain matches; lower it to name more, less certainly. |
| `voice_id_margin` | `0.12` | How clearly the best match must beat the runner-up before it is claimed. The similar-voice-household knob: the hygiene guard also widens it automatically for any two stored voices it finds sitting close together. |
| `voice_id_pending_extra` | `0.08` | How much the naming bar rises while anyone on the roster is unlearnt. Protects a new guest from having their turns claimed by a similar-sounding regular. `0` = off. [VOICE_ID.md](VOICE_ID.md). |
| `voice_id_banking_extra` | `0.1` | How much higher than the naming bar a match must score before its audio is stored as an anchor clip. Keeps borderline matches from feeding the bank that produced them. `0` = off. [VOICE_ID.md](VOICE_ID.md). |
| `voice_id_sufficient_seconds` | `6.0` | Accepted seconds of clear speech a person's stored voice needs before identification trusts it. Below the bar their turns stay uncertain. |
| `voice_id_min_short_clips` | `2` | The second half of the sufficiency bar (#28 PR-B): how many short (~1–2s) clips the stored voice must include, so quick interjections can be recognised, not just full sentences. |
| `voice_id_model_url` | `""` | Override the local speaker model's download URL. Empty uses the built-in pinned URL. Pin the **hash too**: a URL override checked against the default hash just fails verification and the matcher stays unavailable. |
| `voice_id_model_sha256` | `""` | Override the local speaker model's pinned SHA-256. Empty uses the built-in pin. The model is fetched **once** to `<data_dir>/voice_models/`, verified against this hash before use, and never committed. |

### When the matcher is off or missing

A known voice is named on-device in a fraction of a second. A voice the
matcher cannot place stays unnamed. The only ElevenLabs batch call left runs
when voices overlap, to split crosstalk.

With `voice_id_enabled` false, or the `sherpa-onnx` wheel or model file
absent, turns are not named and the room never arms on its own. Introductions,
spoken commands and the switch in the voice settings still arm it by hand.

## Memory (companion service)

| key | default | what it does |
|---|---|---|
| `memory_url` | `http://127.0.0.1:8901` | Where to probe for [Membro](https://github.com/shawn-durrani/membro). Present → memory features light up; absent → fully functional memoryless. Re-probed every 30s, so start order doesn't matter. |
| `MEMORY_AUTH_TOKEN` (env, not a config key) | unset | Membro's owner token, sent as a bearer on its `/search` and on the job-status polls behind imports. One token, put once in Crossband's `.env`. Without it `search_history` reports a failure rather than reading as "no history". See below. |

### The memory token

Membro's `/recall` and `/summary` answer an unauthenticated loopback caller.
Its `/search`, the verbatim transcript search behind the `search_history`
tool, is owner-gated even on loopback, and so are its job-status polls.
The same rule covers membro's MCP server: `search_history` there works
only when the token was passed at registration (membro#81). The example
guest mount passes no token on purpose, so a summoned guest keeps
recall, summary and save and loses verbatim search; hand a guest the
token only as a deliberate choice. `code_mcp`'s `membro-admin` entry
resolves the same variable via `${MEMORY_AUTH_TOKEN}`, so one token in
Crossband's `.env` serves every path. See
[GUEST_PERMISSIONS.md](GUEST_PERMISSIONS.md).

### The memory contract

Crossband reads membro's `contract_version` from `/v1/health` and needs
major 1. A different major is treated as no memory at all. Minor
versions are additive, and each newer field is used only when membro
sends it, so an older membro keeps the behaviour of the version before.

- 1.2: `speaker_identity` beside a guest turn on ingest.
- 1.3: `web_sources` on ingest and on saved facts, so web-derived facts
  are held for review.
- 1.4: `web_sources` comes back on `search_history` hits, and a hit
  that carries any gets the untrusted marker a live fetch gets.
  `browser_origin` on `/v1/health` is where a phone's browser reaches
  membro, and the voice-discard eraser link uses it. Before each
  handoff, crossband reads membro's per-chat watermark and winds its
  own `ingested_upto` back when membro holds less, which is what a
  restore from backup leaves behind. A saved fact's `event_date` is the
  owner's local calendar day, and membro anchors it to local midnight.
- 1.5: `guest_speakers` on a saved fact. In room mode a model's direct
  `save_memory` carries the guests present in the round, as the same
  `guest:<name>` and `guest:unknown` classes ingest uses, and membro
  holds the save for review under its guest-present group. Outside room
  mode, or with the owner alone, the field is not sent.

On a 1.3 membro the marker never appears, the eraser link falls back to
the browser's own host on port 8901, and the watermark route is never
called. On a 1.4 membro the guest stamp is sent and ignored, so such a
save goes straight to recall, and crossband logs one warning per
process.

## Coding guest + GitHub (the `code` toggle)

The short user-facing description is
[README § What it does](../README.md#what-it-does); the security bounds, in
both modes, are [GUEST_PERMISSIONS.md](GUEST_PERMISSIONS.md); change the
allow/deny lists only together with that document.

| key | default | what it does |
|---|---|---|
| `code_repos` | `{}` | Short name → local path a guest may open. **Empty = the whole feature is dark.** |
| `code_mcp` | `{}` | MCP servers mounted into the guest: name → `{command, args, env}`. Mounted in BOTH modes and allowed whole, every tool the server exposes. Worked example, including the required `PYTHONPATH` for Membro: [GUEST_PERMISSIONS.md](GUEST_PERMISSIONS.md). |
| `github_repos` | `{}` | Name → `owner/repo` the AIs may read and file issues against. Auth: `GITHUB_TOKEN` env, else the machine's logged-in `gh` CLI. |
| `code_use_api_key` | `false` | `false` = guest turns ride the machine's Claude Code login (subscription). `true` = bill `ANTHROPIC_API_KEY` per token. Either way the turn records which one actually paid. |
| `code_model` | `default` | Guest model tier: `default`/`opus`/`sonnet`/`haiku`. Per-summon override allowed. Changes the rate, not the account that pays. |
| `code_effort` | `default` | Guest thinking level: `default`/`think`/`think-hard`/`ultrathink`. Per-summon override allowed. |
| `code_allow_writes` | `false` | Implement mode: the guest may branch, test, push and open a PR, but never merge and never push `main`. Off = read-only investigation. |
| `code_default_on` | `false` | New chats start with the `code` toggle already on (harmless without `code_repos`). |
| `code_max_turns` | `50` | SDK turn cap for one read-only visit. |
| `code_timeout_s` | `600` | Wall-clock cap for one read-only visit. |
| `code_impl_max_turns` | `150` | Turn cap for an implement-mode visit. |
| `code_impl_timeout_s` | `1800` | Wall-clock cap for an implement-mode visit. |

## External integrations

| key | default | what it does |
|---|---|---|
| `mcp_servers` | `{}` | MCP servers the resident MODELS may call: name → `{command, args, label?}` (stdio). Configure it in `config.local.json`. Optional `label` shows in the work-status chip while that server is in flight; omitted servers get a generic "Working on it". |
| `ingest_token` | `""` | Bearer for the machine side-channel, `POST /api/ingest` and `POST /api/chats/{id}/notice` (env: `CROSSBAND_INGEST_TOKEN`). Once a password is enrolled it is the only way a producer reaches either route. Empty keeps the historical posture. [PRODUCERS.md](PRODUCERS.md). |
| `slash_commands` | `[]` | Composer suggestion chips for `/` messages: `{insert, label, hint}`. Crossband assigns no meaning to any command: `/` messages go to your tooling, and no model replies. The full producer contract is [PRODUCERS.md](PRODUCERS.md). |
| `slash_ack_timeout_s` | `120` | Dead-man for `/` messages. If nothing acks a slash command within this window, one system line says nothing picked it up, so a stopped watcher stops looking like a queued deploy. `0` = off. [PRODUCERS.md](PRODUCERS.md). |

## Research tool caps

| key | default | what it does |
|---|---|---|
| `search_timeout` | `20` | Web search timeout (s). |
| `fetch_timeout` | `15` | Page fetch timeout (s). |
| `max_tool_output` | `8000` | Chars of tool output returned to the calling model. |
| `tool_log_chars` | `1200` | Chars per tool event when replayed into later transcripts. |
| `max_tool_rounds` | `6` | Tool-call loop cap per reply. |
| `max_transcript_chars` | `100000` | YouTube transcript in-chat cap. |
| `max_audio_mb` | `60` | `transcribe_audio_url` download cap. |
| `max_search_results` | `5` | Results per search. |
| `egress_max_transfer_mb` | `64` | Per-connection byte backstop at the egress proxy. Keep it at or above `max_audio_mb`; podcast audio rides the same path. |
| `egress_politeness_s` | `2` | Minimum spacing between connection BURSTS to the same host. One page load's subresource connects count as a single burst. |
| `egress_idle_timeout_s` | `60` | An egress connection with no bytes moving for this long is closed. |
| `egress_tunnel_lifetime_s` | `300` | Hard wall clock on one egress connection. |
| `fetch_max_page_mb` | `10` | `fetch_page` decoded-body cap; a bigger page errors instead of ballooning in RAM. |
| `browse_timeout_s` | `20` | Wall clock for one rendered view (`view_page`), worker process included. Rendering needs Playwright plus `.venv/bin/playwright install chromium` (~160MB); absent either, the tool is simply not offered. |
| `browse_page_budget_mb` | `30` | Total bytes one rendered page load may pull across all its connections, subresources included. `0` turns the budget off, leaving per-connection caps only. |
| `browse_sandbox` | `true` | On macOS, wrap the render worker in an OS sandbox profile: network limited to the proxy port, writes to its throwaway profile folder, no reads of the data directory, `.env` or `~/.ssh`. Defence in depth; refused profiles render as before. |

## Backups

| key | default | what it does |
|---|---|---|
| `backup_keep` | `14` | Snapshots retained in `data/backups/`. Each cycle writes the database (`chat-<stamp>.db`) AND the learned voices (`voices-<stamp>.tar` of `voice_anchors/`, #33) - restore the voices by untarring into `data/`. |
| `backup_interval_hours` | `6` | Snapshot cadence (plus one at every startup, before anything touches the DB). |
| `backup_mirror_dir` | `""` | Optional second directory that receives **completed** snapshots only, never the live DB, because sync daemons watching a live WAL cause lock hangs. |
| `backup_mirror_keep` | `7` | Snapshots retained in the mirror. |

## Startup behaviour

| key | default | what it does |
|---|---|---|
| `require_keys` | `false` | `true` = abort startup on missing provider keys instead of degrading. Either way every missing key is named loudly at startup. |

---

*The source of truth is `backend/config.py::Settings`. A test
(`tests/test_supervisor_plist.py::test_docs_index_and_config_reference_stay_complete`)
fails CI if a setting exists that this page doesn't name, so no setting can go
missing. The descriptions are hand-written. The same test checks that
`docs/README.md` links every document in `docs/`.*
