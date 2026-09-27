# Voice rig

In a voice chat the app works out who's speaking and puts a name on
each turn. The voice rig tests that without anyone in the room. It
makes up short conversations between made-up people, speaks each line
in a synthetic voice, and knows who spoke every second. It plays each
conversation into a second copy of the app and scores the name the app
wrote on every turn.

It measures and nothing more. It never touches your running app, your
chats, your people or your memory, and nothing here changes how the app
names a voice. Run it when you change how voices are named, to see
whether a change named more turns right, left more unnamed, or put a
wrong name on anyone.

## What a run does

1. It reads the scripts. The committed ones are in `scripts/`, and you
   can add your own.
2. It speaks each line in that person's ElevenLabs voice, once. Every
   line is kept in the rig's cache, so a rerun costs nothing.
3. It mixes each turn on your computer. It trims the silence around a
   line and starts a second line part of the way through the first when
   the script asks for crosstalk. It turns a voice down when the script
   says it's further away and lays cafe or road noise under the whole
   turn. The truth is written down as it goes.
4. It starts a second crossband on port 8920, from a copy of this
   checkout's code, with a data folder of its own.
5. It records each person reading a short passage through the Voices
   page's recording route. Then it waits until the voice models are
   loaded and the calibration covers every recording.
6. It plays each conversation into the voice relay the way the browser
   does, in real time, a new chat for each. Each transcript goes back to
   the app the way the browser sends it.
7. It reads the name the app wrote on each turn and whether it heard
   the introductions and room commands in the script. It closes the
   diariser's tracking session when each conversation ends.
8. It stops the second app, deletes its data folder and writes the
   report.

The chats have no seats, so no model answers anything and nothing is
spent on replies.

## The made-up people

Everyone who speaks is on the synthetic roster, each in a stock
ElevenLabs voice.

| Person | Voice | Before the run |
|---|---|---|
| Alex | Charlie, an Australian man | recorded, and the app's owner |
| Sam | Jessica, an American woman | recorded |
| Dave | Chris, an American man | recorded |
| Mateo | Will, an American man | not recorded, so the app meets him new |

Dave and Mateo were picked to sound close, so every run holds one pair
that's hard to tell apart. `--enrol` changes who's recorded first.

## What stays apart from your app

The app reads its settings from the checkout it runs from. A copy
started in your own checkout would pick up your memory token and every
connected app. The rig starts its copy somewhere else.

- It copies the backend code and `config.json` into a run folder under
  the rig's cache. There's no `config.local.json` and no `.env` there.
- It builds the second app's environment from nothing. It gets the
  path and home variables, its own settings, and two keys. The
  ElevenLabs key transcribes, and the Anthropic key pays for the one
  small model call that reads each turn.
- Its data folder is new for every run, and it refuses to start on the
  app's own.
- Memory points at a closed port and there's no memory token, so
  nothing reaches membro.
- It never uses one of the fleet's ports.

It reads one setting from this checkout's `config.local.json`, the
diariser's address in `diarize_shadow_url`. The diariser is shared with
your app, and it holds only a few tracking sessions at once. The rig
closes its own as each conversation ends.

## Run it

```sh
# check the rig itself, with no keys and no cost:
.venv/bin/python -m eval_voice --mock

# a real run, with the keys from the app's own file:
.venv/bin/python -m eval_voice --env ~/dev/crossband/.env

# some scripts only, keeping the second app's data folder to look at:
.venv/bin/python -m eval_voice --scripts two-voices,introductions --keep

# the mixes and their truth as files, with no app:
.venv/bin/python -m eval_voice --mix-only
```

`--diariser` points at another diariser, and `--no-diariser` names
each turn on its own. `--no-calibrated` leaves the calibrated scorer
off. `--speed 2` plays twice as fast. `--out` and `--json-out` write
the report to files.

