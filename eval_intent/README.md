# Spoken intent harness

You can change how a chat works by saying so: "group mode on" lets
several people talk, "this is Dave" adds a person, "her name is spelt
Aleks" fixes a name, "think harder" makes a seat think more, and
"research more" turns on research mode. The app hears all of these
with one call to the utility model per turn. That call reads the
message and returns every kind of instruction at once. This package
measures how well it hears them, against a baseline of four fixed
phrase lists.

It measures and nothing more. It never touches the live scan, and
nothing here changes what the app hears. It runs the app's own prompt
and parser from `backend/intent.py`, so a change to either shows up in
the next run. Run it when you change that prompt or try a different
utility model.

## What a fixture is

One fixture is one made up user turn and what the app should hear in
it, graded by hand across five axes: a room mode switch, introductions
and departures with any alias, name corrections, a thinking depth
change with a one-off flag, and a research request. A fixture also
names the owner, who is known present, who is known by name, and the
seats, because the prompts take those. An empty expectation means
the turn is plain chat, or only talks about one of these things.

The corpus in `fixtures/` covers the wordings the phrase lists are
known to drop, the negatives the depth rules guard against, research
requests against tool requests, and turns that carry two instructions
at once. It also covers requests for the seats to hold back, like
"just eavesdrop until we ask", which must never switch room mode on or
off. Then there are words spelt out letter by letter to fix the
transcript, like "K-E-R-F", and words spelt in a word game, like "is
Z-O-O-S a word?". Those are never a name correction, while a person's
name spelt out still is one. All of it is made up. A set built from
real turns belongs outside the repository.

## What it compares

The `merged` strategy is the path the app runs, one call per turn. It
also applies the app's rule for a word spelt out letter by letter, so
its score is what the app would act on.
The strategy named `today` is the baseline, and the app doesn't run it.
It checks each turn against the four phrase lists, sends a separate
prompt for each list that fires, and hears no research cue at all. The
lists stay in the code only so the harness has that baseline. Both
paths parse replies with the app's own parsers, so the judge is the
same and only the gate changes.

One number needs no model and no key: how many instruction turns the
phrase lists never send to a model. It comes from running the lists
over the corpus, so it is true in mock mode too.

## Run it

```sh
# smoke-test the harness itself, no keys and no cost:
.venv/bin/python -m eval_intent --mock

# the real comparison on the utility model; needs ANTHROPIC_API_KEY:
.venv/bin/python -m eval_intent

# one path only, or a private corpus kept outside the repository:
.venv/bin/python -m eval_intent --strategy merged \
  --fixtures-dir /path/outside/git --no-builtin-fixtures

# from a summoned guest in run mode, whose worktree has no .env:
.venv/bin/python -m eval_intent --env ~/dev/crossband/.env
```

A summoned guest starts with the provider keys blanked, so that its own
turns bill the Mac's Claude Code login and never your metered key.
`--env` takes the keys from the app's own file for this run alone, and
the guest's own turns stay on the login.

`--mock` runs the whole pipeline against keyless stand ins that answer
with the graded verdicts, wrong on a fixed few, so the report has
misses to show. Its accuracy figures say nothing about any model. The
dropped-turn count is real either way.

The harness's own machinery is pinned by `tests/test_eval_intent.py`,
keyless:

```sh
.venv/bin/python -m pytest tests/test_eval_intent.py -q
```

## Reading the report

Near the top is the dropped-turn count, the instruction turns the
phrase lists never send to a model, and on which axes. The report calls
them today's lists. It's a count for the baseline, and the app itself
sends every turn to the model.

The strategy table gives, for each path, the share of turns heard
right on every axis, the calls per turn, the cost per turn from the
rate card, and the median latency. The merged path should hear at
least as much as the baseline on every axis, and the cost line says
what that costs.

The axis and category tables say where each path fails. Misses are
listed with the turn's text and the reply, so a wrong parse can be told
from a wrong answer.

## What it can't tell you

Whether a real person would have said any of these. The corpus is
written by hand, and the scan logs are content free, so there is no
recorded corpus to mine. Add a fixture when a wording is missed in
use, grade it before you look at what either path says, and rerun.
