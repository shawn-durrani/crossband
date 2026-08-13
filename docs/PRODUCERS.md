# Machine producers: the tooling side-channel contract

Crossband has a deliberate split (#59): the app stores what machine tooling
needs and reports what machine tooling says, but it never executes anything
itself. A "producer" is any long-running process on the same machine (or your
tailnet) that consumes commands from chats and reports events back into them:
a deploy watcher, a build monitor, a parcel tracker, a scheduler. Crossband
ships no producer; this document is the complete interface one builds
against.

The principle behind every rule here: **core assigns no meaning to any
command.** A slash message is inert storage. If nothing on your machine
consumes it, nothing happens - and since #58, Crossband at least tells you
that.

## The outbound half: slash commands

A user message starting with `/` is persisted with `speaker='user'`, runs no
round, and gets no model reply. Models never see it. Suggestion chips for
the composer come from the `slash_commands` config key and carry no
behaviour.

A producer consumes commands however it likes - the contract is only about
trust and acknowledgement:

- **Trust `speaker='user'` rows only.** They can only originate from the
  owner's own client, so a model cannot forge consent for anything a
  producer does.
- **Process each command at most once.** Keep your own durable high-water
  mark, and advance it only AFTER a batch is processed, so a crash replays
  consent instead of silently eating it.
- **Acknowledge what you read (#58).** Post to the notice route (below) with
  `ack_command_id` set to the command message's id - for commands you act
  on AND commands you deliberately ignore (say why). If nothing acks a
  slash command within `slash_ack_timeout_s` (default 120s), Crossband
  posts one system line saying nothing picked it up, so a stopped producer
  stops being indistinguishable from a working one.

## The inbound half: notices and events

Two routes, one credential.

**`POST /api/chats/{chat_id}/notice`** - a status line INTO a chat.
Persists as `speaker='system'`; models see it next round as ground truth;
core assigns no meaning to the text.

```json
{"text": "[14:02] ⏳ Deploy request received — checking crossband #61…",
 "ack_command_id": 9152}
```

- `ack_command_id` (optional) names the user message this notice consumes;
  it must be a user message in THIS chat or the request is refused (400).
- Put the EVENT's own time in the text (the `[HH:MM]` convention) so a
  delayed line can never be mistaken for a live one (#74).
- Notices are best-effort by design: your log is your source of truth. If
  deliveries fail, count them and open your next successful notice with one
  gap line ("N earlier notices failed to deliver") - never replay missed
  events as if they were happening now.

**`POST /api/ingest`** - a generic event into a chat (see
[CONFIG.md](CONFIG.md) for `ingest_token`). Producer-namespaced speaker
(`ext:<source>`), producer-chosen `dedupe_key` for idempotency. Use this for
events that are not about a command; use `/notice` to narrate work a chat
asked for.

## Authentication

Once an owner password is enrolled, every `/api` route needs a browser
session - and producers have no cookie jar. The machine side-channel
credential is `ingest_token` (env: `CROSSBAND_INGEST_TOKEN`): send it as
`Authorization: Bearer <token>` on exactly the two routes above (#62).
Without it, every post 401s and your producer goes silently mute - the
failure mode that cost an evening before this contract existed. The token
buys nothing beyond those two routes.

## What a producer must never do

- Merge, deploy, restart, or otherwise act on Crossband's behalf inside the
  app: a producer acts on YOUR machine and reports back. Crossband never
  grants execution authority - and no notice, however phrased, grants any.
- Post as any speaker other than its own: `/notice` is always `system`,
  `/ingest` is always `ext:<source>`. Consent forgery is structurally
  impossible as long as producers stay on their own routes.
- Treat silence as success. Ack what you read; say what you refused.

## Operational expectations

A producer is infrastructure, and the fleet's own history says exactly where
it rots (a nine-day silent outage, workbench#1-#4, taught every line here):

- **Supervise it.** Run under launchd (or your platform's equivalent) with
  restart-on-crash and start-at-login. A producer that dies silently makes
  every command look consumed-and-ignored.
- **Probe health against auth-exempt routes only.** `GET /api/auth/session`
  answers 200 without a session by design; a gated route 401s an
  unauthenticated probe and reads as "down" the moment the owner enrols.
- **One instance.** Hold a liveness-checked lock so a re-run cannot race a
  running copy.
- **Keep state beside the producer**, not in a folder anyone might tidy:
  the high-water mark IS pending consent.

The reference implementation of all of this lives in the operator's private
tooling repo, deliberately outside Crossband (most installs must never have
an app that merges and restarts itself). This contract is the whole public
interface: anything a producer needs that is not documented here is a
Crossband issue, not a private convention.
