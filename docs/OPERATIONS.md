# Operations: keeping Crossband running

Crossband is a long-running local service (port 8902). On a machine you use as
a personal server, reachable from your phone over Tailscale say, you want it
to behave like an appliance: start on its own, come back if it dies, and
survive a reboot. Without that, a crash or a stray shutdown leaves the app dark
(the sidebar shows no chats, because the chat list loads from the server) until
someone notices and restarts it by hand.

On macOS the built-in tool for this is **launchd**. This directory ships a
one-command installer that hands your Crossband process to launchd as a
supervised service.

## Install (macOS)

```
ops/install-supervisor.sh
```

That's it. The script:

1. Fills in your machine's real paths from a placeholder template
   (`ops/dev.crossband.server.plist.template`) and writes the result to
   `~/Library/LaunchAgents/dev.crossband.server.plist`. The template itself
   carries no personal path, so nothing machine-specific is ever committed.
2. Stops any instance you started by hand, then hands the one real instance to
   launchd.

From then on launchd **owns** the service: it starts at login, restarts it
within about a second if it exits for any reason, and brings it back after a
reboot. (If it crashes immediately on startup, launchd throttles the retries
to one every 10 seconds rather than spinning, so a boot failure reads as a
slow, steady retry loop in `data/service.log`, not a flood.)

**This replaces `./start.sh` for day-to-day use.** While the supervisor is
installed, launchd holds port 8902, so `./start.sh` waits ten seconds for the
port to come free and then refuses to run: "✗ something is still listening on
port 8902 after 10s (pid …)". Its advice to kill that pid doesn't apply here:
killing the process just makes launchd start it again seconds later. Use the
restart command below instead. `./start.sh` becomes the right command again once
you `launchctl bootout` the agent.

## Everyday commands

Run these from the repo folder, since `tail` and the installer are relative to it
(the installer prints the exact absolute log path when it finishes):

```
# restart it, after a git pull or to pick up .env changes
launchctl kickstart -k gui/$(id -u)/dev.crossband.server

# is it running, and as which pid?
launchctl print gui/$(id -u)/dev.crossband.server | grep -iE 'state|pid|program'

# follow the log
tail -f data/service.log

# stop supervising (and stop the service)
launchctl bootout gui/$(id -u)/dev.crossband.server

# start supervising again
ops/install-supervisor.sh
```

The log rotates at each boot once it passes 10MB; the previous
generation is kept beside it as `data/service.log.1`.

## How this interacts with deploys

Because launchd is the single owner of the process, a deploy must restart the
service **through** launchd rather than starting a second copy. Otherwise two
instances would race for the data-directory lock (the failure mode that
motivated this). So if you script deploys, finish the script with

```
launchctl kickstart -k gui/$(id -u)/dev.crossband.server
```

when the agent is loaded, and fall back to `./start.sh` when it isn't
(`kickstart` fails if there's no agent to kick). Either way you can't end up
with two instances by accident: `start.sh` checks the port first and refuses
to start a second copy, so the worst case is a deploy step that fails loudly.

## Stopping it, and why that used to hang

A stop is now bounded: SIGTERM ends the live-events streams immediately, gives
anything genuinely in flight (a chat round mid-generation, a live voice call)
up to **15 seconds** to finish, then exits regardless. Change the ceiling with
`CROSSBAND_SHUTDOWN_TIMEOUT_S` (or `"shutdown_timeout_s"` in `config.local.json`):
raise it if you would rather a long round always complete, lower it for a deploy
loop that values a fast, predictable stop.

Before that, a stop could hang **forever**, and the symptom pointed the wrong
way. Every open browser tab holds a `/api/events/stream` connection, which by
design never ends; uvicorn's graceful shutdown waits for open connections with
no timeout by default. So the socket closed, the app went unreachable, and the
process stayed alive holding the data-directory lock, and the next start
reported "Another instance is already running", naming a pid you had just
killed. If you are on an older build and see that: `kill -9` the pid it names.

Two smaller things follow from the same fix, and both are about the deploy path
rather than correctness. Startup now waits up to ten seconds for a lock still
held by a predecessor that is shutting down, so a restart a second too early
succeeds instead of failing; and when the lock really is held, the message says
whether the recorded pid is still alive, with the command to end it. A stopped
instance that a browser tab was watching will be reconnected by that tab on its
own, because the database is the catch-up buffer, so nothing is lost across a
restart.

## Turning up log verbosity temporarily

By default only `WARNING`-and-above from the app's own code reaches
`data/service.log`, and that includes the content-free Claude-chat cache
telemetry. Set `CROSSBAND_LOG_LEVEL=INFO` (env var, or `"log_level": "INFO"`
in `config.local.json`) for a deliberate sampling session, then unset it
again; it only changes what's written to the log, never what gets cached,
priced, or billed. See [docs/COST_TELEMETRY.md](COST_TELEMETRY.md) for the
full before/after workflow.

## Not on macOS?

The same idea applies with `systemd` on Linux: a unit with `Restart=always`
and `WantedBy=default.target`. A unit file isn't shipped here yet, but the
`ProgramArguments` in the plist template (`bash start.sh`, working directory =
the repo) map directly onto a systemd `ExecStart`/`WorkingDirectory` if you
need to write one.
