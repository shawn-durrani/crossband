# Why a reply takes as long as it does

Most of the silence before a reply is not the model thinking. It is the
app re-sending the conversation. Provider APIs are stateless, so every
turn ships the whole transcript again, and anything that changes between
turns invalidates the prompt cache that would otherwise make the resend
cheap and fast.

This page explains the causes and how to measure your own instance. It
deliberately quotes no numbers from anyone else's usage: latency depends
on your models, your conversation, and your connection, and inheriting
someone else's figures would tell you nothing useful.

## The three causes worth knowing

**A churning cached prefix.** The prompt cache only helps when the
prefix is byte-identical to last time. A field that changes every turn
(a timestamp, a round counter, freshly recalled memory) parked in the
cached block re-writes the entire prefix on every call. A cache write
costs more than a read, so this is slower *and* more expensive, and
neither the dollar total nor the token total moves enough to make it
obvious. The read-to-write ratio is the signal.

**Attachments, which are worse than they look.** Photos are re-uploaded
on every turn, to every participant. A short chat carrying a handful of
phone photos can send tens of megabytes per turn while its text is
trivial. Shrinking an image reduces bytes on the wire but not the token
count the model is billed for: providers downscale before tokenising, so
a smaller image usually costs the same tokens and simply arrives sooner.
The two are separate problems and the context ring reports both.

**Serial work that could be parallel.** Several seats replying in one
round, each preparing its own context, adds up. Multi-voice
serialisation is by design where turn order matters, which means a
diagnostic reading it as a defect is reading it wrong.

## Measuring your own

- **Cache health:** the `usage_json` query in
  [COST_TELEMETRY.md](COST_TELEMETRY.md). Healthy looks like a small
  `cache_creation` and a `cache_read` roughly the size of the
  conversation. Set `MMC_LOG_LEVEL=INFO` for a session to get the richer
  per-call log line.
- **Voice stages:** `GET /api/voice/trace/summary` gives p50, p95 and
  max per stage over a 24 hour window, or any participant can read the
  `voice_latency` diagnostic.
- **Conversation weight:** the context ring in the chat header shows
  tokens by component and megabytes re-sent per turn. Or ask a
  participant for the `conversation_performance` diagnostic, which names
  the cause in words.
- **Pacing between speakers:** compare `created_at` deltas in `chat.db`.

## Two traps when reading the numbers

**Aggregation across tool rounds.** Usage accumulates over every API
call in a turn, so a turn with five tool rounds reports roughly five
times the context of a single call. Divide by the number of provider
calls before concluding that a conversation is enormous.

**Blended measurement windows.** A 24 hour window that spans a fix
contains both the before and the after. Read naively, that makes a fix
look like a regression on the day it landed. Compare like windows, or
wait for the window to clear the change.

## Not yet measured

Text chats record tokens and cost but no durations, so "how long did
that reply take" has no stored answer outside voice, which has its own
per-turn traces. That gap is tracked in the public issues.
