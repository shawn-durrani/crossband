# Comparing seats on the same small job

The benchmark puts the same few scripted cases through the seats you
pick and lines up the timings side by side. It answers one question:
how do these seats compare on the same tiny job. It doesn't tell you
how long a live turn will take, because a real turn carries the
transcript, memory, tools and caches, and its timing is its own. Every
result file says so.

Open it from the Models page, under Benchmark. Pick the seats and the
legs you want, and start the run.

## What it measures

- Text reply: the time to the first visible word, the total seconds,
  the token count, and the reply itself.
- Speech to text: one spoken clip, transcribed once per run.
- Text to speech: one fixed sentence, spoken in each seat's own voice.
- The full pipeline: listen, think and speak in sequence, timed per
  stage.

On an Eleven v3 model the clip uses your v3 stability and accent tag,
the same as a live reply, and each result file records both. The clip
goes to ElevenLabs in one piece, so sentence chunks don't change it.
[Keeping a v3 voice steady](CONFIG.md#keeping-a-v3-voice-steady) has
the settings.

Each leg is selectable on its own. A leg a seat can't run is marked
skipped with the reason, never left blank and never shown as a
failure. Hosted and self-hosted seats run the same cases.

## What it never does

The benchmark never opens the microphone and never plays audio on its
own. The clips it generates are saved and offered as download links,
and listening is your move. Where a result needs a human ear, the clip
is kept and no score is invented. The fixture files and the result
files hold no conversation content and no key values, and the tests
check both.

## The spoken clip

The listening legs use one fixed sentence, generated once through the
voice you pick and cached under `data/benchmarks/fixtures/`. The file
beside it, `spoken-prompt.json`, records how the clip was made. To
test against a real human voice, replace the clip with your own
recording. Keep the sentence in the json, so the transcript can be
checked against it. Without a reference sentence the transcript is
shown and left unjudged.

## Why the numbers are slow and fair

Calls run one at a time, with never more than one in flight. A big
selection is slow for that reason: overlapping calls would measure
contention between them, not the seats. Each text call carries the
seat's own reasoning and thinking settings, because those decide most
of the wait before the first word. A seat on an OpenAI-style API tries
the Responses API first and falls back to chat completions, the same
order a live turn uses. A call that hasn't answered after three
minutes fails, so a wedged local model can't stall the run.

## Where the results go

Each run writes `results.json`, plus any audio, under
`data/benchmarks/runs/`. The file is updated after every unit, so an
interrupted run keeps what it measured. Every result file is labelled
synthetic and carries the run's timestamp, the cases, how the clip was
made, and each seat's settings. The voice legs carry an estimated cost
from the voice pricing map. A pipeline reply longer than 400
characters is cut before it's spoken, and the result says so.

Runs stay on disk until you delete them from the panel, which removes
the audio too. Benchmark spend never lands on a chat, so the spend
page ignores it. Your ElevenLabs quota still counts it.
