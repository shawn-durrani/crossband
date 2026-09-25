# What the test suites guarantee

There are two suites, and neither needs a key.

```sh
env -u OPENAI_API_KEY -u ANTHROPIC_API_KEY .venv/bin/python -m pytest -q
npm --prefix frontend test
```

The frontend command runs three gates in turn: `eslint`, the
`node --test` rule suites, and the render smoke. CI runs the three as
separate steps, so a green pytest run isn't the whole gate. Nothing in
either suite calls a provider. Every network path is mocked, and a test
that needed a key would be a bug in the test.

Every suite file opens with a docstring saying what it covers and,
where there was one, which failure it was written against. Those
docstrings are the per-suite reference, and a test checks each file
has one.

## The transcript

The projection is the trick the whole app rests on. Each provider gets
its own past turns as the assistant and everyone else's as labelled
user turns. It's pinned per provider, including what happens when a
seat has never spoken and when a message carries attachments.

Context the app assembles arrives as a system entry where the provider
supports one, and otherwise carries a marker that changes each time the
process starts. Tests forge the marker from inside a transcript and
check the forgery is rejected.

There is one insert path for messages. A guard test reads the source,
and any raw insert into the messages table outside `db.insert_message`
fails the build, with one named exemption for the history importer. A
live insert has to ring the notify bell after it commits, never before.

Rounds survive the connection. A dropped connection doesn't cancel
generation, a reconnect replays from a watermark, only a real abort
marks a message as cut off, and two rounds for one chat can't
interleave.

## The prompt cache

The layout is pinned. Stable content sits before the cache breakpoint
and per-round content after it. Tests check which block each field
lands in, because a volatile field in the stable block rewrites the
whole cached prefix every turn. That costs money and changes nothing
you can see.

Room state rides the volatile tail. The engine hands every seat the
chat's room mode and the names present as one short line. The
cache-split pins prove that flipping the mode or the roster moves no
byte of the cached prefix, and that roster names stay out of the
transcript turns.

## Voice and room mode

Every live microphone is visible everywhere. The relay registers each
capture session and unregisters it the moment capture ends, at a clean
finish and at an abrupt close alike, so the registry can never claim a
dead mic or hide a live one. A kill from any surface closes that
session's socket with its own code, and the owning client treats the
code as a full stop. Two sessions in one chat are both shown, which is
the doubled-turn case. The banner rules are pure and node-tested.

A finished turn survives a transcription failure. The app records every
turn a second time while it streams, and when realtime transcription
fails with a turn still waiting for its words, that copy goes to
standard transcription and the turn is sent once. A late realtime
result can't send it again, whichever arrives first. A failure with no
turn waiting changes nothing else. When the last piece of a long turn
comes back empty, the pieces already heard are still sent. The suite
drives the real voice client through each case.

Identity work never starves a reply. Everything the identity pass runs
on threads, which is clip banking, the hygiene audit and the crosstalk
call, uses its own bounded executor and never the default pool the
request path shares. The audit runs at most once per cool-down window
without ever dropping a changed bank, and a source-level guard keeps
`asyncio.to_thread` out of the module.

Identity is local or it stays uncertain. The on-device matcher names a
turn or the turn stays unresolved. No ElevenLabs call ever fires
because the matcher deferred: a solo utterance can never trigger one,
every defer reason takes the same silent exit, and with the matcher off
nothing automatic happens at all. The manual doors still arm. One
trigger survives, the batch diarize call for speech that overlaps, and
it's pinned as the only metered voice-identity spend.

The identity pass costs the live turn nothing. Every pass is a
background task nobody awaits, pinned by wedging the call open and
watching the send complete anyway. With the toggle off the realtime
relay sends byte for byte the frames it always sent. A failed pass
leaves the turn unlabelled with everything else working.

A label lands on its own turn, or on nothing. Writes key on the
client's turn id, the same id `/send` stores on the message, so a
dropped short interjection labels nothing and never a neighbour. The
id never reaches the upstream byte stream.

Uncertainty is never rendered as a name. A confidently named turn
enters the projection as that person "(in the room)". An uncertain
turn enters as an unidentified speaker, never the owner and never the
guessed name. A chat with no labels at all renders byte for byte as
the builders always produced. Crosstalk is marked as crosstalk, and
it forces `guest:unknown` on memory ingest even after a human
corrects who spoke.

### Names and banks

