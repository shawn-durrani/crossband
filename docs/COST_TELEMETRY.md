# Cache telemetry and utility spend

This is the operator/developer guide to two things: a content-free diagnostic
log line for Claude-chat's prompt cache, and cost attribution for the cheap
"utility" model behind rolling summaries, auto-titles and project
distillation. Neither changes what gets cached, which model answers, or what
you're billed; they only make what was already happening *visible*.

**This document does not claim measured savings.** The stable/volatile split
described below divides Claude-chat's system prompt so an ordinary per-message
change (fresh memory results, who already replied this round) no longer busts
the large, unchanging persona/project/instructions prefix; that is a change to
cache *content layout*, not a promise about your bill. Whether it actually
reduces cache-write volume on your traffic is exactly the kind of thing the
telemetry below lets you check for yourself, with your own before/after
sample. Nobody has published a number, so don't repeat one.

## 1. Scope and vocabulary

The Spend page's by-source breakdown separates three cost sources that used to
be blurred together (or, for utility, invisible):

| Source (Spend page label) | What it is | Billing |
|---|---|---|
| **Model turns** | Every resident chat participant's replies, Claude *and* GPT seats alike. Prompt-cache telemetry (§2) only applies to the Claude/Anthropic seats in this bucket; GPT participants use the OpenAI Responses API, which has no equivalent cache-control breakpoint to instrument here. | Metered API, always. |
| **Coding agent** | A summoned Claude Code guest turn (`backend/guest.py`), which is a separate code path from chat participants, with its own cache-usage accounting (`cache_creation_input_tokens` from the Claude Code SDK). It does **not** emit the `claude_chat_cache` log line described below. | Metered API key or the account's Claude Code subscription, whichever the turn actually recorded (`usage_json.auth`); see `ARCHITECTURE.md`, "A recorded dollar is not a charged dollar". |
| **Utility (titles/summaries)** | Rolling-summary, auto-title and project-distillation calls (`backend/chat_memory.py`), typically routed to a cheap model (`claude-haiku-4-5` by default). Non-streaming single calls with **no prompt caching at all**, so there's nothing to instrument here beyond the token counts already recorded. | Metered API, when a utility model is configured. |

If you're trying to explain a Claude-chat cost line, first confirm you're
actually looking at "Model turns" and not "Coding agent" or "Utility", because the
cache telemetry in this doc concerns Claude-chat participants only.

## 2. Cache telemetry: what's logged, and what isn't

Every real request from a Claude-chat participant (Anthropic provider) emits
one log line per tool-use round, at `INFO` level, from the `crossband.providers`
logger, tagged `claude_chat_cache`:

```
claude_chat_cache speaker=<slug> model=<model-id> tool_round=<int>
  stable_hash=<16-hex> stable_chars=<int>
  volatile_hash=<16-hex> volatile_chars=<int>
  transcript_hash=<16-hex> ttl=<label>
  input_tok=<int> cache_read_tok=<int>
  cache_write_5m_tok=<int> cache_write_1h_tok=<int>
  output_tok=<int>
```

### Every field is content-free

`*_hash` values are a truncated SHA-256
fingerprint (`backend/providers.py:_content_hash` / `_messages_hash`) of a
prompt block: proof that a block changed or didn't, never a way to recover
what it said. `*_chars` are byte counts, not text. The token and cache-write
counts come straight from Anthropic's own `usage` / `cache_creation` response
fields. **Prompt and transcript text are never logged, by construction.**
There is no code path in this feature that writes chat content to the log.

### Field reference

- **`stable_hash` / `stable_chars`**: the persona/project/shared-instructions
  block (`providers._stable_system_parts`): identical across calls for a
  given participant/project/round, and the block that actually carries the
  `cache_control` breakpoint (the whole point of the split).
- **`volatile_hash` / `volatile_chars`**: the memory summary, ambient memory,
  delegation note, memory-write warning and round-predecessors block
  (`providers._volatile_system_parts`). Expected to change on nearly every
  call. It rides at the very END of the message list, inside the final user
  turn after the transcript breakpoint (a `developer` input item on the OpenAI
  side), because any changing byte upstream of that breakpoint invalidates the
  whole conversation cache.

  `chat_summary` is deliberately NOT here. Its changes are coupled to the
  transcript, since one writer advances `summary_upto` in the same statement,
  so a rebuild already re-writes the transcript. Moving the summary would cost
  uncached tokens every turn to prevent a bust it is not causing.
- **`chat_id` / `tools_hash` / `tools_n` / `changed`**: the fields that
  let a miss name its own cause. `tools_hash` covers the tool definitions, which
  lead Anthropic's cache prefix AHEAD of system and messages: change them and
  everything behind is invalidated while every other hash here stays identical
  (`engine.py` adds/removes `summon_claude_code` depending on whether a summons
  is claimed, exactly that shape). `chat_id` matters just as much: switching
  chats legitimately changes the whole prefix, so without it a benign switch and
  a real break look the same. `changed` lists which components differ from this
  seat's previous call IN THIS CHAT: `none` on a hit, `first-call` after a
  restart. All four also ride `usage_json.cache_prefix`, so this is SQL-queryable
  rather than log-only.
