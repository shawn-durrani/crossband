# Silence eval: speak or pass in a group chat

## What the eval measures

In a group chat each model decides for itself whether to reply or stay quiet. A
rule that judges only informational redundancy gets a whole class of cases
wrong. On a plain group-directed greeting a second "good, thanks" adds no
information at all, and a member that stays quiet still reads as absent, or as
ignoring the person who spoke.

The rule in `backend/providers.py` therefore asks a general question instead of
classifying message types: would this silence be noticed, and read as absence?
Speaking and staying quiet each carry a cost, so a member passes only when BOTH
are low: the informational value of speaking AND the relational cost of staying
silent. Either one being high means speak.

A hardcoded greeting or phrase detector was considered and rejected. This is a
relational-contract problem in a group conversation, and a detector can only
enumerate the surface forms someone might use to address the room, so it would
have to be extended for every new one.

This package is the small behavioural fixture matrix that keeps that principle
honest as the prompt text evolves. Each fixture is one scenario, graded by a
human on the two axes and carrying the verdict a human expects; what the eval
checks is that the grading implies the verdict under the rule. It is built
fixture-style like `eval_critic/`, with one structural difference described
next.

## There is no live-model runner here, on purpose

`eval_critic/` can drive a real critic model against real prompts because "is
this claim grounded in the supplied memory" has an objective answer a model can
check. "Does staying quiet feel like being ignored" does not, and mechanising
that judgment is exactly what the greeting-detector decision rejected. It stays
with the model, under a clearer prompt rule.

What is testable without a model is whether the fixture set encodes the
principle **consistently** across five contrasting scenarios: group check-in,
resolved factual question, roll-call, mid-debate paraphrase, and direct
address. `eval_silence/policy.py` expresses the rule as code for that
consistency check alone. It is never imported by `backend/engine.py` or
`backend/providers.py` and never runs against a live message.

Routing is a separate concern and is untouched by this work.
`pick_responders` / `_vocative_responders` in `backend/engine.py` already put
the full roster into an unaddressed "Hey guys" round. What this eval covers is
each model's own speak-or-pass judgment after routing has already offered it
the turn.

## The contrast that matters

`seed_group_checkin.json` and `seed_resolved_factual_question.json` have the
*same* redundancy shape, in that a second identical answer adds zero new
information, and *opposite* expected verdicts. What separates them is whether
the speaker was being addressed as part of the group.

## Fixture schema

| field | meaning |
|---|---|
| `id`, `category` | identifiers |
| `responder` | the roster member whose speak/pass call is being judged |
| `conversation` | list of `{speaker, content}`, the visible transcript |
| `already_answered_by` | who already said something functionally equivalent |
| `informational_value` | `low` \| `high`: would speaking add a new fact or angle? |
| `relational_cost_of_silence` | `low` \| `high`: would quiet read as absence or as ignoring? |
| `expected_verdict` | `speak` \| `pass` |
| `notes` | why the fixture exists |

The two axes are graded by a human when the fixture is written. The verdict is
graded by a human too. The eval checks that the axes imply the verdict under
the rule, which is what catches a fixture that was added with a gut-feel
verdict its own grading does not support.

Every committed fixture is synthetic: generic roster members (`gpt`, `claude`)
and invented exchanges, so the corpus holds no real conversation. Fixtures
replayed from real group chats belong outside this repository. `load_fixtures`
takes extra directories, the same way `eval_critic`'s `--fixtures-dir` does,
and can skip the committed corpus entirely:

```python
from eval_silence.fixtures_loader import load_fixtures

load_fixtures(dirs=["/path/outside/this/repo/private-replay"],
              include_builtin=False)
```

## Run it

```sh
env -u OPENAI_API_KEY -u ANTHROPIC_API_KEY .venv/bin/python -m pytest tests/test_silence_eval.py -q
```

No keys, no network, no model calls. The suite loads the seed corpus, checks
each fixture against the schema, checks that all five scenarios are still
present, pins the four axis combinations of `policy.py`, asserts every
fixture's axes imply its verdict, and asserts the check-in and the resolved
factual question still resolve oppositely. The wording of the live rule is
pinned separately, in `tests/test_silence_rule.py`.

## How to read the result

Green means every fixture's graded axes imply its graded verdict under
`eval_silence/policy.py`, and the five scenarios still illustrate one principle
rather than several.

Green does not mean the prompt change works on live models. What this harness
does hold in place is the principle and the five scenarios illustrating it, so
that a later prompt rewrite cannot quietly change what the rule means.

## What a failing case looks like

**A fixture graded against itself.** Edit `seed_group_checkin.json` to
`"relational_cost_of_silence": "low"` while leaving
`"expected_verdict": "speak"`, and `decide("low", "low")` returns `pass`.
`test_every_seed_fixture_matches_the_general_principle` then fails with
`group_checkin_all_speak` as the assertion message. The harness cannot tell you
which of the two fields is wrong; that is the judgment call it hands back to
you.

**The matrix stops making its point.** The test named
`test_redundancy_alone_does_not_decide_it` pins `group_checkin_all_speak` and
`resolved_factual_question_pass` to the same `informational_value`, to opposite
verdicts, and to different relational costs. The same edit fails that test too,
which is the harness saying the two scenarios no longer demonstrate that
redundant content on its own decides nothing.

**A fixture that fails the schema.** `load_fixtures` raises `FixtureError` for
an axis value outside `low`/`high`, a verdict outside `speak`/`pass`, a missing
required field, an empty `conversation`, a duplicate `id` across two files, or
a fixtures directory that does not exist. Deleting one of the five seed files
instead fails `test_builtin_seed_corpus_loads_and_validates`, naming the
category that went missing.

**The rule itself drifting.** `test_decide_passes_only_when_both_axes_low` pins
all four axis combinations, so a rewrite of `decide()` that passes whenever
informational value alone is low goes red at once. The other direction of
drift, where the prompt text in `backend/providers.py` changes and `policy.py`
quietly stops mirroring it, is beyond what this suite can see. Keeping those
two in step is a human check whenever either one is edited.

## Extending this

Regression coverage for "does this actually change model behaviour" needs live
conversations, which this harness does not have. Add a fixture here when a new
scenario sharpens the contrast between informational value and relational cost;
grade both axes before choosing the verdict, so the verdict follows the grading
rather than the other way round.
