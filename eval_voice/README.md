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
   says it's further away and lays noise under the whole turn. The
   truth is written down as it goes.
4. It starts a second crossband on port 8920, from a copy of this
   checkout's code, with a data folder of its own. Its voice sessions
   end after 45 quiet seconds, where yours wait 10 minutes.
5. It records each person reading a short passage through the Voices
   page's recording route. Then it waits until the voice models are
   loaded and the calibration covers every recording.
6. It plays each conversation into the voice relay the way the browser
   does, in real time, a new chat for each. Each transcript goes back to
   the app the way the browser sends it.
7. It reads the name the app wrote on each turn and whether it heard
   the introductions, room commands and answers in the script.
8. It waits for the conversation's voice session to go quiet and for
   the app's last naming pass, then reads every name again.
9. It forgets anyone the app met in that conversation, so the next one
   starts knowing only the people recorded before the run.
10. It stops the second app, deletes its data folder and writes the
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

Some scripts have a TV on, in Daniel's voice, a British newsreader. The
TV isn't a person, so it's never recorded, introduced or named.

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
- Its voice sessions end after 45 quiet seconds, through its own
  `voice_session_idle_s`. Yours keep the 10 minutes.

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
off. `--speed 2` plays twice as fast. `--session-idle 0` leaves the
second app's voice sessions at 10 minutes, and the last naming pass
goes unscored. `--out` and `--json-out` write the report to files.

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
eight committed scripts and the three recordings come to about 4,300
characters, around 47 cents on the app's rate card. After that, every
rerun of the same lines reads them from the cache. The two recorded
rooms are a minute of sound effects, about 12 cents, also once.

Every run streams its audio to the app, which transcribes it on
ElevenLabs. The committed scripts are about four minutes of audio,
around 3 cents. The app's one model call per turn comes to about 13
cents a run. The report ends with each of these, and the second app's
own ledger.

A run takes about 12 minutes. Most of it is the conversations played
in real time, and each one then waits 45 seconds for its voice session
to end.

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

A turn the TV spoke is named right when it carries no name at all,
whatever the reason. Any name on it is wrong.

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

A spelling is heard when the person ends up shown under the spelt name,
and marked as a name you set.

### After the last naming pass

The app names every voice once more when a voice session goes quiet.
It adds or changes names and never takes one off. The rig waits for
that pass after each conversation and reads every turn again. The
section after the targets sets each verdict's count when the
conversation ended beside its count after the pass, and lists every
turn the pass changed.

### Who's this

When a voice nobody knows has talked for 4 seconds, the app asks who it
is. Some scripts answer out loud, with a name or "that's the TV". This
section judges each answer on the names as they finally stand.

- The ask pointed at a turn the new voice spoke.
- That turn took the answer. For a name it carries the name, and for
  the TV it carries no name and the TV as its reason.
- The new voice's earlier turns were relabelled the same way, and its
  later turns kept it.
- A named voice had a clip saved. The TV had no person made for it.
- No ask was still open when the conversation ended.

### Every turn

The per-turn table names who spoke, the conditions, what the app wrote
and how the app named the voice. It carries no words.
`--show-words` adds what the transcriber heard.

## Writing scripts

A script is one JSON file with an id, the people in it, an optional
noise bed and the turns. A turn is one line from one person, or from
the TV. It can carry `over` for a second person talking part of the way
through, and `gain_db` for a quieter voice. It can also carry what the
line says for the app to act on.

- `introduce` names someone on the roster who's joining.
- `room` switches the room on or off.
- `answer` answers the app's ask about a new voice, with a name on the
  roster or `TV`.
- `correct` spells out a name on the roster.

`script.py` has the whole shape, and the validator refuses any name off
the roster.

### Noise

A script's noise is one of four kinds, laid under every turn at the
script's signal-to-noise ratio.

- `cafe` is made up from random numbers, a hiss with the odd clink of a
  cup.
- `road` is made up the same way, a low rumble that swells as traffic
  passes.
- `chatter` is a recorded cafe full of people talking, with no words
  clear. It's the hard case, because the noise is voices too.
- `kitchen` is a recorded kitchen, with a fan, a tap and dishes going.

The recorded rooms are made once by ElevenLabs' sound effects model
from a short description in `beds.py`, and kept in the cache like the
rendered lines. Each is 30 seconds that loops, and each turn takes its
own stretch of it, so a script always mixes the same way. Making them
needs the sound effects permission on the ElevenLabs key. Without it a
script on a recorded room is left out of the run, and the report says
why. The `cafe-chatter` and `kitchen` scripts are two of the others
again, over a recorded room, so you can set the two kinds of noise side
by side.

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

The rig ends each voice session after 45 quiet seconds. Your app waits
10 minutes, and a real session holds far more turns, so the last naming
pass has more to work with at home.

The rig plays straight into the voice relay. Playing a conversation out
of a speaker into the laptop's microphone isn't built yet, and that's
the only way to test a real room and its echo. Thinking depth changes
aren't scored either, because depth needs a seat and the chats have
none.