- **`transcript_hash`**: a fingerprint of the CACHEABLE conversation
  prefix: the outgoing message list *before* the volatile tail joins it
  (hashing the tail in would show churn by construction). Same value on
  every tool round of one reply; compare it across calls to tell whether
  the conversation-side breakpoint saw the same prefix or a new one.
- **`ttl`**: always `5m-ephemeral-default` today (`providers.CACHE_TTL_LABEL`,
  a constant rather than a per-call measurement). It exists so a future TTL change,
  if one is ever trialled, is provably absent or present here rather than
  assumed. **1-hour TTL is not enabled.** Anthropic's ephemeral breakpoints
  default to 5 minutes when no `ttl` field is sent, which is what this code
  still does everywhere it sets `cache_control`. A 1h trial would need its own
  code change, its own telemetry-informed evaluation first, and its own PR.
  It does not ship here.
- **`cache_read_tok`**, **`cache_write_5m_tok`**, **`cache_write_1h_tok`**:
  real counts from Anthropic's `cache_creation` breakdown. Given the TTL point
  above, `cache_write_1h_tok` should read `0` on every line today; if it
  doesn't, that itself is worth reporting, since it would mean the API
  behaved unexpectedly, not that Crossband requested it.
- **`tool_round`**: which iteration of the tool-use loop this call was
  (`0` for a plain reply with no tool calls). Logged once per round, not once
  per reply, so a tool-heavy exchange produces several lines for one visible
  message.

### Where it goes

These are ordinary Python `logging` calls under the `crossband`
logger hierarchy, which lands in `data/service.log` under the launchd
supervisor (or your terminal under `./start.sh`); see
[docs/OPERATIONS.md](OPERATIONS.md). By default the app only surfaces
`WARNING`-and-above from its own code, so **these `INFO` lines are silent
unless you turn them on**. Set `CROSSBAND_LOG_LEVEL=INFO` (any standard level name,
case-insensitive) for the duration of a sampling session, then unset it again.
It only changes what's written to the log, never what gets cached, priced,
or billed, and it affects nothing else (uvicorn's own request/access logging
is configured independently and is unaffected either way).

### No log? The database has the same story

Per-message cache
counters are always persisted in `messages.usage_json` (`input`,
`cache_read`, `cache_creation`, `output`, summed across a reply's tool
rounds), so cache behaviour can be checked after the fact even when the
INFO line was dark at the time:

```sh
sqlite3 data/chat.db "SELECT json_extract(usage_json,'$.model'),
  json_extract(usage_json,'$.input'), json_extract(usage_json,'$.cache_read'),
  json_extract(usage_json,'$.cache_creation') FROM messages
  WHERE usage_json IS NOT NULL ORDER BY id DESC LIMIT 20"
```

Healthy caching reads as `cache_read` ≈ the conversation size with small
`cache_creation`; a persistently large `cache_creation` on every reply means
the prefix is being invalidated. The log line remains the
richer view, because it splits stable/volatile/transcript by hash and shows
which TTL bucket a write landed in, but it is a sampling tool rather than the
only record.

## 3. How to interpret it, carefully

**A repeated cache write is not, by itself, evidence of anything broken.**
Two different, legitimate causes produce the same symptom, and you cannot
tell them apart from a write/read ratio alone:

