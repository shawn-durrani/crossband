# Configuration: every setting, in one place

Crossband reads its settings from four layers, and each layer overrides
the one before it. The order is the defaults in code, then
`config.json`, then `config.local.json`, then any `CROSSBAND_*`
environment variable.

```
defaults (in code)  <  config.json (committed)  <  config.local.json (gitignored)  <  CROSSBAND_* environment
```

`config.json` is committed and holds the defaults everyone shares, so
only put a value there that makes sense for everyone.
`config.local.json` is yours. It sits next to `config.json` in the repo
root, it's gitignored, and it holds machine paths, repo names, private
MCP servers and anything the public repo must never learn. Copy
[`config.local.json.example`](../config.local.json.example) to start.
Any setting can also be set in the environment as `CROSSBAND_<NAME>`,
with the key in capitals, such as `CROSSBAND_PORT=9000` or
`CROSSBAND_USER_NAME=Alex`. A setting that holds a dict takes JSON, and
a value that can't be parsed is ignored, so a typo never stops the app
starting.

API keys aren't settings. They live in `.env` only and never reach the
browser: `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `ELEVENLABS_API_KEY`,
`TAVILY_API_KEY`, `BRAVE_API_KEY`, `REDDIT_CLIENT_ID`,
`REDDIT_CLIENT_SECRET`, `GITHUB_TOKEN` or `GH_TOKEN`, and
`CLAUDE_CODE_OAUTH_TOKEN`. A seat added from a one-click preset takes
its own provider key, listed with that preset in
[MODELS.md](MODELS.md).

A broken config file never stops the app starting. It reads as empty
and that layer is skipped, and unknown keys are ignored. Restart the
service after you change a file, with `./start.sh`, or with
`launchctl kickstart -k gui/$(id -u)/dev.crossband.server` under the
supervisor as [OPERATIONS.md](OPERATIONS.md) describes.

## Server

| key | default | what it does |
|---|---|---|
| `host` | `127.0.0.1` | The address the app binds. It refuses to start on any non-loopback host, as [SECURITY.md](../SECURITY.md) explains. |
| `port` | `8902` | The one port everything is served on. |
| `data_dir` | `""` | Where the database, the backups and the logs live. Empty means `<repo>/data`. |
| `trusted_hosts` | `""` | Extra Host headers to accept, comma-separated, for Tailscale serve, such as `my-mac.my-tailnet.ts.net`. Empty means loopback only. [REMOTE_ACCESS.md](REMOTE_ACCESS.md). |
| `funnel_check_s` | `180` | How often the app asks `tailscale serve status --json` whether Funnel has its port on the public internet. While it does, the app serves nothing but a page that says so. `0` turns the check off. [REMOTE_ACCESS.md](REMOTE_ACCESS.md). |
| `tailscale_identity_required` | `true` | A request on a trusted host must carry the `Tailscale-User-Login` header Tailscale adds for tailnet users, and one without it came in through Funnel and is refused. Turn it off only for a trusted host that isn't Tailscale serve. |
| `recovery_secret` | `""` | Gates first-run password enrolment and reset, never the everyday login. Empty mints a fresh random secret each start, printed only while no password is enrolled. Set it in `.env` so a reset works without the terminal. [SECURITY.md](../SECURITY.md). |
| `log_level` | `""` | How much the app's own `crossband.*` loggers write. Empty means warnings and above. Set `INFO` for a cache-telemetry sampling session, as [COST_TELEMETRY.md](COST_TELEMETRY.md) describes, then unset it. |
| `shutdown_timeout_s` | `15` | Seconds a stop waits on work in flight before exiting anyway. Live-events connections end at once regardless. Raise it so long rounds finish, or lower it for a snappier deploy loop. [OPERATIONS.md](OPERATIONS.md). |

## Models

| key | default | what it does |
|---|---|---|
| `anthropic_model` | `claude-opus-4-8` | The model for the default Claude seat. It seeds the seat on first run, and after that you edit seats on the Models page. |
| `openai_model` | `gpt-5.1` | The model for the default GPT seat, with the same seed rule. |
| `utility_model` | `claude-haiku-4-5` | The cheap model behind rolling summaries, auto-titles, project distillation, and the one call that reads every message you send for an instruction. That covers typed and spoken turns, with room mode on or off, and skips a `/` message. A `gpt-*` value routes to OpenAI. |
| `pricing` | the built-in rate card | Per-model `{input, output}` prices per million tokens, with provenance. Matched by exact model id, then an entry's `aliases`, then a date-stamped reissue of the same model. There's no family fallback, so an unknown model stays unpriced. |

### Pricing a model

Set prices on the Models page, which checks what a text editor can't:
an estimate needs a checkable source and a real date, rates are bounded
so a misplaced decimal is refused, and aliases must be exact model ids.
It writes the block for you and keeps a `.bak`.

Hand edits still work and skip every check, so the figures are yours to
stand behind. Entries layer over the built-in table one model at a
time, so pricing your own model leaves every other card intact.
[MODELS.md](MODELS.md#pricing-a-model-in-the-app) covers the whole
procedure, including the local `$0` case.

## Identity and display

| key | default | what it does |
|---|---|---|
| `user_name` | `User` | How the models address you, and the name in a `[cut off by …]` marker. |
| `claude_display_name` | `Claude` | Seeds the default roster on first run only. After that, names live in the participants table and you edit them in the app. |
| `gpt_display_name` | `GPT` | The same, for the GPT seat. |

## Conversation shaping

| key | default | what it does |
|---|---|---|
| `max_response_tokens` | `16000` | The output cap per reply. |
| `summary_threshold_chars` | `60000` | The conversation weight that triggers the rolling summary, counted in character equivalents. It counts message text and attachments, with images weighed at the resolution providers tokenise and files by length. |
| `keep_recent_messages` | `12` | Messages always kept word for word after the summary. |
| `max_attachment_mb` | `20` | The upload size cap, applied to the file as you send it. A photo is scaled down to about 1568 pixels on arrival, so what's stored and sent again on every turn is far smaller. |
| `attribution_audit` | `true` | Flags a reply's claim about who said what, like "you said…", "I said…" or "only Claude said…", when the transcript doesn't match it. It shows a quiet chip on the reply and writes one content-free log line, and never blocks or edits a reply. |
| `echo_guard` | `true` | Drops a text reply that mostly restates the seat's own previous message or a reply already given this round: one retry with the reason stated, then suppression. Quotes, short agreements, repeat requests, tool replies and paraphrase are exempt, and voice rounds only log. |
| `citation_check` | `true` | Flags a "the docs say…" claim in a reply that ran no tools, on the same chip. The claim may still be right from memory, so the chip says unverified. Never a retry and never a block. |

### Reading an attribution-audit flag

The audit checks each claim in a reply about who said what against the
turns the model could see. "You said…" is checked against your turns,
"GPT said…" against GPT's, and a seat's own "I said…" or "I meant…"
against that seat's. Each of those is flagged when there's no
word-for-word match. An "only Claude said…" claim is checked the other
way round, against everyone else's turns, and it's flagged when someone
else used the same words.

A flag is a signal for you to check, and never a verdict that the model
made something up. A missing match also shows up when the moment was
summarised or paraphrased, and an "only" flag shows up when two people
used the same stock phrase. A flagged claim renders as an amber chip
under the reply quoting the claim, since your own transcript already
holds the text. The log line holds no content, only a one-way
fingerprint of the claim with lengths and offsets, at warning level so
a default install records it.

## Voice

| key | default | what it does |
|---|---|---|
| `voice_provider` | `auto` | Which engine hears and speaks. `auto` uses ElevenLabs when `ELEVENLABS_API_KEY` is set and no voice when it isn't, and `elevenlabs` makes that choice explicit. `local` is reserved for a local engine and, until one lands, selects nothing. |
| `tts_model` | `eleven_flash_v2_5` | The ElevenLabs streaming text-to-speech model. |
| `tts_speed` | `1.0` | Speaking speed, from 0.7 to 1.2. Playback speed also has a live slider in the voice dock. |
| `stt_model` | `scribe_v2` | The transcription model. The realtime variant is used on its own when available. |
| `voice_pricing` | built in | The ElevenLabs rate card used to price speech in and out. |
| `room_roster_max` | `6` | How many people the room's roster may hold at once. The cap frees as people leave. An explicit `0` seats no guests, and your tap-correction still seats. |
| `voice_id_enabled` | `true` | Room mode's offline local speaker matcher, and the only identity path. Off, or with `sherpa-onnx` or the model file absent, turns are not named and the room never arms on its own. [VOICE_ID.md](VOICE_ID.md). |
| `voice_id_threshold` | `0.5` | The cosine similarity a voice must reach to be named. On the bundled model the same speaker scores about 0.63 to 0.73 and a stranger about 0.12 to 0.31, so 0.5 sits in the gap. Raise it to name fewer matches. |
| `voice_id_margin` | `0.12` | How clearly the best match must beat the runner-up before it's claimed. The knob for a house with similar voices, and the hygiene guard also widens it on its own for any two stored voices it finds close together. |
| `voice_id_pending_extra` | `0.08` | How much the naming bar rises while anyone on the roster is unlearnt. It protects a new guest from having their turns claimed by a similar-sounding regular. `0` turns it off. [VOICE_ID.md](VOICE_ID.md). |
| `voice_id_banking_extra` | `0.1` | How much higher than the naming bar a match must score before its audio is stored as an anchor clip. It keeps borderline matches from feeding the bank that produced them. `0` turns it off. [VOICE_ID.md](VOICE_ID.md). |
| `voice_id_sufficient_seconds` | `6.0` | The seconds of clear speech a person's stored voice needs before identification trusts it. Under the bar their turns stay uncertain. |
| `voice_id_min_short_clips` | `2` | The second half of the sufficiency bar: how many short clips, of one to two seconds, the stored voice must include, so a quick interjection can be recognised as well as a full sentence. |
| `voice_id_model_url` | `""` | Overrides the local speaker model's download URL. Empty uses the built-in pinned URL. Pin the hash too, because a URL override checked against the default hash fails verification and the matcher stays unavailable. |
| `voice_id_model_sha256` | `""` | Overrides the local speaker model's pinned SHA-256. Empty uses the built-in pin. The model is fetched once to `<data_dir>/voice_models/`, verified against this hash before use, and never committed. |

### When the matcher is off or missing

A known voice is named on your computer in a fraction of a second, and
a voice the matcher can't place stays unnamed. The only ElevenLabs
batch call left runs when voices overlap, to split the crosstalk.

With `voice_id_enabled` false, or the `sherpa-onnx` wheel or the model
file absent, turns are not named and the room never arms on its own.
Introductions, spoken commands and the switch in the voice settings
still arm it by hand.

## Memory, the companion service

| key | default | what it does |
|---|---|---|
| `memory_url` | `http://127.0.0.1:8901` | Where to probe for [Membro](https://github.com/shawn-durrani/membro). When it answers, the memory features light up, and when it doesn't the app runs without memory. It's probed again every 30 seconds, so start order doesn't matter. |
| `MEMORY_AUTH_TOKEN`, in `.env` and not a config key | unset | Membro's owner token, sent as a bearer on its `/search` and on the job-status polls behind imports. One token, put once in Crossband's `.env`. Without it `search_history` reports a failure and never reads as "no history". |

