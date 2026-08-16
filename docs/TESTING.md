# What the test suites guarantee

Two suites, both keyless by design:

```sh
env -u OPENAI_API_KEY -u ANTHROPIC_API_KEY .venv/bin/python -m pytest -q
node --test frontend/src/*.test.js
```

CI runs them as separate steps, so a green pytest run is not the whole
gate. Nothing in either suite calls a provider: every network path is
mocked, and a test that needed a key would be a bug in the test.

Every suite file carries its own module docstring saying what it covers
and, usually, which failure it was written against. That is the per-suite
reference; a test asserts each file has one.

## The transcript

**The projection.** Each provider receives its own turns as assistant and
everyone else's as labelled user turns. This is the trick the whole app
rests on, so it is pinned per provider, including what happens when a seat
has never spoken and when a message carries attachments.

**Trusted context.** App-assembled context arrives as a system entry where
the provider supports one, and otherwise carries a per-process marker.
Tests forge the marker from inside a transcript and assert the forgery is
rejected.

**One insert path.** A guard test greps the source: any raw insert into
messages outside `db.insert_message` fails the build, with one named
exemption for the history importer. Live inserts must ring the notify bell
after committing, never before.

**Rounds and streaming.** A dropped connection does not cancel generation.
A reconnect replays from a watermark. Only a real abort marks a message as
cut off, and two rounds for one chat cannot interleave.

## The prompt cache

**Layout.** Stable content sits before the breakpoint and per-round content
after it. Tests assert which block each field lands in, because putting a
volatile field in the stable block silently re-writes the whole cached
prefix every turn. That costs money and changes no visible behaviour.

**Room state rides the volatile tail.** The engine hands every seat the
chat's room mode and present roster names as one short line. The cache-split
pins prove that flipping the mode or the roster moves no byte of the cached
prefix, and roster names are pinned out of the transcript turns.

## Voice and room mode

**Every live microphone is visible everywhere.** The relay registers
each capture session. It unregisters the moment capture ends, at the
clean done as well as abrupt closes, so the registry can never claim a
dead mic or hide a live one. A kill from any surface closes that
session's socket with a distinct code. The owning client treats the code
as a deliberate full stop. Two sessions in one chat are both shown - the
doubled-turn case. The banner rules are pure and node-tested.

**Identity work never starves a reply.** Everything the identity pass
runs on threads - clip banking, the hygiene audit, the crosstalk call -
uses its own bounded executor, never the default pool the request path
shares, and the audit runs at most once per cool-down window without ever
dropping a changed bank. A source-level guard keeps asyncio.to_thread out
of the module for good.

**Identity is local or honestly uncertain.** The on-device matcher names a
turn or the turn stays unresolved. No ElevenLabs call ever fires because the
matcher deferred: a solo utterance can never trigger one, every defer reason
takes the same silent exit, and with the matcher disabled nothing automatic
happens at all. The manual doors still arm. One trigger survives for the
batch diarize call, genuinely overlapping speech, and it is pinned as the
only metered voice-identity spend.

**The identity pass costs the live turn nothing.** Every pass is a
never-awaited background task, pinned by wedging the call open and watching
the send complete anyway. With the toggle off the realtime relay sends
byte-for-byte the frames it always sent. A failed pass leaves the turn
unlabelled with everything else working.

**A label lands on its own turn, or on nothing.** Writes key on the client's
turn id, the same id `/send` persists on the message, so a dropped short
interjection labels nothing rather than a neighbour. The id never reaches the
upstream byte stream.

**Uncertainty is never rendered as a name.** Confidently named turns enter
the projection as that person "(in the room)". Uncertain turns enter as an
unidentified speaker, never the owner and never the guessed name. A chat
with no labels at all renders byte-identically to what the builders always
produced. Crosstalk is marked rather than papered over, and forces
`guest:unknown` on memory ingest even after a human corrects who spoke.

### Names and banks

**Owner-set names are law.** A rename or a spoken correction sets a
preferred name owner-set, and from then on no automated path may change it.
The preferred name is pinned to win on every surface a name renders or ships:
chips, roster snapshot, projection heads, crosstalk splits, memory ingest,
and the STT keyterm hints. Variants merge instead of duplicating, a
close-but-not-confident name raises a merge question rather than rebinding,
and a forgotten person's name reappearing creates a fresh record.

**An AI participant can never be seated as a person.** Its name, spelt-by-ear
variants included, is dropped before seating, and the seat writer refuses the
exact names outright as a final guard.

**Learning a voice is narrow and labelled as such.** Identification below the
sufficiency bar stays uncertain, and the bar has two parts: a seconds target
and a minimum of short clips, so long clips cannot starve the short class.
Cold-start elimination applies only when exactly one present person cannot yet
be identified; an overlapping-speech verdict, two or more unidentifiable
people, a confident match, and an outright matcher failure each have their own
pin against qualifying. A learning label carries `learning` beside `uncertain`,
so a consumer written before the marker still treats it as a guess.

**The hygiene guard audits every bank change.** A clip closer to another
person's centroid than its own is quarantined: kept on disk, excluded from
matching, surfaced as set aside. Close centroid pairs widen the match margin
for exactly that pair. A bank nobody vouched for cannot re-seat anyone until
the owner confirms it.

**The health surface is content-free.** `GET /api/voice/health` returns
states, counts and milliseconds, never a name and never transcript text. The
per-chat last-decision record is bounded and written only from inside the
never-awaited passes, so the zero-latency law holds by construction.

## Guests, cost, boundaries

**Guest isolation.** Permissions come from this process rather than the
operator's own settings, and each visit gets its own worktree at a freshly
fetched base. On credential files the suite pins the asymmetry rather than a
guarantee: implement mode's options carry the `Read(.env)` family, investigate
mode's options carry no `Read` rule at all, and neither mode path-restricts
`Grep` or `Glob`. Every guest test mocks the SDK boundary, so what is asserted
is which rules are handed to Claude Code, not that the CLI refused a read.

**Cost and provenance.** Metered, subscription-equivalent and unknown never
merge. Provenance is stamped at write time and cannot be backfilled. An
unknown model id stays unpriced instead of inheriting a family rate, and the
usage endpoint exposes no combined total.

**Boundaries.** Non-loopback hosts are refused unless explicitly trusted,
cross-site API requests are rejected, websocket routes check Origin as well as
Host, routes that spawn a process from request data refuse a remote caller,
and read limits are bounded.

**The leak scanner.** It rejects real-shaped keys, infrastructure identifiers
and deny-listed personal content, passes documented placeholders, and the
committed tree must scan clean. A separate test plants a leak in a throwaway
repository to prove the tree walk can detect one, so the gate cannot rot into
a scan of nothing.

## Frontend

Rules live in pure `.js` modules. The suites cover cost formatting and the
subscription-versus-metered split, cache-health verdicts, the event and round
streams including reconnect and lost-wakeup handling, voice gating and
recovery, rate-card layering, and the header and spend views' arithmetic.

React components have no test infrastructure, deliberately. Anything that
exists only inside JSX has no automated guard, so running the app is part of
changing it.

## What neither suite covers

Whether the models say anything useful. Conversation quality, tool choice and
answer accuracy are judged by the eval harnesses in `eval_critic/` and
`eval_silence/` and by use, not by unit tests. Green CI means the machinery
keeps its promises.
