# Reading your cost and cache numbers

This is for anyone who wants to know what their chats cost and where
the money goes. The Spend page splits your spend by where it came
from. The cheap model that writes titles and summaries has its own
spend record, and so does a model call that leaves no message. Every model in a chat has a seat, and each Claude seat
writes one line to the log on every call saying what its prompt cache
did, with none of your words in it. None of that changes what gets
cached, which model answers, or what you're billed.

Crossband lays out each Claude seat's prompt to suit Anthropic's
cache. Anthropic can keep the start of a request between calls and
charge less to read it back, which it calls
[prompt caching](https://platform.claude.com/docs/en/build-with-claude/prompt-caching).
Crossband splits the seat's system prompt into a part that stays the
same and a part that changes every message, and a cache mark after the
first part tells Anthropic to keep everything before it. A fresh
memory result or a new reply from another seat then leaves the large
part cached. That's a change to how the prompt is laid out and no
promise about your bill. Whether it cuts cache writes on your traffic
is what the log line lets you check, with your own before and after
sample. Nobody has published a number, so don't repeat one.

## Where the money goes

The Spend page's By source table splits your spend into these rows.

| Source | What it is | How it's billed |
|---|---|---|
| **Model turns** | Every call a seated model makes, Claude and GPT alike, including the ones that leave no message. | Metered on your API key, always. |
| **Coding agent** | A turn by a Claude Code guest you summoned into the chat. | Your API key or your [Claude Code subscription](https://code.claude.com/docs/en/costs), whichever the turn recorded. |
| **Utility (background model work)** | Rolling summaries, auto-titles and project distillation, plus the scans that read what you say. | Metered on your API key, when a utility model is set. |

The cache log line covers the Claude seats in Model turns and nothing
else. A GPT seat talks to the
[OpenAI Responses API](https://developers.openai.com/api/reference/resources/responses),
which has no cache mark to instrument. A coding agent turn goes through
its own code, in `backend/guest.py`, which records the turn's cache use
as `cache_creation_input_tokens` from the usage Claude Code reports and
writes no cache log line.

Every cost lands in one of three columns that are never added
together: metered on a key, covered by a subscription, or unknown.
Whether a coding agent turn was metered or covered by the subscription
is stored on it as `usage_json.auth`, and
[A recorded dollar is not a charged dollar](../ARCHITECTURE.md#a-recorded-dollar-is-not-a-charged-dollar)
explains how the columns stay apart.

A utility call is a single call with no streaming and no prompt
caching, so its token counts are the whole story. The default utility
model is `claude-haiku-4-5`. The By producer table on the same page
shows the same money as Utility (titles/summaries).

## How a Claude request is laid out

Every time a Claude seat replies, Crossband sends one request made of
four blocks. A round is one pass where each seated model gets a turn
to reply, and the blocks come in this order.

1. The tool definitions.
2. The stable system block: the seat's persona, the project
   instructions and the shared instructions. It reads the same on
   every call for that seat, project and round.
3. The conversation so far.
4. The volatile block: the memory summary, memory fetched for this
   message, the delegation note, the memory-write warning, and who has
   already replied this round. The delegation note says a summons is
   already claimed, and the warning appears when the memory service
   isn't healthy. The block changes on nearly every call.

Crossband puts a cache mark at the end of the stable block and another
on the second-last message of the conversation. The cache works on the
start of the request, called the prefix, so everything before a mark
can be stored and read back on the next call. A change anywhere before
a mark throws away everything from that point on, and the next call
stores it again.

The volatile block comes after both marks, so it can change freely
without touching what's stored. A model that accepts a system turn at
the end of the conversation gets it as one, and the others get it
added to the end of the last user turn, framed as text Crossband
assembled.

Storing a block costs more than reading it back. Anthropic charges
1.25 times the
[normal input price](https://platform.claude.com/docs/en/about-claude/pricing)
to write a block into the cache and a tenth of it or less to read the
block back. Input that's neither read nor written is sent at the full
price. A cache pays off when most of each call's input is read back.

## How the Spend page judges the cache

Under Prompt cache health, the Spend page gives each model the share
of all its input that was read back from the cache. All its input
means what was read, what was written and what was sent at full price.
It rates that share against two bars.

| Share read from the cache | Verdict | What it means |
|---|---|---|
| 80% or more | Healthy | The prompt is reused from call to call, and only the newest turn and the per-call memory block are paid in full. |
| 50% to 80% | Watch | Part of the prompt is sent at full price or stored again each call. |
| Under 50% | Poor | Most input costs full price or more, so input costs over half what it would with no cache at all. |

The top bar comes from how the Claude seat did in steady use. On busy
days it read 83 to 91% of its input from the cache. On quiet days it
read 65 to 72%, because the cache runs out after five minutes between
messages. The bottom bar is where caching stops doing most of its job.
The page leads with the worst model, because one busy model reading
nearly everything from the cache can hide a struggling seat in the
average. A model that did no caching at all is left out.

On a computer, the read to write ratio shows when you hold the pointer
over a verdict. A ratio near one to one means a prompt is being stored again about as
often as it's read, which is the sign of a prefix that keeps
changing.

The chat summary stays in the stable block. When the app folds older
messages into the summary, the same database statement moves
`summary_upto`, the point the transcript is sent from, so a new
summary always comes with a new conversation prefix. Moving it to the
volatile block would cost uncached tokens on every turn to prevent a
miss it isn't causing.

Each block has a fingerprint in the log line, so you can tell which
one changed between two calls.

```mermaid
flowchart LR
  tools["Tool definitions<br/>tools_hash"]
  stable["Persona, project and<br/>shared instructions<br/>stable_hash"]
  transcript["The conversation so far<br/>transcript_hash"]
  volatile["Memory results and who's<br/>replied this round<br/>volatile_hash"]
  claude("Claude")
  tools -- "then" --> stable
  stable -- "cache mark, then" --> transcript
  transcript -- "cache mark on the<br/>second-last message, then" --> volatile
  volatile -- "one request" --> claude
  classDef node fill:#d4d4d8,stroke:#757575,color:#18181b
  classDef hero fill:#38bdf8,stroke:#0284c7,color:#18181b,stroke-width:2px
  class tools,transcript,volatile,claude node
  class stable hero
```

## The cache log line

Every request a Claude seat makes writes one line to the log, at
`INFO` level, from the `crossband.providers` logger, tagged
`claude_chat_cache`. When a model calls a tool, Crossband runs it and
calls the model again with the result. Each of those calls is a tool
round, and each writes its own line.

```
claude_chat_cache speaker=<slug> model=<model-id> chat=<int> tool_round=<int>
  tools_hash=<16-hex> tools_n=<int> changed=<csv|none>
  stable_hash=<16-hex> stable_chars=<int>
  volatile_hash=<16-hex> volatile_chars=<int>
  summary_hash=<16-hex|none> summary_chars=<int>
  transcript_hash=<16-hex> ttl=<label>
  thinking=<type|none> effort=<label|default>
  input_tok=<int> cache_read_tok=<int>
  cache_write_5m_tok=<int> cache_write_1h_tok=<int>
  output_tok=<int>
```

### Nothing in it is your text

No prompt or transcript text is ever written to the log, and no code
path in this feature can write chat content to it. Each `_hash` field
is the first 16 characters of a SHA-256 fingerprint of one block, made
by `_content_hash` and `_messages_hash` in `backend/providers.py`. A
fingerprint proves whether a block changed between two calls and can't
give the text back. Each `_chars` field is a character count, and the
token and cache-write counts come straight from the `usage` and
`cache_creation` fields on
[Anthropic's response](https://platform.claude.com/docs/en/api/messages).

### What each field means

- `speaker` and `model`: the seat that made the call, by its slug,
  and the model id it used. A slug is the short name the app gives a
  seat.
- `chat`: the chat's id. Switching chats changes the whole prefix,
  and this field is what tells a switch apart from a real break.
- `tool_round`: which tool round this call was. A plain reply with no
  tool calls is round `0`, and a reply that used tools produces
  several lines for one visible message.
- `tools_hash` and `tools_n`: a fingerprint of the tool definitions,
  and how many were sent. A change to the tools stores everything
  behind them again while every other fingerprint stays the same. The
  list is set by the chat's switches and what's installed, so it
  changes when you switch web, code or memory on or off for a chat, or
  when an outside tool server connects for the first time since the app
  started. A tool that's down, or a summons that's already claimed,
  stays in the list and refuses the call with a reason.
- `changed`: which parts differ from this seat's previous call in
  this chat, as a comma-separated list drawn from `model`, `tools`,
  `stable`, `volatile`, `transcript`, `thinking` and `effort`. It reads
  `none` when nothing changed, and `first-call` on a seat's first call
  in a chat since the app started. It names `model` when the seat runs
  a different model from its last call in this chat. A new model holds
  none of the old one's cache, so that call writes the whole prefix
  again. It names `effort` or `thinking` when either changed, which is
  what saying "think harder" to a seat does.
- `stable_hash` and `stable_chars`: the stable system block. The
  value holds for a seat, project and round unless you edit the
  persona or the instructions, or the project's memory notes change.
  A restart doesn't change it.
- `volatile_hash` and `volatile_chars`: the volatile block. Expect it
  to change on nearly every call.
- `summary_hash` and `summary_chars`: the memory summary, as a
  fingerprint and a character count, or `none` and `0` for no summary.
- `transcript_hash`: the conversation as sent, fingerprinted before
  the volatile block joins it, because hashing the tail in would show
  churn on every call. It has the same value on every tool round of
  one reply. Compare it across calls to tell whether the
  conversation's cache mark saw the same prefix or a new one.
- `thinking` and `effort`: what was sent on this call. `thinking` is
  the thinking block's type, or `none`, and `effort` is the effort
  level sent, or `default` when none was. A level the model doesn't
  take is never sent, so it reads `default`. Either one changes the
  request without changing any fingerprint, and a change to either
  throws the cache away, which is why `changed` names them.
- `ttl`: how long a stored block lives before Anthropic drops it. It
  always reads `5m-ephemeral-default`, which is the constant
  `providers.CACHE_TTL_LABEL`, a label for what the code does and
  never a measurement. Anthropic keeps a block for five minutes when a
  request sends no `ttl`, and Crossband sends none anywhere it puts a
  cache mark, the `cache_control` field. The field exists so a change
  to that lifetime would show here. A one-hour lifetime is not
  enabled, and turning it on would need its own code change, its own
  evaluation from these numbers, and its own PR.
- `input_tok` and `output_tok`: tokens sent at full price, and tokens
  the model wrote back.
- `cache_read_tok`: tokens read back from the cache on this call.
- `cache_write_5m_tok` and `cache_write_1h_tok`: tokens written into
  the cache, split by lifetime, from the `cache_creation` breakdown
  Anthropic returns. The one-hour column should read `0` on every
  line. If it doesn't, that's worth reporting, because it would mean
  the API did something Crossband didn't ask for.

The four fingerprints, the model, `thinking`, `effort` and `changed`
are also saved on the message as `usage_json.cache_prefix`, so you can
query them from the database as well as the log.

### Where the line goes

The line lands in `data/service.log` when the app runs under
[launchd](https://developer.apple.com/library/archive/documentation/MacOSX/Conceptual/BPSystemStartup/Chapters/CreatingLaunchdJobs.html),
the supervisor that keeps it running on macOS, or in your terminal
when you run `./start.sh` yourself. That's because these are ordinary
calls to Python's
[`logging`](https://docs.python.org/3/library/logging.html) module
under the `crossband` logger. [docs/OPERATIONS.md](OPERATIONS.md)
covers the supervisor and the log file.

To see the lines, set `CROSSBAND_LOG_LEVEL=INFO` for the length of a
sampling session, then unset it. By default only warnings and errors
from the app's own code reach the log, so these lines are silent until
you turn them on. Any standard level name works, and capitals don't
matter. The setting changes what's written to the log, never what gets
cached, priced or billed. [Uvicorn](https://www.uvicorn.org/), the web
server the app runs in, has its own request log, which is set up
separately and unaffected.

## The database has the same numbers

You can check cache behaviour after the fact, even when the log line
was off at the time. Every reply stores its cache counters on the
message in `messages.usage_json`, as `input`, `cache_read`,
`cache_creation` and `output`, summed across the reply's tool rounds.
A call that left no message keeps the same block in
`seat_usage.usage_json`.
The database is a SQLite file, `data/chat.db`, and the
[`sqlite3` shell](https://sqlite.org/cli.html) reads it directly.

```sh
sqlite3 data/chat.db "SELECT json_extract(usage_json,'$.model'),
  json_extract(usage_json,'$.input'), json_extract(usage_json,'$.cache_read'),
  json_extract(usage_json,'$.cache_creation') FROM messages
  WHERE usage_json IS NOT NULL ORDER BY id DESC LIMIT 20"
```

Healthy caching shows `cache_read` at about the size of the
conversation, with a small `cache_creation`. A large `cache_creation`
on every reply means the prefix is being thrown away and stored again
each time. The log line is still the richer view, because it
fingerprints each block and shows which lifetime a write landed in,
but it's a sampling tool and the database is the record.

## When a cache write is repeated

A repeated cache write on its own doesn't mean anything is broken. Two
ordinary causes produce the same numbers, and a read to write ratio
can't tell them apart.

1. The content changed. A different `stable_hash` between two calls
   for the same seat and round means the persona, the project
   instructions or the shared instructions changed, because you edited
   them or the project's memory notes were rebuilt. A fresh write
   there is correct.
2. The stored block expired. The same `stable_hash` on two calls more
   than five minutes apart still costs a fresh write, because
   Anthropic drops a block after five minutes. A slow conversation, or
   you stepping away between messages, produces this with an unchanged
   prompt.

To tell them apart, compare `stable_hash` across consecutive lines for
the same `speaker`. The same hash with a write still happening means
the block expired, or this was the first write for that content. A
different hash means the content changed somewhere before the cache
mark.

Check `volatile_hash` the same way to see whether the split is doing
its job. A `volatile_hash` that changes every call while `stable_hash`
holds across a short burst of messages is the shape you want.

## How often the memory summary repeats

The memory summary is about 3,000 tokens, and every Claude seat call
sends it at full price in the volatile block. It changes only when the
memory service rebuilds it, and a new one never causes a miss, because
it sits after both cache marks. A cached block of its own, after the
stable block, would let a call read it back at the cache price. The cache
would serve that block when an earlier call on the same model sent the
same tools, the same stable block and the same summary less than five
minutes before.

Each call records the summary it carried in `usage_json.cache_prefix`,
so you can measure how often that happens before anything moves.
`summary` is the first 16 characters of the summary's SHA-256
fingerprint, or `none` when the call sent no summary. `summary_chars`
is its length in characters. `requests` is how many requests the call
made, one per tool round, and each one sends the summary again.

`scripts/summary_reuse_report.py` reads those records from
`data/chat.db`. It opens the database read-only and writes nothing. Run
it from the app's folder. It covers the last
seven days unless you give it dates.

```sh
.venv/bin/python scripts/summary_reuse_report.py
.venv/bin/python scripts/summary_reuse_report.py --since 2026-09-29 --until 2026-10-06
```

It gives these numbers for each model.

- The Claude seat calls in the period and the requests they made. A
  call that left no message counts like a reply.
- How many distinct summaries they carried, and how often a seat's
  summary was new since its last call in that chat.
- The repeats. A repeat is a call whose summary an earlier call sent
  within five minutes, on the same model with the same tools and
  stable block. A cached summary would have served those calls. A tool
  round after the first always counts, as it follows seconds later.
- What caching would have saved. Each served send costs the model's
  cache read price in place of its full input price.
- The write premium. Each first send is written to the cache, which
  costs a quarter more than sending it at full price.
- The rewrite. A new summary while a seat's cache in that chat is
  still warm throws away the conversation stored behind it, and the
  call writes it again. The report counts what that call read from the
  cache, which makes it an upper bound.
- The net saving, and its share of what those calls cost.

Tokens are estimated at four characters each. The gap between two
calls is measured between the times each one finished, so a gap reads
a little long and the repeats are a floor. Calls recorded before the
fingerprint existed are counted and left out.

## Calls that leave no message

Some model calls never show up in the chat. A model that has nothing to
add replies `[pass]`, and the app hides that reply completely. When the
app turns a reply down and asks the model again, the first try is
thrown away. That happens when a model passes where it owes an answer,
or when its reply only restates one already given. A reply that comes
back empty leaves nothing either. Each of these is still a call you pay
for. A reply that used tools and wrote nothing is kept as a message
with no words, so the chat holds the record of its tools, and its cost
rides on that message like any other reply's.

The app records each one in the `seat_usage` table instead of the
chat. A row holds the chat, the seat, what became of the call and the
usage block its message would have carried. That block has the token
counts, the cost, its provenance and the cache fingerprints, and never
any text. The `outcome` column reads `pass`, `pass_retried`,
`echo_dropped`, `echo_retried`, `empty` or `cut_off`.

The Spend page counts these calls under Model turns, and the chat's
running cost includes them. Under the detail, a line says how much of
the window went on them. That money is already in every total, so the
line never adds to it.

## Calls cut off partway

A call can stop before it finishes. You talk over the reply, it stalls,
or the provider drops it. The provider sends its full token counts only
when a call ends, so the app records what it knew when the call
stopped. That's every tool step that had finished, plus, for Claude,
the input the reply in flight had already read. Anthropic reports that
input as each reply starts, with its cache reads and writes. OpenAI
reports nothing until a call ends, so a GPT call cut off in its first
step counts nothing.

The usage block carries `"partial": true`. It sits on the cut-off
message when one is saved, and on a `seat_usage` row with the outcome
`cut_off` when none is. The Spend page counts these calls like any
other, and a line under the detail says how much of the window went on
them. Each cost more than it counts, because the output written before
the cut isn't known. In the chat, a cut-off reply's cost reads "at
least".

## How utility calls are counted

Two places write utility spend, and they share one pricing step,
`llm_util.price_utility_call`, so neither can price a call differently
from the other.

`chat_memory._run_utility` covers the chat's own uses, with `kind` set
to `summarize`, `title` or `distill`. The rolling summary folds older
messages into a summary once the unsummarised part of the transcript
grows too large. The auto-title names a chat from its content, and
project distillation folds a chat's new messages into the project's
memory notes.

`llm_util.utility_complete_logged` covers the scans that read what you
say, with `kind` set to `intent_scan` or `mismatch_check`. It also covers
the ranking behind a stronger model for one chat, with `kind` set to
`model_step_up`. Each has a chat id but no open database connection, so
that writer opens its own on a worker thread.

Each real call writes one row to `utility_usage` and commits it at
once. The row holds `chat_id`, `kind`, `model`, `input_tokens`,
`output_tokens`, `cost`, `provenance` and `created_at`. It's written
whatever the caller does with the reply, because a title that comes
back empty was still a call that cost money.

The scan writer swallows its own failures. Both scan callers wrap
their whole turn in a handler that abandons the remaining work, so a
failed spend insert would otherwise cost you a name correction or a
mismatch flag. The reply is returned either way.

Every message you send fires one `intent_scan`, which looks for an
instruction, unless it's a `/` message. A voice turn the app puts a
name on can add one `mismatch_check`, which asks whether the words fit
that name. The Spend page folds every kind into one utility line, so
that line grows with every message you send.

When there's no key for the utility model, no call goes out and
nothing is logged. The app carries on without the summary or title and
says nothing.

When you read the numbers, keep these in mind:

- `cost` can be empty. A utility model missing from the local price
  table is recorded with no cost, and the Spend page shows it as "not
  tracked" and never as a silent `$0.00`. That's the same rule
  `voice_usage` follows.
- `provenance` says where the cost figure came from, and it's written
  once, when the call happens. For a Haiku-family model it's
  `rate_card_estimate`, a figure computed from the local price table
  and never a billed amount. A later edit to that table can't change
  what an existing row's cost meant when it was written, and every
  cost source in the app keeps the provenance it was computed under.
  A row with no provenance, written before the column existed, is
  priced against the live table when it's read.

The Utility line on the Spend page covers calls made since the table
existed, never your lifetime utility spend. Calls made before the app
kept this table have no row, because their token counts were never
kept and can't be rebuilt. A low Utility total on an old install can
mean most of its history came before the table, so it's no proof that
utility calls are cheap.

## Checking your own before and after

This is a safe way to collect your own evidence without exposing chat
or credential content. Every field involved is content-free, so
nothing needs redacting, as long as you don't rename your chats or
seats to something that identifies you.

Decide what you're comparing, such as this build against a future
change, and use a comparable conversation for both samples. That means
the same rough length and pace, and best of all the same scripted
messages replayed into a scratch chat. Comparing a two-message chat
with a fifty-message chat tells you nothing.

1. Turn on the log line for the session by adding this line to
   `config.local.json`. That file is your own gitignored config layer,
   read the same way whether you run the app yourself or under the
   supervisor, so the supervisor's plist stays untouched. The layers
   are the defaults, then `config.json`, then `config.local.json`,
   then the environment, and each one overrides the last.
   ```json
   { "log_level": "INFO" }
   ```
   Restart to pick it up, with `./start.sh` if you run it yourself,
   or under the supervisor with this command.
   ```sh
   launchctl kickstart -k gui/$(id -u)/dev.crossband.server
   ```
   A one-off run without touching config works too.
   ```sh
   CROSSBAND_LOG_LEVEL=INFO ./start.sh
   ```

2. Run your comparison conversation, then pull out the telemetry
   lines for that window.
   ```sh
   grep 'claude_chat_cache' data/service.log > sample-before.log
   ```
   If the app restarted during the sample and the log had passed
   10MB, the earlier lines are in `data/service.log.1`, so grep both
   files. The file is safe to attach to an issue or share as it is,
   because it holds only fingerprints, character counts and token
   counts.

3. Summarise it with three commands.
   ```sh
   # how many distinct stable-block fingerprints the sample saw. If you didn't
   # touch the persona, project or instructions, this stays small: one per
   # seat across the whole sample.
   grep -oE 'stable_hash=[0-9a-f]+' sample-before.log | sort -u | wc -l

   # cache read and write token totals for the sample
   grep -oE 'cache_read_tok=[0-9]+' sample-before.log | grep -oE '[0-9]+' \
     | awk '{s+=$1} END {print "cache_read_tok total:", s+0}'
   grep -oE 'cache_write_5m_tok=[0-9]+' sample-before.log | grep -oE '[0-9]+' \
     | awk '{s+=$1} END {print "cache_write_5m_tok total:", s+0}'
   ```

4. Repeat steps 1 to 3 for the after condition, with the same
   scripted conversation, to get `sample-after.log` and its own
   totals.

5. Compare the shape of the two samples, and don't turn it into a
   dollar claim:
   - Did the number of distinct `stable_hash` values fall for the same
     conversation shape? Fewer for the same content means the stable
     block survived more calls.
   - Did the share of input read from the cache rise? That's the total
     `cache_read_tok` over the three totals `cache_read_tok`,
     `cache_write_5m_tok` and `input_tok` added together.
   - Check the Spend page's Model turns row for the same window as a
     sanity check. It reports cost, never a cache-hit ratio, and cost
     depends on far more than caching, such as which model, how long
     the replies were and how many tool rounds ran.
   - Report the comparison as hash churn and share read from the cache. Give
     a percentage or a dollar figure only if you've confirmed the
     billed numbers on the Spend page over a window long enough to
     mean something. A two-sample log comparison checks a hypothesis
     and doesn't measure a saving.

6. Turn the log level back off, by removing the `config.local.json`
   key or unsetting the variable, and restart, so the service goes
   back to its quiet default.
