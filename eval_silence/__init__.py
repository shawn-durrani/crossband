"""Behavioural eval: does the group-chat speak/pass judgment match the
relational-cost-of-silence principle in backend/providers.py?

This is a FIXTURE-and-CONSISTENCY harness, not a live-model grader (unlike
eval_critic/, which can also drive a real critic model against real prompts).
There is no live model call here on purpose: "does replying feel like the
right call" is exactly the judgment the rule leaves with the model rather
than detecting mechanistically (no greeting/keyword classifier). What IS
testable without a model is that the fixture set encodes the general
principle -- pass only when BOTH the informational value of speaking AND the
relational cost of staying silent are low -- consistently, across a small
contrasting matrix (group check-in, resolved factual question, roll-call,
mid-debate paraphrase, direct address, and the quiet family: a quiet
request, a stale answered question, room chatter, a named seat). See
eval_silence/README.md; tests/test_pass.py replays the quiet family through
a real round against the pass guard.

Nothing here is imported by backend/engine.py or backend/providers.py --
engine.py's pick_responders routing is untouched by this eval (a hardcoded
detector was considered and rejected), and this package never runs against a
live message.
"""
