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
- **API keys are NOT settings.** They live in `.env` only (`ANTHROPIC_API_KEY`,
  `OPENAI_API_KEY`, `ELEVENLABS_API_KEY`, `TAVILY_API_KEY`, `BRAVE_API_KEY`,
  `GITHUB_TOKEN`, `CLAUDE_CODE_OAUTH_TOKEN`) and never reach the browser.

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
| `log_level` | `""` | Verbosity for the app's own `crossband.*` loggers. Empty = WARNING+. Set `INFO` for a deliberate cache-telemetry sampling session ([COST_TELEMETRY.md](COST_TELEMETRY.md)), then unset. |
| `shutdown_timeout_s` | `15` | Seconds a stop may wait on work still in flight (a reply mid-generation, a live voice call) before cancelling it and exiting anyway. The live-events watcher connections end at once regardless. Raise it so long rounds always finish, lower it for a snappier deploy loop ([OPERATIONS.md](OPERATIONS.md)). |

## Models

| key | default | what it does |
|---|---|---|
| `anthropic_model` | `claude-opus-4-8` | Model for the default Claude seat (first-run seed; after that, edit seats in-app on the Models page). |
| `openai_model` | `gpt-5.1` | Model for the default GPT seat (same seed rule). |
| `utility_model` | `claude-haiku-4-5` | The cheap model behind rolling summaries, auto-titles and project distillation. `gpt-*` values route to OpenAI. |
| `pricing` | built-in rate card | Per-model `{input, output}` $/1M-token prices with provenance metadata. Matched **exactly** by model id, then by an entry's explicit `aliases` list, then by a date/build-stamped reissue of the same model. There is no broad family fallback, so a new model the card doesn't know stays *unpriced/unknown* (surfaced, not silently charged as an older family) until you add it. Each entry may carry provider-specific `cache: {read_mult, write_mult}` terms (defaults to Anthropic's). **Prefer the Models page's "Model prices" section** (it writes this block for you, and validates what a hand edit cannot): an estimate needs an http(s) source and a real ISO `as_of` (so it can be re-checked later), rates are bounded (a misplaced decimal is refused as a typo, not stored as a cost basis), `aliases` must be exact ids (no globs, and no collision with another priced model), and only `rate_card_estimate` or `self_hosted_zero_marginal` may be declared, since `provider_reported`/`subscription_equivalent` are recorded per turn from what a provider actually returned and must never be assertable by hand. It also declares a local/self-hosted **$0** for you (no rates needed) and keeps a `.bak` of this file before each write. Editing by hand still works and bypasses all of the above, so it is on you; entries **layer over** the built-in table one model at a time, so pricing your own model leaves every other card intact. (An earlier build let a hand-added block silently REPLACE the whole table, which meant pricing one model unpriced every other one. That is fixed, and it is why the in-app editor exists.) |

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
| `attribution_audit` | `true` | After each completed reply, run a privacy-safe diagnostic that notes when a model's "you said…" claim isn't found word-for-word in your raw messages in the current window. It only writes a **content-free** log line (a one-way fingerprint of the claim plus lengths/offsets, never the conversation text) and never blocks or edits a reply. A "no verbatim match" is a signal for review, **not** a verdict that the model made something up, because it can also fire when the moment was summarised or paraphrased. Set `false` to turn the diagnostic off. |

## Voice

| key | default | what it does |
|---|---|---|
| `tts_model` | `eleven_flash_v2_5` | ElevenLabs streaming TTS model. |
| `tts_speed` | `1.0` | Speaking speed, 0.7–1.2. (Playback speed also has a live slider in the voice dock.) |
| `stt_model` | `scribe_v2` | Transcription model; realtime variant is used automatically when available. |
| `voice_pricing` | built-in | ElevenLabs rate card used to price TTS/STT usage. |

## Memory (companion service)

| key | default | what it does |
|---|---|---|
| `memory_url` | `http://127.0.0.1:8901` | Where to probe for [Membro](https://github.com/shawn-durrani/membro). Present → memory features light up; absent → fully functional memoryless. Re-probed every 30s, so start order doesn't matter. |

## Coding guest + GitHub (the `code` toggle)

The short user-facing description is
[README § What it does](../README.md#what-it-does); the security bounds, in
both modes, are [GUEST_PERMISSIONS.md](GUEST_PERMISSIONS.md); change the
allow/deny lists only together with that document.

| key | default | what it does |
|---|---|---|
| `code_repos` | `{}` | Short name → local path a guest may open. **Empty = the whole feature is dark.** |
| `code_mcp` | `{}` | MCP servers mounted into the guest (e.g. Membro for recall): name → `{command, args, env}`. For Membro, `env` must carry `{"PYTHONPATH": "<membro checkout>"}` because it runs from its checkout and is never pip-installed; without it the server dies at spawn and the guest arrives with the tool missing. `${VAR}` tokens in `env` values resolve from Crossband's own environment at guest launch. Mounted in BOTH modes, allowed whole (every tool the server exposes). Full worked example: [GUEST_PERMISSIONS.md](GUEST_PERMISSIONS.md). |
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
| `mcp_servers` | `{}` | MCP servers the resident MODELS may call: name → `{command, args, label?}` (stdio). Private by placement, so configure it in `config.local.json`. Optional `label` is a trusted, operator-written display string shown in the work-status chip while that server is in flight, e.g. `"Checking job listings"`; omitted servers get a generic "Working on it" fallback, never a guess. |
| `ingest_token` | `""` | Bearer token for `POST /api/ingest`. Empty = loopback-trust only; set one only if a producer posts from beyond loopback. |
| `slash_commands` | `[]` | Composer suggestion chips for `/` messages: `{insert, label, hint}`. Crossband assigns no meaning to any command: `/` messages go to your tooling, and no model replies. |

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

## Backups

| key | default | what it does |
|---|---|---|
| `backup_keep` | `14` | Snapshots retained in `data/backups/`. |
| `backup_interval_hours` | `6` | Snapshot cadence (plus one at every startup, before anything touches the DB). |
| `backup_mirror_dir` | `""` | Optional second directory that receives **completed** snapshots only, never the live DB, because sync daemons watching a live WAL cause lock hangs. |
| `backup_mirror_keep` | `7` | Snapshots retained in the mirror. |

## Startup behaviour

| key | default | what it does |
|---|---|---|
| `require_keys` | `false` | `true` = abort startup on missing provider keys instead of degrading. Either way every missing key is named loudly at startup. |

---

*This reference is code-derived: the source of truth is
`backend/config.py::Settings`, and a test
(`tests/test_supervisor_plist.py::test_docs_index_and_config_reference_stay_complete`)
fails CI if a setting exists that this page doesn't mention, so the table
above cannot silently fall behind the code. The same test checks that
`docs/README.md` links every document in `docs/`.*