### The memory token

Membro's `/recall` and `/summary` answer a loopback caller with no
credential. Its `/search`, the verbatim transcript search behind the
`search_history` tool, needs the owner token even on loopback, and so
do its job-status polls. The same rule covers membro's MCP server, where
`search_history` works only when the token was passed at registration.
The example guest mount passes no token, so a summoned guest keeps
recall, summary and save and loses verbatim search. Hand a guest the
token only when you mean to. The `membro-admin` entry in `code_mcp`
resolves the same variable through `${MEMORY_AUTH_TOKEN}`, so one token
in Crossband's `.env` serves every path.
[GUEST_PERMISSIONS.md](GUEST_PERMISSIONS.md) has the guest side.

If `search_history` or the person sync goes quiet after you rotate the
token, look in `data/service.log` for a line containing
`membro refused`. It names the call membro refused and says the token
in Crossband's `.env` doesn't match membro's copy.

### The memory contract

Crossband reads membro's `contract_version` from `/v1/health` and needs
major version 1. A different major is treated as no memory at all.
Minor versions are additive, and each newer field is used only when
membro sends it, so an older membro keeps the behaviour of the version
before.

- 1.2: `speaker_identity` beside a guest turn on ingest.
- 1.3: `web_sources` on ingest and on saved facts, so a web-derived
  fact is held for review.
