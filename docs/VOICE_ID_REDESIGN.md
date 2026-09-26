# Voice identity, redesigned

When two or more people talk in a Crossband voice chat, the app has to
work out who said each thing. The AIs use that to answer the right
person, and memory uses it to file each fact under the right name.
Today the app judges every turn on its own, against one fixed score.
In a real two-person evening it named only 16 of 55 stretches of speech
correctly on the Mac, and 10 wrongly. The plan here replaces that with
the approach the research and the working products use: follow each
voice through the whole conversation, then name each voice once, with a
score that means the same thing for everyone.

Risk of acting: it's the biggest change to voice since room mode, and
it touches every spoken turn. The new path runs beside today's in
shadow first, changes nothing until it beats today's on your own
sessions, and then stays one setting away from today's until the old
code is deleted. Risk of leaving it: we keep patching. Of 47 voice
identity issues since August, 21 repaired or reversed an earlier fix,
and wrong or missing names still reach the AIs and memory every
evening.

What prompted it: the 25 and 26 September sessions, and the owner's
call on 26 September to stop hacking and redesign from a strong basis,
with the NVIDIA local stack in and nothing that isn't released yet.

## What you should see when it works

- Every spoken turn shows who said it. When two people talk at once,
  each part shows its own speaker.
- A known person is named during their first proper turn of a session,
  and every later turn of theirs is named straight away.
- A new voice gets one question, "Someone new is talking. Who's
  this?", not one per turn. Once you answer, every turn they've said
  in the session takes their name, and the app starts learning them.
- A wrong name is rare, about one turn in a hundred at most. When the
  app isn't sure, it says so, and it never passes a doubtful turn off
  as you.
- It works the same in every mode. Room on, room off and solo differ
  only in whether the app asks about new people and learns them.
- Nothing about anyone's voice leaves the Mac.

## Why today's approach keeps breaking

Each point here is measured on this Mac or found in the code.

1. **It names each turn alone.** A turn is compared with each person's
   voice as a whole, and the next turn starts from nothing. It never
   uses the fact that the same voice was named a moment ago. Every
   speaker model is about four times less accurate on 1 second of
   speech than on 5, so short turns are guesses.
2. **A turn with two voices matches nobody.** The whole turn becomes
   one fingerprint, a blend of both people. In the 25 September
   session, naming whole turns got 16 stretches right and 10 wrong, and
   sent 23 to the cloud. Splitting each turn by voice first and then
   naming the pieces got 46 right and none wrong, with the same model.
3. **One fixed bar judges everyone.** A turn is named when its score
   passes 0.5, whoever the person is. Some voices sit near that bar
   even when they're clearly the best match. On 26 September a guest
   recorded five clean 11 to 12 second turns. Her scores against her
   own voice were 0.49 to 0.61, against 0.36 to 0.42 for the other
   person. One turn went unnamed and one scraped in at 0.502. A score
   judged against how this household's voices really spread named all
   four it could score, with room to spare.
4. **Three routes, three sets of rules.** Room on, room off and solo
   each run their own pass. The same audio can read as a guest in one
   and as you in another. The audit found 12 live defects of this kind,
   such as doubtful turns in room-off and solo reading as you.
5. **Guesses feed the voice banks.** A turn named by a guess can be
   saved as a voice clip, so a mistake grows. Static named as an absent
   guest, and an AI seated as a person, both started this way.
6. **The size of it.** Voice identity is about 10,500 lines of backend
   and 3,900 of frontend. It touched 108 of the repo's 296 commits
   since 8 August. It has 12 settings, about 75 fixed numbers, 11
   reasons for not naming a turn, and 13 states a voice bank can be in.

## How the new approach works

The pipeline, in order: capture, follow each voice, fingerprint the
clean speech, name each voice, ask about new ones, learn, and tidy up
when the session ends. Each step is below as what actually happens.

### Capture

1. The mic opens with one setting in every mode: echo cancellation on,
   noise suppression and automatic gain off. Banks learnt in one mode
   then match speech heard in another.
2. The browser resamples to 16 kHz properly, with a low-pass filter.
   Today it drops samples, which folds high sounds into the speech band.
3. Turn taking stays as it is: when a turn starts, when it ends, the
   pre-roll, and the cuts on long turns.
