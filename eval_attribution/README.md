# Attribution replay harness

This package answers one question offline: which transcript shape lets a
seat answer "who said what" correctly, including about itself? It's the
replay experiment for the field case where a seat took another seat's
remark as its own.

It measures and nothing more. It never touches `backend/engine.py`, and
nothing here changes what a live round sends. It produces the numbers a
person needs to decide whether the projection should change.

## The three shapes

| shape | what a seat sees |
|---|---|
| `current` | what the app sends today: its own turns bare, everyone else labelled "[Name · time]". Built by the real provider builders. |
| `self_labelled` | the same, and its own turns carry the same head as everyone else's. |
| `envelope` | other seats' turns wrapped in a `<member>` envelope inside the user role, the owner plain, plus one preamble line saying so. |

Every call uses the real seat system prompt from `split_system_prompt`,
so a result carries over to the live app.

## What a probe is

One fixture is one made up group chat with two seats and an owner. Each
probe names the seat being asked, the question, and the expected answer.
The question rides as a final owner turn with a one line answer rule,
either "answer with exactly one name from this list" or "answer yes or
no". A probe is one of these kinds.

- `who` expects a name from the roster, the owner, or `self` for the
  probed seat.
- `yesno` is for questions like "did you bring up the jacket?".

The report keeps the probes whose answer is the seat itself apart from
the rest. That is the case the current shape is suspected of failing.

## Run it

```sh
# smoke-test the harness itself, no keys and no cost:
.venv/bin/python -m eval_attribution --mock

# the real experiment, one model per family; needs the keys:
.venv/bin/python -m eval_attribution --model claude-haiku-4-5 --model gpt-5 \
  --max-tokens 4000 --timeout-s 120

# one shape only, a private replay set kept outside the repository:
.venv/bin/python -m eval_attribution --model claude-haiku-4-5 \
  --variant self_labelled --fixtures-dir /path/outside/git --no-builtin-fixtures
```

GPT-5 thinks before it answers, and that thinking counts against
`--max-tokens`, so at the default of 40 every GPT-5 reply comes back
empty and scores 0%.

`--mock` runs the whole pipeline against a keyless stand in that acts
out the hypothesis. Under `current` it answers self probes wrongly, and
under the labelled shapes it answers correctly bar a fixed few. That
shows what the report looks like, and its numbers say nothing about any
model.

The fixtures in `fixtures/` are six chats carrying twenty seven probes.
All of them are made up, with placeholder names, placeholder topics and
no real chat. A replay set from real history belongs outside the
repository.

The harness's own machinery is pinned by `tests/test_eval_attribution.py`,
keyless:

```sh
.venv/bin/python -m pytest tests/test_eval_attribution.py -q
```

## Reading the report

The headline table gives accuracy per shape per model, overall and on
the self probes. A shape only earns a projection change if its self
probe accuracy is clearly higher and its overall accuracy is no lower.
Misses are listed with the reply text, so you can tell a wrong parse
from a wrong answer. Cost is priced from the rate card, and a model
missing from the card shows no cost.

## Fixture schema

| field | meaning |
|---|---|
| `id`, `category` | identifiers, and `category` groups the report |
| `user_name` | the owner's display name |
| `roster` | list of `{slug, name}` seats |
| `transcript` | list of `{speaker, content}`, where a speaker is a roster slug or `user` |
| `probes` | list of `{seat, question, expected, kind, tag}` |
| `notes` | why the fixture exists |

Timestamps are added at run time, a minute apart, so the labels carry a
real time like the live ones.
