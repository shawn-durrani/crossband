# Architecture

Crossband keeps one neutral transcript in SQLite and projects it into
each provider's two-party format at request time. A round runs as a
background task writing into a replayable buffer, so a dropped
connection never kills a reply. Tools, costs and memory are shared
state: whatever one participant looks up, every participant can see.
The decisions below are settled.

## The shape

```
app.py          - FastAPI wiring, loopback guard, cross-site rejection
db.py           - SQLite (WAL); one insert path; the notify bell
engine.py       - a round: who speaks, in what order, with what context
providers.py    - per-vendor projection, streaming, prompt-cache layout
tools.py        - shared tools every seat can call
guest.py        - Claude Code as a summonable participant
accounting.py   - cost with provenance; provenance.py defines the axes
frontend/       - React UI; pure .js modules hold the rules and are tested
```

## The projection

Models only understand two-party conversations. The store holds a
neutral transcript; at request time each provider sees its own messages
as `assistant` turns and everyone else's, human and AI alike, as
labelled and timestamped `user` turns. That single trick is what makes a
real group chat work over APIs that have no concept of one.

## Rounds are detached from the connection

A round is a background task writing into a per-chat buffer that HTTP
responses tail. Closing the tab does not cancel a reply, and reconnecting
replays from the client's watermark. Stopping is an explicit abort, and
only a real abort earns the "cut off" marker on a message.

## The prompt cache splits on independence, not on volatility

Content rides before the cache breakpoint if it changes only when the
transcript changes, and after it if it can change on its own. The test is
not "does this field move?" but "does it move independently of the
transcript?" Getting this wrong re-writes the whole cached prefix every
turn, which costs more than it saves and is invisible in a dollar total:
the ratio of cache reads to writes is the signal.

## App context is a channel, not a louder claim

Context the app assembles is delivered as a mid-conversation `system`
entry where the provider supports one. Where it does not, it carries a
per-process random marker named only inside the cached system prompt.
Untrusted content (pastes, fetched pages, tool results, other
participants) cannot contain that marker, so app context is unforgeable
from inside the transcript.

## One insert path

Every live message goes through `db.insert_message`, which commits
before it rings the notify bell. A raw insert anywhere else fails the
build. The database is the catch-up buffer and the bell is only latency:
every connect and reconnect replays from a watermark before waiting, so a
missed wakeup delays a message rather than losing it.

## External producers are namespaced, never trusted

An ingested event is stored as `ext:<source>`. The speaker flows into
the transcript models read, so an un-namespaced producer calling itself
"claude" could impersonate a participant. The app holds no registry of
allowed producers and stays payload-agnostic; the namespace does the
work.

## A guest's abilities are decided in code, before it starts

A summoned Claude Code session runs with permissions injected by this
process, not read from the operator's personal settings, and in its own
git worktree at a fresh checkout. Denied tools override allowed ones,
which is how implement mode keeps a broad read permission away from
`.env` and `config.local.json`. That protection is mode-specific and not
a property of the guest framework: the default investigate mode denies
whole tools and carries no path rule, so it does not restrict what a
read-only guest may open. Two further caveats ship with it: the list
bounds built-in tools only, and any MCP server mounted for the guest is
available whole.

## A recorded dollar is not a charged dollar

Costs sort into metered, subscription-equivalent, and unknown, and the
three are never summed. Provenance is stamped when the turn is written,
so editing a rate card later cannot rewrite history. Pricing fails
closed: an unknown model id stays unpriced rather than inheriting a
family rate, and its seat stays in trial until someone prices it
deliberately. The usage endpoint returns no combined total, because a
convenient number beside an honest split is the one people quote.

## Rules live in pure modules

Anything worth guarding lives in a plain `.js` module with a
`node --test` suite. React files hold rendering and wiring. There is
deliberately no component-test infrastructure, and the consequence is
stated rather than hidden: logic that exists only inside JSX has no
automated guard, so running the app is part of changing it.