4. The relay keeps the session's audio in memory as it arrives, as it
   does today, and passes every chunk inside a turn to the voice
   tracker. If the live connection drops mid-turn, the backup upload's
   copy of the audio goes to the tracker instead, so every turn is
   checked whichever transcript wins.

### Follow each voice through the session

A voice session runs from turning voice on in a chat to turning it off,
or 10 minutes with no speech. The tracker is NVIDIA's
Nemotron-3-Diarization, run on this Mac by the loopback diariser
service.

1. When voice turns on, the relay opens a tracking session, using the
   tracker's 1.04 second setting.
2. Each audio chunk from inside a turn goes to the tracker as it
   arrives. The silence between turns isn't sent.
3. For each hundredth of a second, the tracker says which of up to 8
   voices is speaking, and whether two overlap. It keeps its own memory
   of each voice, so a voice keeps its number for the whole session.
   Each of those voices is a session voice.
4. When a turn ends, the relay sends the tracker 1.1 seconds of silence.
   That makes it finish labelling the turn's last second, which it
   would otherwise hold back until the next turn.
5. Crossband turns the tracker's frames into spans: which session
   voice, start, end, and whether anyone else spoke over it. It reads
   the frames directly, because the tracker's own list of segments
   counts pauses as speech.
6. Every clean span's fingerprint is checked against its session
   voice's fingerprint. A span that plainly belongs to another session
   voice is moved there. This catches the tracker splitting one person
   whose voice changes, or mixing two up.
7. When the session ends, the session voices are dropped.

Why the tracker keeps the memory, from the spike on this Mac:

| Test | Tracker's memory | Crossband's memory, at its best |
|---|---|---|
| 15 recorded meetings, 17 to 50 minutes each | 0.9% of speech given to the wrong voice, no voice ever swapped | 4.5% wrong, 7 swaps |
| Someone back after 20 minutes' silence | Kept their old voice in all three made-up hours | Found it, but slowly in one |
| A TV talking for 5 minutes | Took a voice of its own, never mixed into a person | Similar |
| Memory used | About 180 MB | About 400 MB |

The same build matched NVIDIA's published accuracy on the full AMI
meeting test: 9.5% error at the 1.04 second setting, against NVIDIA's
9.48%.

### Fingerprint the clean speech

1. For each span of 0.8 seconds or more where one voice speaks alone,
   crossband computes two fingerprints: one with TitaNet-Small, the
   model it uses today, and one with ERes2Net. Both run through
   sherpa-onnx, and their scores are averaged in the next step.
2. Each session voice keeps its fingerprints and a total of clean
   seconds. Its pooled fingerprint is the average, weighted by length.
3. Speech with two voices at once never goes into a fingerprint.

A session voice builds up evidence over many turns. A 1.5 second reply
is named from everything that voice has said so far, not from 1.5
seconds.

### Name each voice

After every turn, crossband works out who each session voice is.

1. **Score.** Each session voice with at least 1 second of clean speech
   is scored against every known person. The score is the average of
   its three best matches among that person's kept clips. All kept
   clips count, from every day and room.
2. **Turn it into a probability.** A calibration fitted on this
   household turns the score, and how many seconds of speech it rests
   on, into the chance that the voice is that person. It's fitted by
   cutting every kept clip into 1, 2, 4 and 8 second pieces and scoring
   each piece against every bank, with that whole day's clips left out
   of its own bank. A short clip cut from a longer one leaves with it.
   Each person's bank is also hidden in turn, so the calibration learns
   what a voice it doesn't know looks like. It's refitted whenever a
   bank changes, in under a second.
3. **Start fair.** At the start of a session, every known person and
   "someone new" are equally likely. The evidence moves the chances
   from there.
4. **One person, one voice.** Session voices and people are matched one
   to one, so two voices can't both be Sam. "Nobody we know" is always
   an option for each voice. If two session voices both look like the
   same person and never spoke at once, they're joined, because the
   tracker split one person in two.
5. **Decide.** Each session voice ends up in one of four states.

