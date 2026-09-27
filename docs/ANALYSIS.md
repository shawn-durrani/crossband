# Running a measurement from the Analysis page

Some questions about how the app behaves need numbers before anyone can
decide. Can a cheap critic catch a made-up memory fact? Are the facts a
round fetches from memory worth the wait? Each question has a harness
in the repo that answers it with a report. The Analysis page runs them
for you, from the Mac or your phone, and keeps every report.

Open it from the sidebar, under Analysis.

## Two kinds of harness

A guard runs on every change in CI. It needs no key, passes or fails,
and pins a rule so it can't drift. The silence eval and the doc style
test are guards. CI presses them, so they never get a button and the
page doesn't list them.

A measurement runs by hand when a decision needs numbers. It produces a
report to read, and it can cost money or touch your own data. The page
lists the measurements only.

| Measurement | What it answers | What it reads |
|---|---|---|
| Critic eval | Can a cheap critic catch a made-up memory fact in a draft reply? | made-up drafts from the repo |
| Attribution replay | Which transcript layout lets a seat say who said what? | made-up group chats from the repo |
| Recall replay | Are the facts a round fetches from memory worth the wait? | your own chat history and your membro |
| Spoken intent check | Does the model call that reads every turn hear a spoken instruction? | made-up turns from the repo |
| Voice rig | Does the app put the right name on each spoken turn? | made-up conversations in synthetic voices |

Each harness has its own `README.md` with the detail and how to read
its report. [docs/README.md](README.md) links every one of them.

## Before you press Run

Every measurement says what it costs, how long it takes and what it
touches, over its buttons. A run that can spend money or read your
data asks once more, with the same words, before it starts. The costs
are rough, from each harness's own docs or a measured run, and a
finished report states what it spent where the harness counts it.

Practice run is the free button. It runs the same harness with keyless
stand-ins and made-up numbers, so it only tells you the harness works.
Its report says so at the top.

Each measurement runs with the settings its `README.md` calls the real
run.
The page takes no options, so there's no way to point it at a private
replay set or ask it for more. Anything else is a terminal run, and
each card shows the command to start from.

## While it runs

A run is a background job. It keeps going when you leave the page, lock
the phone or close the tab, and a dot beside Analysis in the sidebar
shows while anything runs. On the page, the running measurement shows
how long it's been going. After twenty seconds the app checks in on it
every thirty, and the line under it says when it last did.

One run of each measurement at a time, and different measurements can
run side by side. Stop sends the harness the same signal as pressing
Ctrl-C in a terminal, so it cleans up after itself. A run that goes far
past its expected time is stopped the same way and marked as having run
out of time.

A deploy waits while a measurement runs. The app's busy route reports
it, so the deploy watcher holds the restart until it ends.
[docs/OPERATIONS.md](OPERATIONS.md#deploying-a-change) has how that wait
works. If the app stops anyway, it stops each run the same way first,
and the report says the app restarted while it ran.

## Where the reports go

Each run gets its own folder under `data/analysis/`, with the report as
the terminal would print it, the same report as JSON, and a small
record of the run. Only your user account can open the folder or read
anything in it. Past reports are listed newest first, and each opens on
the page. The JSON downloads from there too.

Reports stay until you delete them from the page.

## Who can use it

Only you, signed in. Every part of the page needs your owner session,
even on an install with no owner password yet, where the rest of the
app is open to this Mac. Until you set a password the page says so and
runs nothing. That keeps it from a Claude Code guest working on this
Mac, from a phone on your tailnet that hasn't signed in, and from the
tools that post notices into chats.

The seats can't start a measurement either. There's no tool for it,
because a spoken "run the recall replay" would spend your money on
whoever said it.

## Your own data stays yours

The recall replay is the one measurement that reads your own chats. It
reads the app's database without changing anything and asks your
membro for the facts behind each of your newest 200 turns. Its report
keeps ids, scores and timings, and never the words. The harness can
write the words out when you ask it to from a terminal, and the page
never asks. Every recall report is checked before it's kept, and one
holding any words is deleted.

## The voice rig and your app

The voice rig starts a second copy of the app on port 8920, with its own
data folder and memory pointed nowhere. It speaks made-up conversations
into that copy and scores the names it writes. Your chats, people and
memory aren't touched. When the run finishes, the copy stops and its
folder is removed. A run you stop keeps the folder, with the second
app's log, in the rig's cache for a look.

It shares the diariser with your app, and the diariser serves both on
one model. The rig won't start while a voice chat is live, and a voice
chat started during a run may name voices a little slower until it
ends. The first run downloads the voice models and has ElevenLabs speak
every line, which is kept for next time.