1. **Content actually changed.** A different `stable_hash` between two calls
   for the same participant/round means the persona, project instructions, or
   shared instructions text really did change (you edited them, or the
   project's distilled memory updated). A fresh cache write there is
   correct behaviour, not a bug.
2. **TTL expiry.** The *same* `stable_hash` on two calls more than ~5 minutes
   apart still costs a fresh cache write, because ephemeral breakpoints expire. A
   slow-paced conversation, or you stepping away between messages, produces
   this legitimately with an unchanged prompt.

To tell them apart, compare `stable_hash` across consecutive lines for the
same `speaker`: **same hash, still a write** → TTL expiry (or a first-ever
write for that content); **different hash** → content changed upstream of the
cache. Do the same for `volatile_hash` if you're specifically checking whether
the split is doing its job: a `volatile_hash` changing every call
while `stable_hash` stays put across a short burst of messages is exactly the
intended shape.

## 4. Spend attribution: `utility_usage`

`chat_memory._run_utility` (`backend/chat_memory.py`) is the one call site for
all three utility uses (`kind`: `summarize` | `title` | `distill`). Each real
call persists one row to `utility_usage` (`chat_id`, `kind`, `model`,
`input_tokens`, `output_tokens`, `cost`, `provenance`, `created_at`) and
commits immediately, independent of what the caller does with the reply text
(a degenerate title/summary is still a call that happened and cost money).
Nothing is logged when no call went out at all (missing utility-model key),
which is the same "degrade quietly, log nothing" behaviour the app had before
the split.

Two honesty details worth knowing when reading these numbers:

- **`cost` may be `None`.** A utility model absent from the local pricing
  table is recorded as "not tracked", never a silent `$0.00`, the same
  convention as `voice_usage`.
- **`provenance` is recorded at write time, immutably** (schema v11). The cost
  provenance (typically `rate_card_estimate` for a Haiku-family model) is
  resolved once, when the call happens, and stored on the row, so a later edit
  to the local rate-card table can never retroactively change what an
  already-logged row's cost *meant* when it was made. Every other cost source
  in the app follows the same discipline: a recorded cost keeps the provenance
  it was computed under. Only rows written **before** this column existed
  (`NULL`) fall back to resolving against the live rate-card table at read
  time, exactly as every row did before this change shipped.

**No historical backfill.** Rolling summaries, auto-titles and project
distillation ran long before this attribution existed, and none of that earlier
spend has a `utility_usage` row, because the token counts were never recorded
at the time and cannot be reconstructed after the fact. The Spend page's
"Utility" bucket reflects calls made **after** this deployed, not your lifetime
utility spend. Don't read a low "Utility" total on an old account as evidence
that utility calls are cheap; it may just mean most of that history predates
attribution.

## 5. A redacted before/after workflow

A safe way to collect your own evidence, without exposing conversation or
credential content. Every field involved is already content-free by
construction (§2), so nothing here needs manual redaction beyond not renaming
your own chat/participant slugs to something identifying.

### Before you start

Decide what you're comparing (e.g. "this build" vs. "a
future change"), and use a comparable conversation for both samples: the same
rough length and cadence, ideally the same scripted set of messages replayed
into a scratch chat. Comparing a two-message chat to a fifty-message chat
tells you nothing.

1. **Turn on verbose logging for the session.** `config.local.json` is the
   personal, gitignored config layer (`defaults < config.json < config.local.json
   < environment`, see `backend/config.py`), so it's read the same way whether
   you run directly or under the launchd supervisor, with no need to touch the
   supervisor's plist:
   ```json
   { "log_level": "INFO" }
   ```
   Restart to pick it up (`./start.sh` directly, or
   `launchctl kickstart -k gui/$(id -u)/dev.crossband.server` if supervised;
   see [docs/OPERATIONS.md](OPERATIONS.md)). A one-off run without touching
   config works too: `CROSSBAND_LOG_LEVEL=INFO ./start.sh`.

2. **Run your comparison conversation**, then pull just the telemetry lines
   for that window:
   ```sh
   grep 'claude_chat_cache' data/service.log > sample-before.log
   ```
   This file is safe to attach to an issue or share as-is: it contains only
   hashes, byte counts and token counts (§2), never prompt or chat text.

3. **Summarise it** rather than eyeballing raw lines:
   ```sh
   # number of DISTINCT stable-block fingerprints seen. For a chat where you
   # didn't touch persona/project/instructions, this should stay small
   # (ideally 1 per participant) across the whole sample.
   grep -oE 'stable_hash=[0-9a-f]+' sample-before.log | sort -u | wc -l

   # cache read/write token totals for the sample
   grep -oE 'cache_read_tok=[0-9]+' sample-before.log | grep -oE '[0-9]+' \
     | awk '{s+=$1} END {print "cache_read_tok total:", s+0}'
   grep -oE 'cache_write_5m_tok=[0-9]+' sample-before.log | grep -oE '[0-9]+' \
     | awk '{s+=$1} END {print "cache_write_5m_tok total:", s+0}'
   ```

4. **Repeat steps 1–3 for the "after" condition**, producing `sample-after.log`
   and its own totals, using the same scripted conversation.

5. **Compare structurally, not as a dollar claim:**
   - Did the count of distinct `stable_hash` values drop between before/after
     for the same conversation shape? Fewer distinct stable hashes for the
     same content means the stable prefix is surviving more calls unbusted.
   - Did total `cache_write_5m_tok` fall relative to total `cache_read_tok`
     (or relative to `input_tok`) between the two samples?
   - Cross-check against the Spend page's "Model turns" row for the same
     window as a sanity check, but remember the Spend page reports *billed
     cost*, not cache-hit ratio, and cost depends on far more than caching
     (model choice, reply length, round count).
   - Report the structural comparison (hash churn, read/write ratio) rather
     than a percentage or dollar figure, unless you've independently confirmed the
     billed numbers on the Spend page across a long enough, representative
     window to say something real. A two-sample log comparison is a
     hypothesis check, not a savings measurement.

6. **Turn `CROSSBAND_LOG_LEVEL` back off** (remove the `config.local.json` key, or
   unset the env var) and restart once you're done sampling, so the service
   returns to its default, quiet logging baseline.