| State | When | What the chat shows |
|---|---|---|
| Listening | Under 1.5 seconds of clean speech, or no one clears the bar yet | the turn, with a quiet "…" chip |
| Named | One person at 0.9 or more after the one-to-one match | the name, with a tick |
| Learning | Named by elimination or a single introduction, bank not yet confirmed | the name, marked learning |
| New voice | 4 seconds or more of clean speech, and under 0.1 for everyone known | "New voice", and the question |

6. **Keep it.** A named voice keeps its name as more turns arrive. If
   the chance for that name falls under 0.5 with 10 seconds or more
   behind it, the name comes off and the turn is flagged for you.
7. **Fix the past.** When a voice gets its name, its earlier turns in
   the session take the name too, and the chat updates. The AIs see the
   names from their next reply.

The 0.9, 0.1 and 0.5 are starting values. The shadow stage sets them
from your own sessions.

What the spike measured on the household's kept clips, with a whole day
left out each time. Today's rule, given the same pooled speech, names
the right person as often, but it also names people it has never
learnt. The new scorer calls them new instead.

| Speech heard from one voice | Named right, new scorer | Wrong names | Unknown people named |
|---|---|---|---|
| One turn, about 2 seconds | 55% | 0 | 0 |
| Two turns, about 4 seconds | 93% | 0 | 0 |
| Five turns, about 11 seconds | 100% | 0 | 0 |

Comparing with a crowd of public voices was tried and dropped. It made
things worse, because the public recordings were made on different
microphones from this household's.

### Label each message

1. A message is labelled with the session voices heard in its time
   span. The main speaker is the one with the most time alone. Another
   voice is listed when it spoke for 1 second or more.
2. A long turn is labelled from all of it, not from its last piece.
3. When two voices overlap, each word of the transcript goes to the
   voice speaking at that word's time. ElevenLabs Scribe Realtime sends
   word times when asked, counted from the start of the connection.
   Asking makes it send each final transcript twice, plain and then
   with times, so the relay must use the second and drop the first. The
   split happens on the Mac, so no voice clips go to the cloud to do it.
4. A named voice's label is ready about 50 ms after the turn ends, well
   before the message is saved.

### Ask about new voices

1. A session voice becomes "new" once it has 4 seconds of clean speech
   and matches nobody known.
2. The app asks once for that voice: "Someone new is talking. Who's
   this?" The question holds that voice's clean audio in memory.
3. You answer by saying it ("that's Dave"), the person introducing
   themselves, or picking a name from the menu. The name goes on the
   voice, every turn it spoke is relabelled, and its best clean audio
   becomes that person's first clips, marked as introduced.
4. You can also answer "that's the TV" or "that's the radio". That
   voice is then ignored for the rest of the session, and never asked
   about again.
5. If nobody answers before the session ends, the audio is dropped.
6. In solo mode the app never asks. A new voice shows as "someone
   else" and nothing is learnt.

### Learn

1. A voice clip is saved to a bank only from a session voice that a
   person named, by introduction, correction or confirming, or that was
   named at 0.99 or more with 8 seconds or more of clean speech.
2. Nothing else is ever saved: overlapped speech, a voice still
   listening, a new voice nobody has named, or a voice named by
   elimination alone.
3. Naming by elimination stays for the first meeting. When one person
   in the room has no clips yet and exactly one voice is new, the app
   names it as them, marked learning. Its audio is saved only once you
   or they confirm it.
4. The kept clips rotate as they do today: 10 long and 5 short per
   person, spread across days, with introduced and corrected clips kept
   first.
5. A bank that nobody has vouched for still waits for your ear before
   it can name anyone, in every path.

### Tidy up when the session ends

1. Crossband runs the naming once more, over every fingerprint the
   session collected. A turn whose name changes is relabelled, and one
   line is logged with ids and names only.
2. Every audio buffer from the session is dropped. Only clips saved by
   the rules for learning stay.

### What the AIs and memory are told

The AIs read the same kinds of turn headings as today, now from one
rule in every mode.

| State | The heading the AIs read |
|---|---|
| Named, and it's you | your name, voice confirmed |
| Named, someone else | their name, in the room |
| Learning | their name, learning this voice |
| Listening | identity pending |
| New voice, or someone else in solo | unidentified speaker, a new voice |
| Typed turn | you, as today |

Once your own voice is ready, a spoken turn that isn't named is never
shown to the AIs as you.

