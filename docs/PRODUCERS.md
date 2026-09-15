# Machine producers: talking to chats from your own tooling

Crossband stores what your tooling needs and reports what your tooling
says, and it never runs anything itself. A producer is a long-running
process on the same computer, or on your tailnet, that reads commands
out of chats and posts events back into them: a deploy watcher, a build
monitor, a parcel tracker, a scheduler. Crossband ships no producer,
and everything a producer can rely on is written here.

The one rule under every other rule is that the app gives no command a
meaning. A slash message is stored and nothing more. If nothing on your
computer reads it, nothing happens, and the app tells you so.

## Commands out

A message that starts with `/` is stored as a user message, runs no
round, and gets no reply. The models never see it. The suggestion chips
in the composer come from the `slash_commands` setting and carry no
behaviour of their own.

A producer reads commands however it likes. The contract covers trust
and acknowledgement.

- Trust user messages only. A user message comes from the app's own
  client, never from a model, so a model can't sign off on anything a
  producer does.
- Read each command at most once. Keep your own durable high-water
  mark, and move it only after a batch is done, so a crash replays a
  command instead of quietly eating it.
- Acknowledge what you read. Post to the notice route with
  `ack_command_id` set to the command message's id, for commands you
  act on and for commands you refuse, saying why. If nothing
  acknowledges a slash command within `slash_ack_timeout_s` (120
  seconds unless you change it), the app posts one system line saying
  nothing picked it up. A stopped producer then looks different from a
  working one.

## Notices and events in

Two routes carry everything inbound, and they share one credential.

`POST /api/chats/{chat_id}/notice` puts a status line into a chat. It
is stored as a system message, the models read it next round as
ground truth, and the app gives the text no meaning.

```json
{"text": "[14:02] ⏳ Deploy request received, checking crossband #61…",
 "ack_command_id": 9152}
```

`ack_command_id` is optional and names the user message the notice
answers. It must be a user message in that chat, or the request is
refused with a 400. Put the event's own time in the text, in the
`[HH:MM]` form, so a line that arrives late can't be mistaken for a
live one. Notices are best effort, and your own log is the record. If
deliveries fail, count them and open your next successful notice with
one gap line, such as "3 earlier notices failed to deliver". Never
replay missed events as if they were happening now.

`POST /api/ingest` puts a generic event into a chat, for things that
aren't about a command. The message is stored under your producer's
own speaker, `ext:<source>`, and the `dedupe_key` you choose makes the
post safe to repeat: the same source and key never land twice.
`priority` is `normal` or `high`, and `high` only adds a mark to the
line.

```json
{"source": "parcels", "target_chat": 12, "dedupe_key": "AU123456789",
 "priority": "normal",
 "payload": {"title": "Parcel out for delivery",
             "body": "Expected before 5pm.",
             "url": "https://example.com/track/AU123456789"}}
```

## The credential

Once an owner password is enrolled, every `/api` route needs a browser
session, and a producer has no cookie jar. The machine credential is
`ingest_token` (`CROSSBAND_INGEST_TOKEN` in `.env`), sent as
`Authorization: Bearer <token>` on the notice route and the ingest
route. Without it every post gets a 401 and your producer goes quiet.
The token opens nothing beyond those two routes.
[CONFIG.md](CONFIG.md#external-integrations) lists the setting.

## What a producer must never do

- Merge, deploy, restart, or act on Crossband's behalf inside the app.
  A producer acts on your computer and reports back. The app grants no
  authority to run anything, and no notice, however phrased, grants
  any.
- Post as any speaker but its own. A notice is always the system, and
  an ingested event is always `ext:<source>`. As long as producers stay
  on their own routes, no producer can forge a user's consent.
- Treat silence as success. Acknowledge what you read, and say what
  you refused.

## Keeping a producer alive

A producer is infrastructure, and it fails in known ways.

- Supervise it. Run it under launchd, or your platform's equivalent,
  so it restarts on a crash and starts at login. A producer that dies
  quietly makes every command look read and ignored.
- Probe health on a route that needs no session. `GET
  /api/auth/session` answers 200 without one. A gated route answers 401
  to a probe, and reads as down the moment the owner enrols a password.
- Ask before you restart the app. `GET /api/busy` answers
  `{"busy": <bool>, "reasons": [<fixed labels>]}` on loopback with no
  session. Busy means a round, a voice capture, a guest visit, a person
  sync, a benchmark, an import or a backup is in flight. Wait for
  false, then restart. [OPERATIONS.md](OPERATIONS.md#deploying-a-change)
  has the detail.
- Run one copy. Hold a lock that checks the holder is alive, so a
  re-run can't race a running copy.
- Keep the state beside the producer, never in a folder someone might
  tidy. The high-water mark is the record of what's been acted on.

The producer that ships Crossband's own changes lives in the operator's
private tooling, outside the app, because most installs should never
have an app that merges and restarts itself. Anything a producer needs
that isn't written here is a Crossband issue, not a private convention.
