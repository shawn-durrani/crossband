# Synthetic voice benchmark

A Models-page runner that puts identical scripted cases through the
seats you pick and compares stage timings side by side (#94). It answers
"how do these seats compare on the same tiny job". It does not predict a
live turn: real turns carry history, memory, tools and caches, so their
latency is their own. Every result file says so.

Open it from the Models page, under Benchmark.

## What it never does

The runner never opens the microphone and never plays audio on its own.
Generated clips are saved and offered as download links; listening is
your move. Where quality needs human judgement, the artefact is kept
instead of a score being invented. Fixtures and results carry no
conversation content and no key values; tests pin both.

## Dimensions

- Text reply: time to first visible word, total seconds, token count and
  the reply itself.
- Speech-to-text: a spoken fixture clip transcribed once per run.
- Text-to-speech: one fixed sentence through each seat's own voice.
- Full pipeline: listen, think, speak in sequence, timed per stage.

Each dimension is selectable on its own. A leg a seat cannot run is
reported as skipped with the reason, never as a silent gap or a fake
failure. Hosted and self-hosted seats run the same cases.

## Fixtures

The spoken fixture is one fixed sentence, generated once through a
configured voice and cached under data/benchmarks/fixtures/. Its
provenance sits beside it in spoken-prompt.json. Replace the clip with
your own recording to test against a real human voice. Keep the sentence
in the json so the transcript check still has a reference; without one,
the transcript is shown unjudged.

## Honest numbers

Calls are minimal and strictly sequential, one in flight at a time. A
big selection is therefore slow on purpose: overlap would measure
contention, not seats. Text calls carry each seat's own reasoning and
thinking settings, because those dominate first-word latency. OpenAI-
style seats try the Responses API first and fall back to chat
completions, the same adapter order a live turn uses.

## Results and retention

Each run writes results.json plus any audio under
data/benchmarks/runs/, updated after every unit, so an interrupted run
keeps what it measured. Every file is labelled synthetic and carries the
run timestamp, the cases, the fixture provenance and each seat's
configuration. Voice legs carry an estimated cost from the voice pricing
map. Runs persist locally until deleted from the panel, which removes
the audio too. Benchmark spend never lands on a chat, so the spend page
ignores it; the ElevenLabs quota reflects it.