Memory gets the probability as the speaker's confidence. Membro links a
fact to a person by itself at 0.8 or more, which now means 80% sure.
Today it's compared with a raw score that rarely passes 0.8 even for the
right person. Introductions and corrections also send their method,
introduced or owner correction, which today they don't, so a turn you
named yourself counts as the strongest evidence.

### When a voice is ready

A person's voice is ready when 2 second pieces of their own speech are
named as them at least 95 times in 100, and never as anyone else, with
that day's clips left out, over at least 20 pieces. Seconds count
speech only, not pauses. The Voices page shows that instead of a count
of seconds. A voice that isn't ready can still be named once a session
voice has enough evidence.

Most banks aren't ready today. In the spike only one of six people
passed, and two had under 5 seconds of speech saved. So the Voices page
gains a way to record someone on purpose:

1. You pick the person and press record, with them at the mic.
2. The app shows a short passage to read aloud and records about 30
   seconds, with the same mic setting as a voice chat.
3. The speech check trims it, and it's cut into clips marked as
   introduced, so they're vouched for and kept first.
4. The page shows the readiness test's result straight away. A person
   who isn't ready yet is asked to record once more, ideally on
   another day or in another room.

## Modes

- Every spoken turn is checked whenever voice is on, in every mode.
  The three routes become one pass.
- Room mode becomes something you see, not a switch on how voices are
  checked. It still turns on by itself when a second person is named or
  a new voice appears.
- Solo names people it knows, never asks, never seats anyone and never
  learns, as decided on 25 September. Saying "solo mode" still turns
  the room off at once.

## What stays, what goes

Stays, because each one fixed a real failure:

- An AI's name can never be a person in the room, spelt-by-ear
  variants included. The same check now covers spoken corrections and
  tapping a turn to correct it.
- Names are yours: the name you set is never changed by the app. Name
  variants join or ask, and relationship words are never names.
- Your own name, and ways of mishearing it, never create a second you.
- The speech check, so static and noise never name, seat or teach.
- Forget, the correction record that stops a deleted clip coming back,
  and the sync of people and clips with membro.
- Content-free logs, owner-only files, and audio kept in memory only.
- The label is on the message before the AIs read it.
- The one model call per turn that hears introductions, corrections and
  mode commands.
- The mismatch check, as a doubt flag that never changes a label.

Goes:

- The three routes, their decision tables and the live mirrors.
- The fixed bars: the 0.5 naming bar, the margin, the extra bar while
  someone is unlearnt, and the wider bar for similar pairs.
- The average of each person's three best clips as their fingerprint.
- The sliding-window check for two voices in a turn.
- The early check before a turn ends.
- The cloud split for crosstalk. It's the one path that sends people's
  voice clips to ElevenLabs.
- The introduction stash, and the separate check for backup transcripts.
- The hygiene check's own fingerprint. Clips are checked with the same
  scorer that names people.

## Models, and why these

| Job | Choice | Why |
|---|---|---|
| Follow voices | NVIDIA Nemotron-3-Diarization, through NeMo-Speech.cpp on Metal | Up to 8 voices, open licence that allows commercial use (OpenMDW 1.1). The best published error rates of any open diariser that runs live, matched on this Mac. About 180 MB, and about half a second of work per turn, spread through the turn. |
| Fingerprints | TitaNet-Small and ERes2Net, scores averaged | The pair named the most short pieces with no wrong names. Both are Apache 2.0. ERes2Net takes about three times as long as TitaNet-Small, still well under a second a turn. |
| Instead of ERes2Net | CAM++, with a patched model file | Nearly as good at a third of ERes2Net's cost. The runtime sherpa-onnx ships mishandles its last block of audio, and one setting in the file fixes it. |
| Speech to text | ElevenLabs Scribe v2 Realtime, kept | It adds word times, for splitting crosstalk. |

Considered and not chosen:

