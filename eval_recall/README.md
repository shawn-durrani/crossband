# Recall replay harness

This package answers one question about memory. When a round asks membro
for the facts behind the user's latest turn, does it get facts worth the
wait? Every round already runs that recall and puts the top six facts in
front of each seat. What nobody has measured is whether those six are
the right ones, how often the recall comes back with nothing useful, and
where a floor on the score would stop noise from reaching the prompt.

It measures and nothing more. It never touches `backend/engine.py`, and
nothing here changes what a live round sends. It reads your own chat
history and asks your own membro, so the numbers are about your install
and they belong to you.

## What it replays

The engine keys the ambient recall on the text of the user's latest
message, trimmed to 500 characters, and asks for six facts scoped to
that chat. The harness reads every user turn out of `chat.db`, newest
first, and sends the same query to the same route with the same chat
scope. It asks for ten facts, so it can see what ranks seven to ten
would add. A slash command runs no round, so it is skipped, and a chat
with memory switched off never recalls, so its turns are skipped too.

Each fact that comes back is checked against the standing summary, the
memory profile every prompt carries anyway. A fact whose content words
are mostly in the summary already is marked carried. A fact the summary
doesn't hold is marked new, and new facts are the ones the recall earns
its place with.

## Run it

```sh
# smoke-test the harness itself, no membro and no chat history:
.venv/bin/python -m eval_recall --mock

# your own install: the newest 200 user turns, against the membro in .env
.venv/bin/python -m eval_recall

# one chat, every turn, and the corpus written out for tuning
.venv/bin/python -m eval_recall --chat 42 --max-turns 0 \
  --format json --with-content --out /path/outside/git/recall-42.json
```

Membro has to be up, and the harness stops with a message if it isn't.
Each replayed turn is one `/recall`, so a run of 200 turns is 200
recalls in a row, and each one embeds its query through membro's
embedding provider. Each lands in membro's access log under the origin
`eval`, apart from the `auto` the live rounds use. The summary is
fetched once, as it stands today, so a fact the summary learnt last
week reads as carried even for a turn from last month.

`--mock` runs the whole pipeline against a made up ledger and a stand
in memory that scores facts by word overlap. It shows what the report
looks like, and its numbers say nothing about your install.

The harness's own machinery is pinned by `tests/test_eval_recall.py`,
keyless and without membro:

```sh
.venv/bin/python -m pytest tests/test_eval_recall.py -q
```

## Reading the report

The first table is the floor sweep. For each candidate floor on
membro's score it gives the share of turns that would put at least one
fact in the prompt, the mean number of facts they would put there
within the live six, and how many of those the summary didn't already
carry. The floor to pick is the highest one that keeps the new facts
and drops the rest. A floor of 0 is what ships today.

The second table is by rank. For each rank from one to ten it gives how
often a fact exists at that rank, its mean score, and how often it's
new against the summary. If ranks seven to ten are as often new as rank
six, six is too few. If ranks four to six are rarely new, six is too
many.

Latency is the time each recall took, at the median and the 95th
percentile. That is what a turn pays for the lookup, and it's the
number to hold beside the new facts per turn.

The JSON report carries one row per turn with the fact ids, scores and
ranks, and no content unless you pass `--with-content`. That file is
the corpus for tuning. With content it holds your own memory, so keep
it outside the repository.

## What it can't tell you

Whether a seat used the fact. A fact in the prompt isn't a fact in the
reply, and the seat ledger and the attribution audit are where that is
looked at. Whether a carried fact still helped, because the summary
states a fact once and a recalled fact carries its date, origin and
confidence. And whether the summary at the time of an old turn matched
the summary now.
