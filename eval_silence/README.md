# Silence eval: speak or pass in a group chat

## What the eval measures

In a group chat each model decides for itself whether to reply or stay
quiet. A rule that judges only whether a reply would repeat information
gets a whole class of cases wrong. When someone greets the whole group,
a second "good, thanks" adds no information at all, and a member that
stays quiet still reads as absent, or as ignoring the person who spoke.

The rule in `backend/providers.py` asks one general question of every
turn, and never sorts messages by type. Would this silence be noticed,
and read as absence? Speaking and staying quiet each carry a cost, so a
member passes only when both are low. Either a high informational value
of speaking or a high relational cost of staying silent means speak.

A hardcoded greeting or phrase detector was considered and rejected. The
problem is the relational contract of a group conversation, and a
detector can only list the surface forms someone might use to address
the room, so it would need extending for every new one.

This package is the small fixture matrix that holds that principle
steady as the prompt text changes. Each fixture is one scenario, graded
by a human on the two axes and carrying the verdict a human expects.
The eval checks that the grading implies the verdict under the rule. It
is built fixture style like `eval_critic/`, with one structural
difference.

## There is no live model runner

`eval_critic/` can drive a real critic model against real prompts,
because "is this claim grounded in the supplied memory" has an answer
a model can check. "Does staying quiet feel like being ignored" has no
such answer, and mechanising that judgment is what the greeting
detector decision rejected. The judgment stays with the model, under a
clearer prompt rule.

What can be tested without a model is whether the fixture set encodes
the principle the same way across contrasting scenarios. The first five
are a group check in, a resolved factual question, a roll call, a mid
debate paraphrase, and a direct address. The quiet family covers a room
where the seats were asked to stay quiet. It has the quiet request
itself, a question both seats already answered, two people talking to
each other, and a seat named after a quiet request.
`eval_silence/policy.py` expresses the rule as code for that
consistency check alone. It is never imported by `backend/engine.py` or
`backend/providers.py`, and it never runs against a live message.

Routing is a separate concern and the eval leaves it alone.
`pick_responders` and `_vocative_responders` in `backend/engine.py`
already put the full roster into an unaddressed "Hey guys" round. The
eval covers each model's own speak or pass judgment after routing has
offered it the turn.

## The contrast that matters

`seed_group_checkin.json` and `seed_resolved_factual_question.json`
have the same redundancy shape, in that a second identical answer adds
no new information, and opposite expected verdicts. What separates them
is whether the speaker was being addressed as part of the group.

`seed_asked_to_stay_quiet.json` and `seed_quiet_then_named.json` make
the same point after a quiet request. In both the seat has nothing new
to say. People in the room asking each other something leaves its
silence unremarkable, however long the quiet has run. Naming the seat
invites it back in, and silence then reads as absence.

## The quiet family goes through a real round

The pass guard in `backend/engine.py` can't tell who a question is for.
It sees a question mark in the newest turn, holds back the first seat's
`[pass]` once, and states a note on the retry. Each quiet family
fixture also runs through a real round in `tests/test_pass.py`, because
a note that orders an answer makes an obedient seat answer something,
often a question it already answered. A stand in seat there does what
the retry note says. A pass verdict must leave no seat reply behind, and
a named seat must answer. No model is called.

## Fixture schema

| field | meaning |
|---|---|
| `id`, `category` | identifiers |
| `responder` | the roster member whose speak or pass call is being judged |
| `conversation` | list of `{speaker, content}`, the visible transcript |
| `already_answered_by` | who already said something to the same effect |
| `informational_value` | `low` or `high`. Would speaking add a new fact or angle? |
| `relational_cost_of_silence` | `low` or `high`. Would quiet read as absence or as ignoring? |
| `expected_verdict` | `speak` or `pass` |
| `notes` | why the fixture exists |

A human grades the two axes when the fixture is written, and grades the
verdict too. The eval checks that the axes imply the verdict under the
rule. That catches a fixture added with a gut feel verdict its own
grading doesn't support.

Every committed fixture is made up, with generic roster members such as
`gpt` and `claude` and invented exchanges, so the corpus holds no real
conversation. Fixtures replayed from real group chats belong outside
the repository. `load_fixtures` takes extra directories, the same way
`eval_critic`'s `--fixtures-dir` does, and can skip the committed
corpus:

```python
from eval_silence.fixtures_loader import load_fixtures

load_fixtures(dirs=["/path/outside/this/repo/private-replay"],
              include_builtin=False)
```

## Run it

```sh
env -u OPENAI_API_KEY -u ANTHROPIC_API_KEY .venv/bin/python -m pytest tests/test_silence_eval.py -q
```

No keys, no network, no model calls. The suite loads the seed corpus,
checks each fixture against the schema, checks that every scenario is
still present, pins the four axis combinations of `policy.py`, asserts
every fixture's axes imply its verdict, and asserts both contrast pairs
still resolve oppositely. The wording of the live rule is pinned
separately, in `tests/test_silence_rule.py`. The quiet family's round
replay is in `tests/test_pass.py`.

## How to read the result

Green means every fixture's graded axes imply its graded verdict under
`eval_silence/policy.py`, and the scenarios still illustrate one
principle.

Green doesn't mean the prompt change works on live models. What the
harness holds in place is the principle and the scenarios that
illustrate it, so a later prompt rewrite can't quietly change what the
rule means.

## What a failing case looks like

A fixture graded against itself. Edit `seed_group_checkin.json` to
`"relational_cost_of_silence": "low"` while leaving
`"expected_verdict": "speak"`, and `decide("low", "low")` returns
`pass`. `test_every_seed_fixture_matches_the_general_principle` then
fails with `group_checkin_all_speak` as the assertion message. The
harness can't tell you which of the two fields is wrong. That is the
judgment call it hands back to you.

The matrix stops making its point. The test named
`test_redundancy_alone_does_not_decide_it` pins
`group_checkin_all_speak` and `resolved_factual_question_pass` to the
same `informational_value`, to opposite verdicts, and to different
relational costs. The same edit fails that test too, which is the
harness saying the two scenarios have stopped showing that redundant
content on its own decides nothing.

A fixture that fails the schema. `load_fixtures` raises `FixtureError`
for an axis value outside `low` and `high`, a verdict outside `speak`
and `pass`, a missing required field, an empty `conversation`, a
duplicate `id` across two files, or a fixtures directory that doesn't
exist. Deleting one of the seed files fails
`test_builtin_seed_corpus_loads_and_validates` instead, naming the
category that went missing.

The rule itself drifting. `test_decide_passes_only_when_both_axes_low`
pins all four axis combinations, so a rewrite of `decide()` that passes
whenever informational value alone is low goes red at once. Drift the
other way, where the prompt text in `backend/providers.py` changes and
`policy.py` quietly stops mirroring it, is beyond what the suite can
see. Keeping those two in step is a human check whenever either one is
edited.

## Extending it

Regression coverage for whether a change moves live model behaviour
needs live conversations, which the harness doesn't have. Add a fixture
when a new scenario sharpens the contrast between informational value
and relational cost. Grade both axes before you choose the verdict, so
the verdict follows the grading and never the other way round.
