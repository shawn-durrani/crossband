# Architecture

Crossband keeps one neutral transcript in SQLite and turns it into each
provider's two party format at request time. A round runs as a
background task that writes into a replayable buffer, so a dropped
connection never kills a reply. Tools, costs and memory are shared, and
whatever one participant looks up, every participant can see. The
decisions are settled, and a change that crosses one starts as an
issue.

## The shape

```
app.py          - FastAPI wiring, loopback guard, cross-site rejection
routers/        - one file per HTTP surface; app.py mounts them
db.py           - SQLite (WAL); one insert path; the notify bell
engine.py       - a round: who speaks, in what order, with what context
rounds.py       - the per-chat buffer a round writes and HTTP tails
providers.py    - per-vendor projection, streaming, prompt-cache layout
tools.py        - shared tools every seat can call
memory_client.py - the optional Membro bridge; absent means memoryless
egress.py       - the vetting proxy every model-influenced URL exits by
url_ledger.py   - fetchable means already seen from a non-model source
browse.py       - rendered viewing; browse_worker.py is the keyless child
guest.py        - Claude Code as a summonable participant
voiceid.py      - the voice matcher; anchors.py is its clip store,
                  introductions.py the phrase and seating layer, and
                  diarize.py crosstalk splitting and the label passes
accounting.py   - cost with provenance; provenance.py defines the axes
frontend/       - React UI; pure .js modules hold the rules and are tested
```

## The projection

A model only understands a conversation between two parties, itself and
one user. The store holds a neutral transcript with every speaker in
it. At request time each provider sees its own messages as `assistant`
turns, and everyone else's messages, human and AI alike, as labelled and
timestamped `user` turns. That one trick is what makes a real group chat
work over APIs that have no idea of one.

## Rounds are detached from the connection

A round is a background task writing into a buffer for its chat, and an
HTTP response tails that buffer. Closing the tab doesn't cancel a reply.
Reconnecting replays from the client's watermark. Stopping is a separate
abort, and only a real abort earns the "cut off" marker on a message.

## The prompt cache splits on independence

A provider's prompt cache only helps when the start of the prompt is the
same as last time. Content goes before the cache breakpoint if it
changes only when the transcript changes, and after it if it can change
on its own. The question to ask of a field is whether it moves on its
own, apart from the transcript. Put such a field in the cached block and
the whole prefix is written again every turn, which costs more than it
saves and never shows in a dollar total. The ratio of cache reads to
cache writes is the signal.

## App context is an unforgeable channel

Context the app assembles is delivered as a `system` entry in the middle
of the conversation, where the provider supports one. Where it doesn't,
the context carries a random marker made once per process and named
only inside the cached system prompt. Untrusted content, such as pastes,
fetched pages, tool results and other participants, can't contain that
marker, so nothing inside the transcript can pretend to be app context.

## One insert path

Every live message goes through `db.insert_message`, which commits
before it rings the notify bell. A raw insert anywhere else fails the
build. The database is the catch up buffer and the bell only saves
latency. Every connect and reconnect replays from a watermark before it
waits, so a missed wakeup delays a message and never loses one.

## External producers are namespaced, never trusted

An ingested event is stored under the speaker `ext:<source>`. The
speaker flows into the transcript the models read, so a producer with
no namespace that called itself "claude" could pass as a participant.
The app holds no registry of allowed producers and reads nothing into
the payload. The namespace does the work.

## A guest's abilities are decided in code, before it starts

A summoned Claude Code session runs with permissions this process
injects, and it never reads the operator's personal settings. It works
in its own git worktree at a fresh checkout. Denied tools override
allowed ones, which is how the two modes with a shell, implement and
run, keep a broad read permission away from `.env` and
`config.local.json`. The default investigate mode denies whole tools
and carries no path rule, so it doesn't restrict what a read only guest
may open. The list bounds built in tools only, and any MCP server
mounted for the guest is available whole.

## A recorded dollar is not a charged dollar

Costs sort into metered, subscription equivalent and unknown, and the
three are never added together. Provenance is stamped when the turn is
written, so editing a rate card later can't rewrite history. Pricing
fails closed. An unknown model id stays unpriced and never inherits a
family rate, and its seat stays in trial until someone prices it by
hand. The usage endpoint returns no combined total, because a
convenient number beside a split is the one people quote.

## Rules live in pure modules

Anything worth guarding lives in a plain `.js` module with a
`node --test` suite. React files hold rendering and wiring.

One render smoke sits beside those suites. It mounts the real message
list against fixture messages, so a crash at render time fails CI
before it can blank every chat. Effects don't run under
`renderToStaticMarkup`, so the smoke guards the render path and nothing
else. Behaviour that exists only inside a component still has no
automated guard, and running the app is part of changing it.