Owner-set names are law. A rename or a spoken correction sets a
preferred name as owner-set, and from then on no automated path may
change it. The preferred name is pinned to win on every surface a name
renders or ships: chips, the roster snapshot, projection heads,
crosstalk splits, memory ingest, and the STT keyterm hints. Variants
merge without duplicating, a close but unconfident name raises a merge
question and leaves the binding alone, and a forgotten person's name
reappearing creates a fresh record.

An AI participant can never be seated as a person. Its name, spelt by
ear variants included, is dropped before seating, and the seat writer
refuses the exact names outright as a final guard.

Learning a voice is narrow and labelled as such. Identification under
the sufficiency bar stays uncertain, and the bar has two parts, a
seconds target and a minimum of short clips, so long clips can't starve
the short class. Cold-start elimination applies only when one present
person can't yet be identified. An overlapping-speech verdict, two or
more unidentifiable people, a confident match, and an outright matcher
failure each have their own pin against qualifying. A learning label
carries `learning` beside `uncertain`, so a consumer written before the
marker still treats it as a guess.

The hygiene guard audits every bank change. A clip closer to another
person's centroid than its own is quarantined: kept on disk, excluded
from matching, and shown as set aside. Close centroid pairs widen the
match margin for that pair alone. A bank nobody vouched for can't
re-seat anyone until the owner confirms it.

The health surface holds no content. `GET /api/voice/health` returns
states, counts and milliseconds, never a name and never transcript
text. The per-chat last-decision record is bounded and written only
from inside the passes nobody awaits, so the zero-latency rule holds by
construction. The newer evidence surfaces keep the same floor: the
per-chat decision history, the parked-label outcomes and the one-tap
diagnostics dump carry turn ids, outcome words, timings and capped
error text, never speech.

The app saves that dump by itself when voice looks stuck: a finished
turn the server still hasn't saved 30 seconds later, a round that goes
quiet without ending, or speech stranded behind a round that won't
finish. It saves at most once every ten minutes and three times per
page load, and the server takes at most one automatic dump every five
minutes whichever device asks. The suite runs a spoken turn through the
real voice client and checks that none of its words reach the saved
file or the server log.

## Guests, cost, boundaries

A guest is isolated. Its permissions come from this process and never
from the operator's own settings, and each visit gets its own worktree
at a freshly fetched base. On credential files the suite pins an
asymmetry and no guarantee: the options for implement and run modes
carry the `Read(.env)` family, investigate mode's options carry no
`Read` rule at all, and no mode path-restricts `Grep` or `Glob`. Every
guest test mocks the SDK boundary, so what's asserted is which rules
are handed to Claude Code, not that the CLI refused a read.

Cost and provenance stay apart. Metered, subscription-equivalent and
unknown never merge. Provenance is stamped at write time and can't be
backfilled. An unknown model id stays unpriced and inherits no family
rate, and the usage endpoint exposes no combined total.

The boundaries hold. Non-loopback hosts are refused unless you've
trusted them, cross-site API requests are rejected, websocket routes
check Origin as well as Host, routes that spawn a process from request
data refuse a remote caller, and read limits are bounded.

The leak scanner is proved to work. It rejects real-shaped keys,
infrastructure identifiers and deny-listed personal content, passes
documented placeholders, and the committed tree must scan clean. A
separate test plants a leak in a throwaway repository to prove the tree
walk can find one, so the gate can't rot into a scan of nothing.

## Frontend

Rules live in pure `.js` modules. The suites cover cost formatting and
the split between subscription and metered spend, cache-health
verdicts, the event and round streams including reconnect and lost
wakeups, voice gating and recovery, rate-card layering, and the
arithmetic of the header and spend views.

React components have no behaviour-test setup, and one render smoke is
the exception. `frontend/scripts/render-smoke.mjs` bundles
`src/renderSmoke.entry.jsx` and mounts the real message list against
fixture messages. A render-time crash, a free identifier or a bad prop
shape fails CI there, before it can blank every chat. Effects don't run
under `renderToStaticMarkup`, so behaviour inside a component still has
no automated guard, and running the app is part of changing it.

## What neither suite covers

Whether the models say anything useful. Conversation quality, tool
choice and answer accuracy are judged by the eval harnesses in
`eval_critic/`, `eval_silence/`, `eval_attribution/`, `eval_recall/` and
`eval_intent/`, and by use,
never by unit tests. Green CI means the machinery keeps its promises.
