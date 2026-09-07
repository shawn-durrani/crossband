# Keeping Crossband running

Crossband is a service that runs on your computer and answers on port
8902. Say that computer is one you use as a home server, and you reach
the app from your phone. Then you want it to start on its own, come
back if it dies, and survive a reboot. Without that, a crash leaves the
app dark until you notice and start it again by hand. The sidebar shows
no chats while it's down, because the chat list comes from the server.

A supervisor is a program that starts another program and keeps it
running. On macOS the built-in supervisor is launchd, and the repo
ships a one-command installer that hands Crossband to it.

## Install the supervisor (macOS)

```sh
ops/install-supervisor.sh
```

That's it. The script does two things. First it fills your computer's
real paths into a template, `ops/dev.crossband.server.plist.template`,
and writes the result to `~/Library/LaunchAgents/dev.crossband.server.plist`.
That file tells launchd what to run. Then it stops any copy of the app
you started by hand and hands the one real copy to launchd. The template
holds no personal path, so nothing about your machine is ever committed.

launchd calls a job like this an agent, and this one is named
`dev.crossband.server`. In every `launchctl` command, `gui/$(id -u)`
tells launchd to look in your own login session.

From then on launchd owns the service. It starts the app at login,
starts it again within about a second if it exits for any reason, and
brings it back after a reboot. If the app crashes as soon as it starts,
launchd waits ten seconds between tries. A broken boot reads as a slow,
steady retry in `data/service.log`.

