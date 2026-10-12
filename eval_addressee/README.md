# Addressee trial

## What it measures

In a voice chat, every turn goes to every seat, and each seat decides for
itself whether to answer. When you're alone that's fine. When someone else
is in the room, a lot of what's said isn't for the AIs. You might ask your
partner if the oven's on, or a guest might tell you about their day. Each
of those turns still starts a round. A turn with a question mark also stops
the first seat from passing, so it answers a question that was never put
to it.

This trial measures whether one quick check per turn could say three
things before the round starts:

- for: the AIs, a person in the room, nobody, or the TV
- asks: whether the AIs owe a reply, such as a question or request to
  them, or an answer to something they asked
- seat: which seat the turn names, all of them, or none

Each judge answers the same made-up turns. Each turn says who's in the
room, the last few things said, who's speaking and what they say. Each
also carries the right answer to all three questions.

The report counts the mistakes that matter most:

- Wrong skips are turns for the AIs judged not for them. A real question
  would go unanswered.
- Needless rounds are turns for someone else judged for the AIs. Each
  costs a round of calls that end in a pass.
- Forced replies are turns that owe the AIs nothing, judged as owing them
  a reply. A seat would talk over the room.
- Missed replies are turns that owe a reply, judged as owing none.

It also reports how often each judge is right on all three, how long it
took and what it cost.

## The judges

- `rules`: what the app does today. Every turn is for the AIs. A question
  mark or a seat's name means a reply is owed, and a turn that opens with
  one seat's name picks that seat.
- `jev`: TypeSafe's Jev, asked all three questions in one call. It needs
  `TYPESAFE_API_KEY` in the environment or in the file you pass with
  `--env`. Without one, its rows show errors.
- `claude`: the app's utility model, `claude-haiku-4-5` unless you pass
  `--claude-model`. It answers all three in one JSON reply.
- `mock`: answers every turn right, to check the harness without keys.

Both models read the same description of a turn. The description and the
questions live in `backend/addressee.py`, so a live trial would send what
was measured.

## Running it

```bash
.venv/bin/python -m eval_addressee --mock
.venv/bin/python -m eval_addressee --judge rules --judge jev --judge claude --env ~/dev/crossband/.env
```

You can also run it from the Analysis page, with all three judges.

## What it costs and touches

It sends made-up turns and nothing of yours. Claude costs about 3 cents a
run. Jev costs about a fifth of a cent at TypeSafe's published price.

## What it can't tell you

The wording of Jev's questions was tuned on these same turns, so its score
here is better than a fresh set would give. A shadow run on real turns
would say more. The turns arrive typed, so there are no transcription
mistakes, and the speaker is always known. In the app, a voice the app
can't name yet arrives as an unknown speaker.
