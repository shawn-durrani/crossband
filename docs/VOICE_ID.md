# Voice identification: where it falls short, and what to tune

Crossband can hear you and talk back. When more than one person is in
the room, it also works out who is speaking and puts a name on each
turn. To do that it keeps a short recording of each person it has
learnt, compares every new turn against those recordings on your own
computer, and names the speaker only when the match is clear. The app
calls this room mode, and the readme's
[Have more than one person in the room](../README.md#what-you-can-do)
has the short version.

The defaults were set using the voices of one family. If the app names
the wrong person, or nobody, start with
[What to check when identification misbehaves](#what-to-check-when-identification-misbehaves).

The app puts a name on a turn only when the voice check on your own
computer is sure. That check is called the matcher. When it isn't sure,
the turn stays unnamed, and no cloud service is asked to guess instead.
The only cloud transcription left in room mode runs when two people
talk over each other, to work out which words were whose.

## How the room switches on and off

The app keeps a list of who's in the room, called the roster, and
putting a person on that list is called seating them. Switching room
mode on is called arming the room.

The voice dock is the strip that holds the voice controls, and it
shows which of three states a chat is in. "listening" is the default,
where the room is off and the app switches it on by itself when it
hears a reason to. "room on · 2" means the room is on with two people
seated. "solo" means you switched the room off for this chat, and the
app won't switch it back on by itself.

The room arms itself from "listening" when:

- a voice the app remembers speaks, and it isn't yours
- a clear voice the app doesn't know speaks, and the app asks who's
  joined. This one needs your own voice learnt first, or the app
  can't tell a stranger from you.
- someone is introduced out loud
- you say or type "group mode", or use "switch on now" in the voice
  settings

Saying "solo mode" takes the chat to "solo", and so does "switch off
for this chat" in the voice settings. Either one marks everyone as
left and closes any open question about who's speaking. From "solo",
only an introduction, "group mode" or "switch on now" brings the room
back.

Once the room is on, each spoken turn lands in one of three states.
If the voice matches a remembered person, the turn gets their name.
If only one person in the room has no learnt voice, an unmatched turn
is theirs by elimination, and it's labelled with their name and
"learning this voice". If nobody fits, the app asks who joined.

```mermaid
stateDiagram-v2
  direction LR
  classDef node fill:#d4d4d8,stroke:#757575,color:#18181b
  classDef hero fill:#38bdf8,stroke:#0284c7,color:#18181b,stroke-width:2px
  classDef boundary fill:transparent,stroke:#757575,color:#757575
  state "room on" as room {
    named: turn named
    learning: learning this voice
    asked: asks who joined
    [*] --> named: a voice it knows
    [*] --> learning: one unlearnt person, by elimination
    [*] --> asked: nobody fits
    learning --> named: 6s of speech and 2 short clips stored
  }
  [*] --> listening
  listening --> room: a voice it remembers, or a new clear voice
  listening --> room: an introduction, saying group mode, or the switch in settings
  listening --> solo: saying solo mode
  room --> solo: saying solo mode, or the switch in settings
  solo --> room: an introduction, saying group mode, or the switch in settings
  class listening,solo,learning,asked node
  class named hero
  class room boundary
```

## Where the trigger phrases came from

Room mode listens for spoken introductions, like "say hi to Alex" or
"my mate Dave is here", and for the mode commands "group mode" and
"solo mode". The phrase lists that spot these came from real
sessions in one home, in English, and they won't cover everything
people say in yours.

What the phrase lists miss is caught another way.

- A small, cheap model reads any turn that looks even roughly like an
  introduction or a command. An unusual phrasing still lands as long
  as the rough shape is there.
- Recognising a voice needs no phrases at all. Every spoken turn gets
  a quiet voice check on your own computer, so a person the app
  already knows is recognised however they were greeted. That holds
  whether or not room mode is on. With the room armed, the check
  compares against everyone the app remembers, and a remembered
  person who isn't seated yet joins the room on their first turn.
- The third is the ask. When a clearly new voice appears and nothing
  on record explains it, the app asks who's speaking. It doesn't
  guess.

If a phrasing never triggers anything, the manual doors always work:
the two switches in the voice settings drawer, "switch on now" and
"switch off for this chat", and typing the command. There's no switch
in the dock itself, which shows the state while the automation does
the arming.

## Starting from nothing

Learning a voice from nothing is called a cold start, and it has one
route of its own. Every other route assumes the app has something to
work with. If a person's stored voice is empty, because you forgot
them or cleared your own record, none of them can help. Being
recognised needs stored clips, an introduction needs an
introduction-shaped sentence, and correcting a name needs a name on
the turn to correct.

The cold start needs room mode on, only one person in the room whose
voice isn't learnt yet, and everyone else present already
recognisable. A turn the matcher can't place is then worked out by
elimination. Anyone else in the room would have been recognised, so it
can only be the one unlearnt person. The app stores short clips of
each person's voice, called anchors, and one person's set of anchors
is their bank. The audio goes into that person's bank, and the turn is
labelled with their name and "learning this voice". That's a name
worth using and not yet worth trusting.

A bank with 6 seconds of clear speech and 2 short clips in it is
enough to identify someone, and the app calls such a bank sufficient.
The two numbers are the settings `voice_id_sufficient_seconds` and
`voice_id_min_short_clips`. Once a person's bank is sufficient, their
voice is remembered and ordinary recognition takes over. From then
on, a match that clears the naming bar with room to spare tops up
their bank with that turn's audio, which the app calls accumulation.
The cold start is what lets a new guest be learnt while you sit in
the room, already recognised.

### Where elimination never applies

Elimination is only sound when there's one candidate, so the rule is
kept narrow. It never applies:

- with two or more unlearnt people in the room, which is what the ask
  is for
- when two voices overlap on one turn
- with room mode off, where the app can't tell you from a stranger
  with nothing on record
- in place of a confident match

If the app got it wrong, tap the "learning" label to correct it, the
same as any other label, and the correction feeds the right person.

### An introduction outranks a voice match

When someone introduces themselves under a name that isn't a spelling
of anyone the app remembers, the introduction wins over a voice match.
The new person is seated under the name your own ears heard, and the
voice resemblance becomes a merge question for you to answer. If the
name could be a spelling of a remembered person's name, in any form
the app has recorded for them, a confident voice match still
re-identifies them quietly, because that's the same human. "Sounds
like Sam" has banked the wrong person before, and one tap on a
question is cheaper than an afternoon of stolen turns.

A model's name can never be seated as a person. The models are
addressed by name in nearly every spoken sentence, and a mishearing
like "This is Claude..." looks like an introduction. Elimination is
only as sound as the roster it reads. If that mishearing seated the
model as a person, elimination would learn a human voice under the
model's name, clip by clip, until a person who doesn't exist was a
remembered voice. The app spots your own name in its misspelt forms,
and it spots a model's name the same way. Any such name is dropped
before seating, and the seat writer refuses the exact names outright
as a final guard.

## English bias

Two separate parts lean English. The first is the phrase lists and the
confirmation step, so an introduction or a command spoken in another
language will often not be recognised. Use the switches in the voice
settings drawer, or type the command. Recognising a voice the app
already knows still works in any language, because it listens to the
voice and not to the words.

The second is the speaker model, which ships trained on English, and
every threshold here was calibrated with English speech. Matching goes
on how a voice sounds more than on what it says, so other languages
are expected to work. Nobody has measured them, so the calibration
promises hold for English only.

When the matcher can't name a turn, the turn stays unnamed, and room
mode then arms only through a voice it knows or the manual doors.

## The model itself

The matcher runs `nemo_en_titanet_small`, Nvidia's
[NeMo TitaNet-Small](https://catalog.ngc.nvidia.com/orgs/nvidia/teams/nemo/models/titanet_small)
speaker model. It's about 38MB and licensed
[CC-BY-4.0](https://creativecommons.org/licenses/by/4.0/), which lets
anyone use it as long as Nvidia is credited. It runs on your computer
through
[sherpa-onnx](https://k2-fsa.github.io/sherpa/onnx/index.html), an
open-source speech toolkit. The app pins the model by its download
address and its SHA-256, a hash of the file that changes if a single
byte does. It fetches the file once from the sherpa-onnx releases into
`<data_dir>/voice_models/` and checks that hash before first use.
`GET /api/voice/health` reports the live model's file name, the start
of its hash, and whether the built-in pin was overridden.

You can swap the pin: `voice_id_model_url` and `voice_id_model_sha256`
in [CONFIG.md](CONFIG.md) override it together, both or neither. A new
address checked against the old hash fails verification, and the
matcher stays unavailable. It never runs an unverified file.

Before you swap, know what a swap costs.

- Every threshold here was calibrated for TitaNet-Small's score
  distribution. A different model needs its own calibration, using
  the knobs in [The tuning knobs](#the-tuning-knobs).
- Stored voices survive a swap on their own, because the app keeps the
  anchor clips and never the model's output. A voice fingerprint lives
  only in memory and is rebuilt from the clips by whichever model is
  loaded, so after a swap and a restart there's nothing to migrate and
  no mixed comparisons. Matching warms up again from the same clips.

## The tuning knobs

The full list, each knob with its default and what it controls, is
the Voice table in [CONFIG.md](CONFIG.md). Every one can be set in
`config.local.json` or as a `CROSSBAND_*` environment variable. The
defaults may need moving when voices in your house sound alike, like
siblings or a parent and an adult child. They were calibrated on the
model's published benchmarks plus the voices in one home.

Change one knob at a time, and check the voice dock first. Its top
row leads with the room state ("room on · N", "listening" or "solo").
Next comes one chip per person, which shows a tick once their voice is
remembered, or "learning 4s" while it's still being learnt, and the
row ends with how fast the last turn was identified. The tick
describes the stored voice and says nothing about the turn being
spoken. Each turn's own label shows the live attribution, and a hard
turn can stay uncertain under a green tick. The matcher's own state
and any "sound close" warning sit behind the settings button beside
the controls, along with the manual room switches.

## When two people sound alike

Two people who sound alike are the hardest case. The app is built to
stay quiet when in doubt, so when it can't tell them apart it leaves
the turn unnamed.

- The hygiene guard audits the stored voices whenever they change. A
  stored clip that sounds more like a different person than its own
  is set aside, kept on disk, shown as "clips set aside" under
  Remembered voices, and left out of matching. Two people whose
  stored voices sit too close are flagged behind the settings button
  ("Alex and Sam sound close - matching is stricter"), and the
  matcher demands a wider winning margin between those two.
- If mix-ups still slip through, raise `voice_id_margin` first, then
  `voice_id_threshold`. Expect more unnamed turns in exchange for
  fewer wrong names. No setting buys certainty for free.
- Tapping a named turn to correct it fixes the label and feeds the
  corrected audio to the right person as ground truth. That's the
  fastest way to pull two confusable voices apart.

## The owner's ear

The hygiene audit can't catch a bank that is wholly someone else's
voice under the wrong name, because every clip in it agrees with every
other. It compares clips in pairs, so what it catches is a bank with
someone else's clips mixed in. A bank that's wholly wrong has one
shape. It reached enough speech without a single spoken introduction
or correction from you, by cold start and accumulation alone.

The app keeps only the best few clips in a bank, so older clips are
dropped as better speech arrives. That's rotation. A bank is vouched
the moment a human stands behind it, which happens when an
introduction banks into it or when you correct a turn into it. The
stamp is on the person, so it survives rotation and merges.

A sufficient bank nobody vouched for asks for your ear under
Remembered voices: listen to its clips and confirm. Until you do, a
bank the app watched become sufficient is paused. A paused bank is
left out of the candidate list the matcher checks a voice against, so
it can neither name nor seat anyone in a session. The armed room, the
room-off check and the early check that runs in the pause before a
turn ends all build that list the same way, so no path can drift.

The one exception to the pause is a person already seated in the live
chat, whether the session is still learning them by elimination or a
human placed the seat. They keep being identified, because the pause
guards re-seating and leaves the seat alone. A real person unlocks a
paused bank at once by introducing themselves, since the introduction
vouches the bank. A bank that was already sufficient before the app
began recording that crossing keeps working while flagged, so an
upgrade takes nothing away.

Vouching can also be outlived. Each time accumulation banks a clip,
the app records the score it matched at. When rotation has replaced
every clip a human stood behind, those scores decide the bank's
standing. Weak scores pause identification until your ear confirms
the voice again, and strong scores keep it working, with a note under
Remembered voices. The exception for someone already seated applies
to this pause too. Clips stored before scores were recorded carry
none, and a bank made of those keeps working.

## The durable home

Learnt voices have a second home in
[membro](https://github.com/shawn-durrani/membro), the optional memory
service that remembers from one conversation to the next. With membro
set up (`MEMORY_AUTH_TOKEN` in the environment), a background pass
uploads accepted clips to membro's person records, pulls in people
this install doesn't hold, and obeys forget marks. Forgetting a person
in either app deletes the stored audio in both. The pass runs at
startup, after rounds, and the moment you forget someone, always on a
worker thread that no turn waits for. If membro is down, the pass logs
once and does nothing. Identification never waits on it.

Your corrections travel too. Moving a clip to the right person,
deleting one, merging duplicate people, or forgetting someone here is
recorded and replayed against membro on the next pass. The durable
record then reflects your judgement, and a rebuild can never bring
back a recording you corrected away. A correction made while membro
is down waits for the next pass.

Forget doesn't wait for that pass. It sends the forget the moment you
press it, and a later pass retries one membro couldn't take. Membro
deletes its copy of the audio and sends the facts it learned from that
person back to review. Forgetting someone also settles any waiting
correction that named them. The other record in a merge they won is
forgotten too, and a clip moved into them is deleted at its source.
Merging two people settles them the same way, and a waiting
correction that named the merged-away person now names the survivor.

## Scale bounds

- The roster holds 6 people at once by default (`room_roster_max`),
  and the cap frees as people leave. It's a product choice, and
  nothing technical forces it.
- The transcription service that splits crosstalk can tell apart up
  to 32 voices in one request. The roster cap keeps real sessions
  nowhere near that.
- One Crossband instance belongs to one person, and everything about
  memory, spend and trust assumes it. Guests are remembered voices
  with names, never co-owners. That's how the whole fleet is built,
  and no knob changes it.

## What to check when identification misbehaves

1. The voice dock's settings button. On a phone, tap the one-line
   summary to open it. Is the matcher `ready`? A `fetching` or
   `unavailable` matcher means no turns are named, and nothing arms by
   itself until it recovers.
2. The chips on the dock's top row, and Remembered voices on the
   Voices page. Does each person show a tick, or are they still
   learning? Are clips set aside? Is there a "sound close" warning?
3. The knobs, one at a time.
4. If a name is wrong, tap the name on the turn and correct it. The
   correction is final, and no automated step will change it back.