- 1.4: `web_sources` comes back on `search_history` hits, and a hit
  that carries any gets the untrusted marker a live fetch gets.
  `browser_origin` on `/v1/health` is where a phone's browser reaches
  membro, and the voice-discard eraser link uses it. Before each
  handoff, Crossband reads membro's per-chat watermark and winds its
  own `ingested_upto` back when membro holds less, which is what a
  restore from backup leaves behind. A saved fact's `event_date` is
  your local calendar day, and membro anchors it to local midnight.
- 1.5: `guest_speakers` on a saved fact. In room mode a model's direct
  `save_memory` carries the guests present in the round, as the same
  `guest:<name>` and `guest:unknown` classes ingest uses, and membro
  holds the save for review under its guest-present group. Outside
  room mode, or with you alone, the field isn't sent.
- 1.6: `source_app` and `conversation_id` on every recall, the same
  pair ingest uses. Membro binds the facts it draws from a guest's
  turns to the chat they came from and hands them back only to that
  chat. A model's direct `save_memory` names no chat, because membro's
  save route doesn't take one. A save made with guests present is still
  held for review, and once you approve it, it's recalled in every chat.

On a 1.3 membro the marker never appears, the eraser link falls back to
the browser's own host on port 8901, and the watermark route is never
called. On a 1.4 membro the guest stamp is sent and ignored, so such a
save goes straight to recall, and Crossband logs one warning per
process. On a 1.5 membro the chat named on a recall is ignored, so a
guest's facts surface in every chat as they did before.