| Option | Why not |
|---|---|
| TitaNet-Large | As good as the pair on its own, but a 100 MB download under a licence that needs credit, for no gain over ERes2Net. |
| NVIDIA Streaming Sortformer | Nemotron's predecessor. 4 voices at most, and worse on every published test. |
| pyannote Community-1, diart, DiariZen | Community-1 and DiariZen work on a whole recording, not live, and DiariZen's weights are non-commercial. diart is live but makes two to three times the errors. |
| FluidAudio (Swift, Apache 2.0) | Runs Nemotron and others on Apple hardware. Not needed, since NeMo-Speech.cpp matched NVIDIA's accuracy here. |
| Picovoice Eagle | Its licence key checks in with Picovoice's servers, and it's sold to businesses only. |
| Speechmatics, pyannoteAI | Cloud services, so voices would leave the Mac. |
| ElevenLabs Scribe batch speaker library | Batch only, and no documented way to enrol a speaker. |

The pipeline meets the rest of the app in two places: "who spoke when"
and "who is this voice". Both are local today. Either can be swapped
for a service later without changing anything else.

## Measuring it

Two kinds of truth, both private to this Mac and never committed:

- **Made-up sessions from real voices.** 20 to 60 minute sessions
  assembled from the household's kept clips, with crosstalk, short
  replies and long monologues. A stranger is a household member whose
  bank is hidden for that run, because public recordings are too easy
  to tell apart. The truth is known exactly. This is the heart of the
  voice rig issue.
- **Real sessions in shadow.** Your own evenings, with your corrections
  and confirmations as the truth.

| Measure | Target |
|---|---|
| Wrong names | 1 per 100 spoken turns at most |
| Unsure turns | 5 per 100 at most, after each person's first two turns |
| First name | A known person named by the end of their first turn of 1.5 seconds or more, 9 times in 10 |
| Label speed | On the message when it's saved, and 95 in 100 within 300 ms of the end of speech |
| New voices | One question per new person per session |
| Staying on track | No voice swapped for another in a 60 minute made-up session |
| Cost | No new cloud calls. Memory under 500 MB more than today. |

## Rollout

Every stage is its own pull request and can be reverted on its own.

1. **Spike, outside the app. Done on 26 September.** The tracker
   matched NVIDIA's accuracy and keeps its own memory. The pooled,
   calibrated naming held with no wrong names. Scribe sends word
   times. The findings are folded into the sections here.
2. **Shadow.** The new pipeline runs beside today's on every spoken
   turn in every mode, and changes nothing. It writes content-free
   rows and shows a comparison. It replaces today's shadow test, and
   fixes a flaw in it: a turn saved to a bank was then scored against
   itself, which inflated three scores on 26 September. Recording a
   voice on purpose ships in this stage too, so banks are ready before
   the gate is judged. Gate: five or more sessions with two or more
   people, and five solo ones, meeting the targets and no worse than
   today on any session.
3. **Switch.** A setting picks the new path, and it's the default.
   Today's path is one setting away for two weeks of use.
4. **Delete.** Today's path goes, with the cloud crosstalk split.
   VOICE_ID.md is rewritten for the new pipeline, and the stored trust
   scores are recomputed in the new units.

Nothing migrates by hand. Fingerprints are never stored, so every bank
is rebuilt from its clips by the new scorer at the first start.

### Where it stands, 26 September

Part of stage 3 went live early, as a narrower step than the plan's
switch. The owner asked to cut over during that evening's game, after
the session naming named 33 of 35 turns where the matcher named 4.

Live on this install:

- The diariser's tracking sessions, and the session naming running
  beside the matcher on every spoken turn in an armed room.
- The relay feeds the tracker as you talk. When the matcher can't name
  a turn, the session's name goes on it before the models read it,
  about a tenth of a second after you stop.
- A voice named later fills in its earlier unnamed turns.
- The calibrated scorer, used so far only for the readiness test on
  the Voices page.

Not built yet:

- Session names come from TitaNet-Small against every kept clip, on
  the matcher's own bar. The calibrated two-model scorer doesn't name
  turns yet.
- Recording a voice on purpose, asking who a new voice is, saving clips
  from a session, and the one mic setting.
- The session name only fills gaps. It never overrules the matcher,
  and today's path hasn't been removed.

## Risks