The Analysis page in the app runs the real run as a background job and
keeps its reports on the Mac. It won't start the rig while a voice chat
is live, since the two share the diariser.
[docs/ANALYSIS.md](../docs/ANALYSIS.md) says how.

The machinery is pinned by `tests/test_eval_voice.py`, with no keys:

```sh
.venv/bin/python -m pytest tests/test_eval_voice.py -q
```

## What it costs

Rendering a line costs ElevenLabs characters the first time only. The
four committed scripts and the three recordings come to about 3,100
characters, around 35 cents on the app's rate card. After that, every
rerun of the same lines reads them from the cache.

Every run streams its audio to the app, which transcribes it on
ElevenLabs. The committed scripts are about two minutes of audio,
around a cent. The app's one model call per turn is about a fifth of a
cent. The report ends with each of these, and the second app's own
ledger.

## Reading the report

Each turn gets one verdict.

- Named right means the voice heard most on its own was named, and
  every name on the turn belongs to someone who spoke in it.
- Unnamed means no wrong name, and the main voice wasn't named. The
  report says whether the app gave a reason, "still listening" or "a
  new voice".
- Wrong name means a name on the turn belongs to someone who didn't
  speak in it. These are listed one by one, because that number has to
  stay at zero.
- No label means the app wrote nothing about the turn.

A name the transcriber spelt its own way, like "Matteo", still counts
as Mateo.

The targets table holds the run against the targets in
[docs/VOICE_ID_REDESIGN.md](../docs/VOICE_ID_REDESIGN.md#measuring-it).
It shows wrong names per 100 turns, and unnamed turns after each
person's first two. It shows whether each known person was named on
their first turn of 1.5 seconds or more, and whether the name was on
the message when it was saved.

The conditions table breaks the verdicts down by noise, a quiet voice,
crosstalk, a short turn and a voice the app wasn't given first. A
crosstalk turn is also judged on its split. That's whether it was marked
as two voices, whether both were named, and what share of the words the
app gave a person went to the person who said them. Introductions and
room commands are heard when the roster or the room changes within 10
seconds of the turn.

The per-turn table names who spoke, the conditions, what the app wrote
and how the app named the voice. It carries no words.
`--show-words` adds what the transcriber heard.

## Writing scripts

A script is one JSON file with an id, the people in it, an optional
noise bed and the turns. A turn is one line from one person. It can
carry `over` for a second person talking part of the way through, and
`gain_db` for a quieter voice. It can also carry the introduction or
room command the line holds. `script.py` has the whole shape, and the
validator refuses any name off the roster.

The utility model can write more, in the same shape:

```sh
.venv/bin/python -m eval_voice --generate 2 --with crosstalk,room
.venv/bin/python -m eval_voice --scripts-dir eval_voice/cache/scripts
```

Generated scripts are checked like the committed ones and kept in the
cache. Commit one only after you've read it.

## Testing another system

The scripts, the renderer, the mixer and the scorer know nothing about
crossband. The crossband adapter in `crossband.py` is one thin piece
that plays turns in and reads names back. Another speaker
identification system plugs in with its own adapter, the three methods
in `adapter.py`. `--mix-only` also writes each conversation as one WAV
file with who spoke when beside it, for a system that takes a file.

## What it can't tell you

A synthetic voice is cleaner and steadier than a person. The rig
catches regressions and mix-ups, and it can't set the thresholds for a
real room. Every recording is made on the same day, so the readiness
test on the Voices page can't pass for anyone. Naming still works,
because the calibration fits on one day's clips.

The app names every voice once more when a voice session ends, after
10 quiet minutes. A run stops the second app before then, so that last
naming pass never runs, and the names scored are the ones the turns
had while the conversation was going.

The rig plays straight into the voice relay. Playing a conversation out
of a speaker into the laptop's microphone isn't built yet, and that's
the only way to test a real room and its echo. Spoken name corrections
and thinking depth changes aren't scored yet either, because depth needs
a seat and the chats have none.
