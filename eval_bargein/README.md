# Interruption judge trial

## What it measures

When you talk over a spoken reply, the app pauses it and decides from your
words whether to cut it off. The word rules settle the clear cases. A
"yeah" lets the reply carry on, and "wait" or a seat's name cuts it. A
remark that starts with someone's name is for them. What's left is the
unclear middle. That's a few words of your own that might be for the
AIs, or might be for someone else in the room.

This trial measures the judges that could decide that middle. They all
answer the same 136 made-up moments. Each moment says what the AI was
saying, who spoke over it, and what they said. Someone can be you,
someone else in the room, a voice the app doesn't know, or the TV. Each
moment carries the right answer, one of three:

- interrupt: said to the AIs, so the reply should stop
- aside: said to someone else, or to nobody, so the reply carries on
- listening: only a "yeah" or a "wow", so the reply carries on

A judge is scored on how often it's right, and how often it would cut a
reply that wasn't meant for the AIs. It's also scored on how often it
would miss a real interruption, and how long it took. The report has two
rows for each judge. One is the judge alone. The other, "rules, then X",
asks the judge only where the word rules cut on word count alone, which
is how a judge would sit in the app.

## The judges

- `rules`: today's word rules from `frontend/src/interruptWords.js`, run
  through Node. They read only the words.
- `claude`: the app's utility model, `claude-haiku-4-5` unless you pass
  `--claude-model`.
- `jev`: TypeSafe's Jev. It needs `TYPESAFE_API_KEY` in the environment
  or in the file you pass with `--env`. Without one, its rows show errors.
- `local`: any OpenAI-compatible server on your Mac, like
  `mlx_lm.server`. Start it yourself with a model that's already
  downloaded, and set `HF_HUB_OFFLINE=1` so it can't fetch one.
- `mock`: answers every moment right, to check the harness without keys.

## Running it

```bash
.venv/bin/python -m eval_bargein --mock
.venv/bin/python -m eval_bargein --judge rules --judge claude --env ~/dev/crossband/.env
.venv/bin/python -m eval_bargein --judge jev --env ~/dev/crossband/.env
.venv/bin/python -m eval_bargein --judge local --local-url http://127.0.0.1:8131/v1 --local-model mlx-community/Qwen3.5-4B-OptiQ-4bit
```

You can also run it from the Analysis page, with the rules, Claude and
Jev.

## What it costs and touches

It sends made-up moments and nothing of yours. Claude costs about a cent a
run. Jev bills on TypeSafe's terms. A model on your Mac costs nothing but
time.

## What it can't tell you

Made-up moments don't sound like a real kitchen. The words arrive typed,
so there are no transcription mistakes, and nobody's voice is checked.
With `--as-heard` they arrive the way a live transcript often has them,
in lower case with no punctuation, which is closer but still clean.
The trial picks a judge worth trying in the app. It isn't the final word
on one.