This replaces `./start.sh` for everyday use. While the supervisor is
installed, launchd holds port 8902. `./start.sh` waits ten seconds for
the port to come free, then refuses to run: "✗ something is still
listening on port 8902 after 10s (pid …)". The message's advice to kill
that process doesn't apply here. Killing the process only makes launchd
start it again a second later. Use the restart command in
[Everyday commands](#everyday-commands) instead. `./start.sh` is the
right command again once you've run `launchctl bootout` on the agent.

## Everyday commands

Run these from the repo folder. The `tail` path and the installer are
relative to it. The installer prints the absolute path of the log when
it finishes.

```sh
# restart it, after a git pull or to pick up .env changes
launchctl kickstart -k gui/$(id -u)/dev.crossband.server

# is it running, and as which pid?
launchctl print gui/$(id -u)/dev.crossband.server | grep -iE 'state|pid|program'

# follow the log
tail -f data/service.log

# stop supervising, which also stops the service
launchctl bootout gui/$(id -u)/dev.crossband.server

# start supervising again
ops/install-supervisor.sh
```

The log rotates at each boot once it passes 10MB. The log it replaced
is kept beside it as `data/service.log.1`.

## Backups

The app backs up its database and its learnt voices on its own, into
`data/backups/`. How often, how many it keeps, and an optional mirror
folder are settings. [docs/CONFIG.md](CONFIG.md#backups) lists them.

## Deploying a change

launchd is the one owner of the process, so a deploy has to restart the
service through launchd. A deploy that starts a second copy makes the
two copies fight over the lock file. That file lives in `data/`, and the
running copy holds it so a second copy can't open the same database. If
you script deploys, end the script with:

```sh
launchctl kickstart -k gui/$(id -u)/dev.crossband.server
```

Use that while the agent is loaded, and fall back to `./start.sh` when
it isn't. `kickstart` fails if there's no agent to kick. Either way you
can't end up with two copies by accident. `start.sh` checks the port
first and refuses to start a second copy, so the worst case is a deploy
step that fails loudly.

A deploy can ask whether the service is busy before it restarts. Send
`GET /api/busy`, the busy route, from the same computer and it answers
`{"busy": true, "reasons": [...]}` with no login needed. That's the same
rule as the health route, `/api/auth/session`, which a deploy checks to
see that the app is up. Busy means a round is still generating in any
chat, a voice capture is live, or a Claude Code guest is at work. It
also means a sync of people to memory, a benchmark, an import or a
backup is part way through. The reasons are fixed labels, never
anything from a chat. Wait for `"busy": false`, then restart. A
service too broken to answer is restarted anyway.

### How a deploy watcher does it

A deploy watcher is a program on the same computer that merges a pull
request once you've approved it, then restarts the app. The one that
ships Crossband's own changes does all of this. It isn't part of
Crossband, because most installs should never have an app that merges
and restarts itself. [docs/PRODUCERS.md](PRODUCERS.md) has the rules
for writing one.

You type `deploy crossband #12` in a chat. Within a minute the watcher
reads it, checks the pull request's CI is green, and merges it as one
commit. It pulls main into its checkout, installs any new dependencies,
and runs the test suite. If the tests pass it builds the frontend while
the old server is still running, so the gap while it restarts is only
Python starting up. Then it asks the busy route every twenty seconds,
for up to fifteen minutes, and posts one line in chat while it waits.
Once the app is free it restarts it through launchd, then asks the
health route every five seconds until the new process answers, for up
to three minutes. One line in chat says the app is live and healthy.
If the tests, the build or the busy wait fail, there's no restart, the
old code keeps running, and the chat and the pull request both say
why. If the new process doesn't answer within three minutes, the chat
says to check the machine.

```mermaid
flowchart LR
  Y(["You"])
  CB("Crossband")
  W("Deploy watcher")
  GH("GitHub")
  T["Test suite"]
  B["Frontend build"]
  Q{"Busy?"}
  R["Restart through launchd"]
  H["Health check,<br/>every 5s"]
  N["Chat: live<br/>and healthy"]
  S["Chat says why"]
  Y -- "types deploy<br/>crossband #12" --> CB
  CB -- "the command,<br/>within a minute" --> W
  W -- "merges once<br/>CI is green" --> GH
  GH -- "pulls main" --> T
  T -- "green" --> B
  B -- "built" --> Q
  Q -- "yes: asks again in 20s,<br/>for up to 15 min" --> Q
  Q -- "no" --> R
  R -- "kickstart -k" --> H
  H -- "answers within 3 min" --> N
  T -- "fail" --> S
  B -- "fail" --> S
  Q -- "still busy after 15 min" --> S
  H -- "no answer in 3 min" --> S
  classDef node fill:#d4d4d8,stroke:#757575,color:#18181b
  classDef hero fill:#38bdf8,stroke:#0284c7,color:#18181b,stroke-width:2px
  classDef bad fill:#fca5a5,stroke:#dc2626,color:#18181b
  class Y,CB,W,GH,T,B,Q,H,N node
  class R hero
  class S bad
```

## Stopping it

A stop finishes within fifteen seconds. It starts with `SIGTERM`, the
signal `kill` and launchd send to ask a process to stop. Every open
browser tab holds a connection to `/api/events/stream`, which is how
new messages reach the page, and that connection never ends on its own.
Uvicorn, the web server the app runs in, waits for every open
connection to finish before it exits. The app ends those streams the
moment the signal arrives, so that wait is over in milliseconds.
Anything still in flight, a chat round part way through generating or a
live voice call, gets up to fifteen seconds to finish, and then the app
exits regardless. A tab that was watching the old process reconnects on
its own and catches up from the database, so nothing is lost across a
restart.

Change the fifteen-second ceiling with `CROSSBAND_SHUTDOWN_TIMEOUT_S`,
or `"shutdown_timeout_s"` in `config.local.json`. Raise it if you'd
rather a long round always finish. Lower it for a deploy loop that
wants a fast, predictable stop.

Two more things make a restart forgiving. Startup waits up to ten
seconds for a lock still held by the copy that's shutting down, so a
restart a second too early succeeds. If the lock is still held after
that, the message says whether the process holding it is alive, with
the command to end it. If you see "Another instance is already running
against this data directory", the process it names is stuck draining a
connection, and `kill -9` on it ends it.

## Turning up the log for a while

By default only warnings and errors from the app's own code reach
`data/service.log`. The app also writes a line per Claude call saying
how much of it came from cache, with no chat content in it. Those lines
are at `INFO` level, so they're quiet by default. Set
`CROSSBAND_LOG_LEVEL=INFO`, or `"log_level": "INFO"` in
`config.local.json`, for the session where you want them, restart, then
unset it. That changes only what's written to the log. Nothing about
what's cached, priced or billed changes with it.
[docs/COST_TELEMETRY.md](COST_TELEMETRY.md) has the before-and-after
workflow.

## Not on macOS?

The same idea works with systemd on Linux: a unit with `Restart=always`
and `WantedBy=default.target`. The repo doesn't ship a unit file. The
plist template's `ProgramArguments` run `bash start.sh` with the repo as
the working directory, and those map straight onto a systemd `ExecStart`
and `WorkingDirectory` if you write one.
