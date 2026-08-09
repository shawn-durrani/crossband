# Voice identification: known limitations and tuning

Who this page is for: anyone running Crossband's room mode in their own
house. It states plainly where voice identification was grown, where it is
likely to fall short for you, and which knobs to turn when it does. It was
calibrated in one household; yours is different, and this page is the
honest map of that gap.

The one design rule to hold onto while reading: **identity is local or
honestly uncertain**. The app names a speaker only when the on-device
matcher is confident. When it is not confident, the turn simply stays
unnamed. There is no cloud service guessing names behind the scenes; the
only cloud transcription left in room mode runs when two people talk over
each other, to untangle who said what. A wrong name is treated as worse
than no name, everywhere.

## Where the trigger phrases came from

Room mode listens for spoken introductions ("say hi to Alex", "my mate
Dave is here") and mode commands ("group mode", "solo mode"). The phrase
lists that spot these were grown from one household's real sessions, in
English, and they will not cover everything your household says.

Three things catch what the phrase lists miss:

- A cheap confirmation model reads any turn that looks even vaguely like
  an introduction or a command, so unusual phrasings still land as long
  as the rough shape is there.
- Ambient detection does not need phrases at all: every spoken turn gets
  a quiet on-device voice check, so a person the app already knows is
  recognised by voice alone, however they were greeted. This holds
  whether or not room mode is already on: with the room armed, the
  check compares against everyone the app remembers, not just the
  people already listed as present, and a recognised remembered voice
  joins the room on their first turn.
- The ask-fallback: when a clearly new voice appears and nothing on
  record explains it, the app asks who is speaking rather than guessing.

If a phrasing never triggers anything, the manual doors always work: the
room switches in the voice settings drawer ("switch on now" and "switch
off for this chat"), or typing the command. There is no toggle in the
tray itself any more - the tray shows the room state ("room on · N",
"listening" or "solo") and the automation does the arming.

## Starting from nothing: cold-start enrolment

Every route above assumes the app has something to work with. If your
stored voice is empty - you forgot a person, or cleared your own record -
none of them can help: being recognised needs stored clips, an
introduction needs an introduction-shaped sentence, and correcting a name
needs a name on the turn to correct.

So there is one route that needs none of that. With room mode ON and
exactly one person in the room whose voice is not yet learnt - everyone
else present already recognisable - a turn the matcher cannot place is
worked out by elimination: anyone else in the room would have been
recognised, so it can only be the one unlearnt person. The audio is
stored towards learning that voice, and the turn is labelled with their
name, marked "learning this voice" - a name worth using, not yet worth
trusting. Once enough speech is stored, the voice is remembered and
ordinary recognition takes over. (This is what lets a genuinely new
guest be learnt while the owner sits in the room, already recognised.)

It is deliberately narrow, because elimination is only sound when there
is genuinely one candidate:

- never with two or more unlearnt people in the room (that is what the
  ask-fallback is for),
- never when two voices overlap on one turn,
- never with room mode off, where the app cannot tell you from a
  stranger with nothing on record,
- and never instead of a confident match.

The label is a normal label: tap it to correct it if the app got it
wrong, and the correction feeds the right person instead.

## English bias, stated plainly

Two separate things lean English:

- **The phrase lists and the confirmation step.** Introductions and
  commands spoken in other languages will often not be recognised. Use
  the switches in the voice settings drawer or typed commands instead;
  ambient recognition of already-known voices still works regardless of
  language, because it listens to the voice, not the words.
- **The speaker model.** The on-device voice matcher ships as an
  English-trained model, and every threshold in this document was
  calibrated with English speech. Voice matching itself is largely about
  how a voice sounds rather than what it says, so other languages mostly
  still work, but the calibration promises below are not measured
  promises for them.

What degradation looks like since the cloud identity path was retired:
turns that cannot be confidently named stay unnamed, and room mode arms
only through voice recognition of known people or the manual doors.
Functional, not magical. (Older builds fell back to a cloud pass with
"Voice 1"-style labels; that path produced a confidently wrong name in
field testing and was removed on purpose.)

## The tuning knobs

All of these live in [CONFIG.md](CONFIG.md) and can be set in
`config.local.json` or as `CROSSBAND_*` environment variables. Defaults
were calibrated on the model's published benchmarks plus a single
household. Similar-sounding voices in your house (siblings, a parent and
an adult child) are exactly the case where the defaults may need moving.

| knob | default | what it controls |
|---|---|---|
| `voice_id_threshold` | `0.5` | How similar a voice must be to a stored one before it can be named at all. Raise it and fewer, more certain names appear; lower it and more turns get named, less certainly. |
| `voice_id_margin` | `0.12` | How clearly the best match must beat the second-best before it is trusted. This is the knob that protects similar-sounding households; the hygiene guard widens it automatically for any two stored voices it finds sitting close together. |
| `voice_id_sufficient_seconds` | `6.0` | How much clear speech must be stored before a person's voice is trusted for identification at all. Below the bar their turns stay uncertain. |
| `voice_id_min_short_clips` | `2` | The second half of that bar: how many short (one-to-two-second) clips the stored voice must include, so quick interjections ("yes", "hang on") can be recognised, not just full sentences. |
| `room_roster_max` | `6` | How many people the room can hold at once. |

When tuning, change one knob at a time and check the voice dock first.
Its top row leads with the room indicator ("room on · N", "listening" or
"solo") and shows one chip per person - a tick once their voice is
remembered, "learning 4s" while it is still being learnt - plus how fast
the last turn was identified. The matcher's own state and any "sound
close" warning sit behind the settings button beside the controls, along
with the manual room switches.

## Similar-voice households

Two people who genuinely sound alike are the hardest case, and the app
is built to fail towards silence there rather than towards mix-ups:

- The **hygiene guard** audits the stored voices whenever they change. A
  stored clip that sounds more like a different person than its own is
  set aside (kept on disk, shown as "clips set aside" under Remembered
  voices, and no longer used for matching). Two people whose stored
  voices sit too close are flagged in the health strip ("Alex and Sam
  sound close - matching is stricter") and the matcher automatically
  demands a wider winning margin between exactly those two.
- If mix-ups still slip through, raise `voice_id_margin` first, then
  `voice_id_threshold`. Expect more unnamed turns in exchange for fewer
  wrong names; that trade is deliberate and there is no setting that
  buys certainty for free.
- Tap-to-correct on any named turn both fixes the label and feeds the
  corrected audio to the right person as ground truth, which is the
  fastest way to pull two confusable voices apart.

## Scale bounds

- Roster cap: 6 people at once by default (`room_roster_max`). The cap
  frees as people leave, and it is a product choice, not a technical
  limit.
- The crosstalk-splitting transcription service clusters up to 32 voices
  per request; the roster cap keeps real sessions far below that.
- Single owner by design: one Crossband instance belongs to one person,
  and everything about memory, spend and trust assumes it. Guests are
  remembered voices with names, never co-owners. This is the fleet's
  architecture, not an accident, and no knob changes it.

## What to check when identification misbehaves

1. The voice dock's settings button (or, on a phone, tapping the one-line
   summary open). Is the matcher `ready`? A `fetching` or `unavailable`
   matcher means no turns are named and nothing arms automatically until
   it recovers.
2. The chips on the dock's top row, and Remembered voices (settings).
   Does each person show a tick, or are they still learning? Are clips
   set aside? Is there a "sound close" warning?
3. The knobs above, one at a time.
4. If a name is wrong, tap the name on the turn and correct it. The
   correction is law: no automated step will change it back.
