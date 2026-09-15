# Why a reply takes as long as it does

Most of the silence before a reply isn't the model thinking. It's the
app sending the conversation again. A provider's API keeps nothing
between calls, so every turn ships the whole transcript, and anything
that changes between turns breaks the prompt cache that would make the
resend cheap and fast.

You won't find figures from anyone else's setup here. How long a reply
takes depends on your models, your conversation and your connection,
so the only numbers worth reading are your own, and the second half
says how to get them.

## The three causes worth knowing

The cached prefix churns. A prompt cache only helps when the start of
the prompt is byte for byte the same as last time. A field that changes
every turn, such as a timestamp, a round counter or freshly recalled
memory, parked in the cached block rewrites the whole prefix on every
call. A cache write costs more than a read, so the turn is slower and
dearer at once, and neither the dollar total nor the token total moves
enough to show it. The ratio of cache reads to cache writes is the
signal.

Attachments weigh more than they look. Every photo is uploaded again on
every turn, to every seat. A short chat carrying a handful of phone
photos can send tens of megabytes per turn while its text is tiny.
Shrinking an image cuts the bytes on the wire but not the tokens the
model bills for, because providers scale an image down before they
count it. A smaller image costs the same tokens and arrives sooner. The
two are separate problems, and the context ring in the chat header
reports both.

Seats take turns. Several seats replying in one round, each preparing
its own context, add up. They reply in order so that each can read the
one before, and a diagnostic that reads that as a fault has read it
wrong.

## Measuring your own

- For cache health, run the `usage_json` query in
  [COST_TELEMETRY.md](COST_TELEMETRY.md). Healthy looks like a small
  `cache_creation` and a `cache_read` about the size of the
  conversation. Set `CROSSBAND_LOG_LEVEL=INFO` for a session to get the
  fuller per-call log line.
- For the voice stages, `GET /api/voice/trace/summary` gives the
  median, the 95th percentile and the maximum per stage over the last
  24 hours, and any seat can read the `voice_latency` diagnostic.
- For the weight of a conversation, the context ring in the chat header
  shows tokens by component and the megabytes sent again per turn. Or
  ask a seat for the `conversation_performance` diagnostic, which names
  the cause in words.
- For the pacing between speakers, compare the `created_at` gaps in
  `chat.db`.
- For what each seat's completion did, `GET /api/models/seat_trace`
  lists the recent completions, newest last. Each carries the round and
  seat, the time to first token, the total time, chunk and character
  counts, the finish reason, how it ended, and a short hash of the
  reply. A reply that repeats an earlier one by the same seat in the
  same chat is marked, and a doubled send of the same text within ten
  seconds appears too. Never the text itself. Add `?chat_id=` to narrow
  it, and the voice diagnostics dump carries the same list for its
  chat.

## Two traps when reading the numbers

Usage adds up across tool rounds. Every API call in a turn counts, so a
turn with five tool rounds reports about five times the context of one
call. Divide by the number of calls before you decide a conversation is
enormous.

A window that spans a fix blends before and after. Read as one number,
a 24 hour window can make a fix look like a regression on the day it
landed. Compare like windows, or wait for the window to clear the
change.

## Not yet measured

Text chats store tokens and cost but no durations, so "how long did
that reply take" has no stored answer for text. The seat ledger holds
the timings of recent completions in memory only, and they go when the
app restarts. Voice has its own stored per-turn traces.