| Risk | What happens | What limits it |
|---|---|---|
| The tracker loses track in a long session | Two voices swap | None in 15 recorded meetings. The fingerprint check on every span, and the end-of-session naming pass. |
| A voice sounds unlike its bank (a cold, a whisper, a new mic) | Named late, or left listening | Probabilities fall, so the app waits instead of guessing. Confirming a turn teaches it. |
| Two similar voices, like siblings | Both near the bar | The calibration sees them close, one to one stops both getting one name, and you confirm |
| The AIs' own playback reaches the mic | A voice made of AI speech | A voice heard mostly while the AIs are talking is never named or saved |
| A TV or radio talks in the background | It takes one of the 8 voices, and gets asked about | It never mixed into a person in the spike. You say "that's the TV" once per session. |
| Thin voice banks | People stay listening for longer | Recording a voice on purpose, and the readiness test on the Voices page |
| Memory in a long session | The session's audio grows | Keep the last 10 minutes of audio. Older speech lives on only as fingerprints. |
| A new setting to tune | Bars set wrong for a house | Bars start from this house's own calibration and move in shadow first |

## Decisions

Made by the owner on 26 September:

1. The cloud crosstalk split goes, with no fallback. Overlap is split
   on the Mac from Scribe's word times.
2. One mic setting in every mode, solo included.
3. The app asks about a new voice after 4 seconds of their speech.


## Detail

What changes where, by the end of stage 4:

- `backend/diarize.py`: the routes, the decision tables, the mirrors,
  the speculative check and the cloud split go. What's left becomes the
  session tracker client and the label writer.
- `backend/voiceid.py`: `classify_utterance` and its bars go. New:
  scoring against every clip with two models, calibration and the one
  to one match.
- `backend/anchors.py`: sufficiency becomes readiness, and trust moves
  to the new units. Rotation, vouching, protection and the correction
  record stay.
- `backend/voice_shadow.py`: becomes stage 2's shadow of the whole new
  pipeline.
- `backend/routers/voice.py`: opens and closes the tracking session,
  feeds it from both transcript paths, and asks Scribe for word times.
- `frontend/src/voice.js`, `captureProfile.js`, `identityClip.js`: one
  mic setting and proper resampling. The backup copy of the audio stays,
  as the tracker's fallback feed.
- `backend/providers.py`, `memory_client.py`: one heading rule, and the
  probability as confidence. Membro's side needs no change, and the
  meaning of confidence is noted in the memory contract.
- workbench `diarserve/diarserve.py`: gains session routes to open a
  tracking session, push audio, end a turn with the silence, and close.
- The Voices page and `backend/routers/room.py`: recording a voice on
  purpose, and the readiness result.

Sources:

- [Nemotron-3-Diarization model card](https://huggingface.co/nvidia/Nemotron-3-Diarization),
  with error rates of 12.73% on DIHARD III and 9.25% on AMI, its
  [Mac support in NeMo-Speech.cpp](https://github.com/NVIDIA/NeMo-Speech.cpp/pull/50),
  the [FluidAudio port](https://github.com/FluidInference/FluidAudio/pull/883),
  and [OpenMDW 1.1](https://openmdw.ai/license/1-1/).
- [Streaming Sortformer v2.1](https://huggingface.co/nvidia/diar_streaming_sortformer_4spk-v2.1),
  [pyannote Community-1](https://huggingface.co/pyannote/speaker-diarization-community-1),
  [diart](https://github.com/juanmc2005/diart) and
  [DiariZen](https://github.com/BUTSpeechFIT/DiariZen).
- Short clips four times harder: [DAME, ICASSP 2026](https://arxiv.org/abs/2601.13999).
- Naming voices rather than turns, and leaning towards too many voices:
  [TST, 2026](https://arxiv.org/html/2606.14091).
- One to one matching: [Wang et al., EDM 2024](https://educationaldatamining.org/edm2024/proceedings/2024.EDM-short-papers.33/index.html),
  and pyannoteAI's [voiceprint identification](https://docs.pyannote.ai/tutorials/identification-with-voiceprints).
- Scores into probabilities: [Brümmer](https://arxiv.org/abs/1307.7981).
- Household banks and when to add a clip: [Sholokhov et al. 2022](https://arxiv.org/abs/2205.00288).
- The AMI meeting references the spike scored against, the same ones
  behind NVIDIA's numbers: [diar-forced-alignment](https://github.com/nttcslab-sp/diar-forced-alignment).
- Scribe Realtime word times and its lack of diarisation:
  [the realtime API](https://elevenlabs.io/docs/api-reference/speech-to-text/v-1-speech-to-text-realtime).