## The coding guest and GitHub

The short description is in
[What you can do, in README.md](../README.md#what-you-can-do), and the
security bounds in every mode are in
[GUEST_PERMISSIONS.md](GUEST_PERMISSIONS.md). Change the allow and deny
lists only together with that page.

| key | default | what it does |
|---|---|---|
| `code_repos` | `{}` | Each short name maps to a local path a guest may open. Empty means the whole feature is dark. |
| `code_mcp` | `{}` | MCP servers mounted into the guest, each name mapping to `{command, args, env}`. Mounted in every mode and allowed whole, every tool the server exposes. [GUEST_PERMISSIONS.md](GUEST_PERMISSIONS.md) has a worked example with the `PYTHONPATH` Membro needs. |
| `github_repos` | `{}` | Each name maps to an `owner/repo` the models may read and file issues against. It signs in with `GITHUB_TOKEN` from the environment, or else the computer's logged-in `gh` command. |
| `code_use_api_key` | `false` | `false` means guest turns ride the computer's Claude Code login, the subscription. `true` bills `ANTHROPIC_API_KEY` per token. Either way the turn records which one paid. |
| `code_model` | `default` | The guest's model tier, one of `default`, `opus`, `sonnet` or `haiku`. A summon may override it. It changes the rate and never the account that pays. |
| `code_effort` | `default` | The guest's thinking level, one of `default`, `think`, `think-hard` or `ultrathink`. A summon may override it. |
| `code_allow_writes` | `false` | Implement mode. The guest may branch, test, push and open a pull request, and never merge or push `main`. Off leaves investigate, which only reads, and run, which runs the project's own commands without writing. |
| `code_default_on` | `false` | New chats start with the `code` toggle already on, which is harmless without `code_repos`. |
| `code_max_turns` | `50` | The SDK turn cap for one read-only visit. |
| `code_timeout_s` | `600` | The wall-clock cap for one read-only visit. |
| `code_impl_max_turns` | `150` | The turn cap for an implement-mode visit. |
| `code_impl_timeout_s` | `1800` | The wall-clock cap for an implement-mode visit. |

## External integrations

| key | default | what it does |
|---|---|---|
| `mcp_servers` | `{}` | MCP servers the seats may call over stdio, each name mapping to `{command, args, label?}`. Set it in `config.local.json`. The optional `label` shows in the work-status chip while that server is in flight, and a server without one shows a plain "Working on it". |
| `ingest_token` | `""` | The bearer for the machine side-channel, `POST /api/ingest` and `POST /api/chats/{id}/notice`, set as `CROSSBAND_INGEST_TOKEN`. Once a password is enrolled it's the only way a producer reaches either route. [PRODUCERS.md](PRODUCERS.md). |
| `slash_commands` | `[]` | Suggestion chips in the composer for `/` messages, each `{insert, label, hint}`. Crossband gives no command a meaning, so a `/` message goes to your tooling and no model replies. [PRODUCERS.md](PRODUCERS.md) has the contract. |
| `spend_note_every` | `30` | While a seat sits above its default depth, or [research mode](WEB_RESEARCH.md#research-mode) is on, the chat gets a system line every this many messages saying what that seat, or the chat, has spent since then. A rate-card estimate, never a bill. `0` turns it off. |
| `slash_ack_timeout_s` | `120` | The dead-man for `/` messages. If nothing acknowledges a slash command within this window, one system line says nothing picked it up, so a stopped watcher stops looking like a queued deploy. `0` turns it off. [PRODUCERS.md](PRODUCERS.md). |

## Research tool caps

| key | default | what it does |
|---|---|---|
| `search_timeout` | `20` | Seconds a web search may take. |
| `fetch_timeout` | `15` | Seconds a page fetch may take. |
| `max_tool_output` | `8000` | Characters of tool output returned to the calling model. |
| `tool_log_chars` | `1200` | Characters per tool event when it's replayed into later transcripts. |
| `max_tool_rounds` | `6` | The tool-call loop cap per reply. |
| `research_tool_rounds` | `18` | The tool-call loop cap per reply while [research mode](WEB_RESEARCH.md#research-mode) is on for the chat. |
| `max_transcript_chars` | `100000` | The cap on a YouTube transcript in the chat. |
| `max_audio_mb` | `60` | The download cap for `transcribe_audio_url`. |
| `max_search_results` | `5` | Results per search. |
| `egress_max_transfer_mb` | `64` | The per-connection byte backstop at the egress proxy. Keep it at or above `max_audio_mb`, because podcast audio rides the same path. |
| `egress_politeness_s` | `2` | The minimum spacing between bursts of connections to the same host. One page load's subresource connections count as a single burst. |
| `egress_idle_timeout_s` | `60` | An egress connection with no bytes moving for this long is closed. |
| `egress_tunnel_lifetime_s` | `300` | The hard wall clock on one egress connection. |
| `fetch_max_page_mb` | `10` | The decoded-body cap for `fetch_page`. A bigger page errors and never balloons in memory. |
| `browse_timeout_s` | `20` | The wall clock for one rendered view with `view_page`, worker process included. Rendering needs Playwright plus `.venv/bin/playwright install chromium`, about 160MB, and without either the tool isn't offered. |
| `browse_page_budget_mb` | `30` | The total bytes one rendered page load may pull across all its connections, subresources included. `0` turns the budget off and leaves the per-connection caps only. |
| `browse_sandbox` | `true` | On macOS, wraps the render worker in an operating-system sandbox profile: network limited to the proxy port, writes only to its throwaway profile folder, and no reads of the data folder, `.env` or `~/.ssh`. A refused profile renders as before. |

## Backups

| key | default | what it does |
|---|---|---|
| `backup_keep` | `14` | Snapshots kept in `data/backups/`. Each cycle writes the database, as `chat-<stamp>.db`, and the learnt voices, as `voices-<stamp>.tar` of `voice_anchors/`. Restore the voices by untarring into `data/`. |
| `backup_interval_hours` | `6` | How often a snapshot is written, plus one at every startup before anything touches the database. |
| `backup_mirror_dir` | `""` | An optional second folder that receives completed snapshots only, never the live database, because a sync daemon watching a live WAL file causes lock hangs. |
| `backup_mirror_keep` | `7` | Snapshots kept in the mirror. |

## Startup behaviour

| key | default | what it does |
|---|---|---|
| `require_keys` | `false` | `true` aborts startup on a missing provider key. `false` runs without that provider. Either way every missing key is named loudly at startup. |

The source of truth is `Settings` in `backend/config.py`. A test in
`tests/test_doc_style.py` fails when a setting exists that isn't named
here, so no setting can go missing, and the descriptions are written by
hand. The same test checks that [docs/README.md](README.md) links every
document in `docs/`.
