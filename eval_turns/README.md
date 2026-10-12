# Turn-taking rig

In a voice chat the app decides when you've finished talking, and what to
do when someone talks over a reply. The turn-taking rig times both. It
plays made-up moments into the real voice page and writes down what the
page did, and how fast.

It measures and nothing more. It never touches your running app, your
chats, your people or your memory. Run it before and after you change
anything about timing, like the pause that ends your turn, so you can
see whether the change helped.

## What it measures

Pauses. You say one sentence with a silence in the middle. The right
answer is one turn, sent once you've finished. The rig counts how often
the app took the pause as the end of your turn, and how long it took to
send the turn after your last word. A pause longer than the app's pause
setting is meant to end the turn, so those are counted apart from the
shorter ones.

Talking over a reply. A seat starts a reply, and two seconds in someone
talks over it. There are four kinds of moment:

- An interruption, like "Wait, stop." The reply should be cut.
- A "yeah" or a "mm-hm". The reply should carry on.
- A remark to someone else in the room. The reply should carry on.
- The TV. The reply should carry on.

For each moment the rig says whether the reply was paused, cut or let
go, and how long each took from the first word. It also says what the
word rules and the judge decided. A cut can come late. The reply is let
go, then the words arrive and cut it. The report counts those.

It also times the whole loop once per moment, from the end of your
question to the reply's first sound.

## How it works

1. It speaks each line in that person's ElevenLabs voice once and keeps
   it in the voice rig's cache. It sets the words to the level of
   someone talking at a laptop, and builds each moment's audio with its
   pause and any noise.
2. It starts a second crossband on port 8921 with the voice rig's
   machinery. It's a copy of this checkout's code and built page, with
   a data folder of its own and memory switched off.
3. It puts a stand-in seat in Claude's place. That's a tiny server on
   your Mac that answers like a local model. It gives the same long
   reply to the opening question and "Sure." to anything else, so every
   moment talks over the same words.
4. It opens the voice page in a headless Chromium. It hands the page a
   made-up microphone, and clicks talk as you would.
5. It plays each moment into the microphone and reads the page's own
   debug lines, which carry the page's clock and never any words. Every
   decision lines up with the audio to a few milliseconds.
6. It stops the second app and deletes its data folder.

Everything that decides a turn runs in the page, so the rig drives the
page itself. That covers the voice detector, the pause, the word rules
and the call to the judge.

## Run it

```sh
# check the rig itself, with no keys and no cost:
.venv/bin/python -m eval_turns --mock

# a real run, with the keys from the app's own file:
.venv/bin/python -m eval_turns --env ~/dev/crossband/.env

# only the interruptions, three times each:
.venv/bin/python -m eval_turns --moments interrupt --repeat 3
```

`--moments` takes moment ids, a kind (`pause` or `over`) or a group
(`interrupt`, `backchannel`, `aside` or `background`). `--pause` sets the
app's pause, as the settings drawer would. `--level` changes how loud
the words reach the page. `--over-after` changes how far into a reply
each moment starts. `--keep` keeps the second app's data folder and the
page's record of the run.

It needs the page built in `frontend/dist`, which a deploy does, and
Chromium for Playwright. The app's web viewing uses it too, so it's
usually there. If not, run `.venv/bin/playwright install chromium` once.

The judge is on when there's a `TYPESAFE_API_KEY`, as it is in the app.
Without one the word rules decide alone, and the report says so.

You can also run it from the Analysis page.

## What it costs

- The stand-in seat's replies are spoken by the app on ElevenLabs. That's
  about 2,000 characters a run, around 22 cents.
- The moments' own lines cost about 4 cents the first time. After that
  they come from the cache.
- Transcribing the moments costs about a cent.
- The app's one model call per turn costs about 5 cents.
- Each call to the judge is a fraction of a cent.

A run takes about six minutes.

## What it can't tell you

A synthetic voice through a made-up microphone is cleaner than a person
in a kitchen. There's no echo, because the reply never reaches the
microphone, so it can't test the echo canceller. The stand-in seat
answers in 0.3 seconds, so the time to the first sound is the app's own
part of the wait and leaves out a real model.

A short phrase sits close to the detector's limit. It needs half a
second of steady speech before it pauses a reply, and "Wait, stop." is
about that long, so it can land either side. Use `--repeat` to see how
steady a borderline moment is.
