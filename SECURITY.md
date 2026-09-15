# Security

## Reporting

Report a suspected vulnerability privately through GitHub. Open the
Security tab on the repository and choose "Report a vulnerability".
Please don't open a public issue for a security report. One person
maintains the app, and you'll hear back within a few days.

## The trust boundary

Who can reach the port is the outer fence, and a browser gate stands
inside it. The gate knows three credentials, each with its own job.

- A passkey is the everyday unlock once one is enrolled. It's a
  [WebAuthn](https://developer.mozilla.org/en-US/docs/Web/API/Web_Authentication_API)
  platform authenticator, and the app stores only the credential's
  public key, per origin. Enrolling a passkey needs a session that's
  already unlocked, never the lock screen.
- An owner password is the fallback. It's held as a scrypt verifier in
  the local store.
- A recovery secret gates enrolment and reset. It's either
  `CROSSBAND_RECOVERY_SECRET` or a random value made at each start and
  shown only before enrolment.

A session is an opaque id that expires and that the server can revoke.
It travels in an httpOnly cookie with SameSite set to Strict.

The gate wakes up when the owner enrols a password. Until then the
loopback API stays open, and the startup banner says so on every start,
while a trusted host off loopback is held to the lock screen anyway.
Once a password is enrolled, every `/api` route outside the login
surface needs a session, loopback included. An install whose owner
never enrols stays open on loopback.

Machine tooling has no cookie jar, so it gets one exception. A
configured `ingest_token` (`CROSSBAND_INGEST_TOKEN` in `.env`) passes
the gate as a bearer on two routes, `POST /api/ingest` and
`POST /api/chats/{id}/notice`, which together are the machine
side channel. A missing or wrong bearer is refused, the token opens
nothing beyond those two path shapes, and an install that never
configures one keeps the session rule everywhere.

- Loopback by default. The server binds `127.0.0.1` and refuses to bind
  anywhere else.
- Tailnet only, if you widen it. `tailscale serve` puts the UI on your
  own tailnet devices, and `CROSSBAND_TRUSTED_HOSTS` lists which hosts
  off loopback may be served at all. An anonymous caller on a trusted
  host reaches the lock screen and nothing else. The steps are done by
  hand and written out in [docs/REMOTE_ACCESS.md](docs/REMOTE_ACCESS.md),
  and the repository ships no script for them. Never open the port to
  the internet, and never use Tailscale Funnel. The app checks for
  Funnel every few minutes and refuses to serve while it's on. A request
  on a trusted host without the identity header Tailscale adds for
  tailnet users is refused before the lock screen.
- Cross site requests to `/api/*` are refused when the browser marks
  them, and websocket routes check `Origin` as well as `Host`. The
  browser's cross origin rules skip websockets, and without that check
  any page you visit could drive the metered voice relays.
- Host level routes answer on loopback only. Adding, editing or testing
  an MCP server spawns a command, so those routes refuse a remote caller
  even on a trusted host. A remote caller also sees environment variable
  names without their values.

## How a request from the tailnet is checked

[docs/REMOTE_ACCESS.md](docs/REMOTE_ACCESS.md#where-a-request-goes)
has the short version. The rest is here.

The app checks where each request came from. A browser marks every
ordinary request with its origin, in a field called
[Sec-Fetch-Site](https://developer.mozilla.org/en-US/docs/Web/HTTP/Reference/Headers/Sec-Fetch-Site).
A request to an `/api/` route marked `cross-site` is refused, so a
page on another website that has learnt your tailnet name can't drive
the app from its own address. Only that one value is refused. A page
the browser calls `same-site`, meaning one served under another name on
your tailnet domain, gets through.

Voice runs over
[websockets](https://developer.mozilla.org/en-US/docs/Web/API/WebSockets_API),
connections that stay open in both directions, and that check never
sees them. The two voice relays, `/api/voice/tts` and
`/api/voice/stt-stream`, do their own checking, in
`backend/routers/voice.py`. They check the Host name against the same
list, and a second field, called
[Origin](https://developer.mozilla.org/en-US/docs/Web/HTTP/Reference/Headers/Origin),
which the browser fills with the address of the page that opened the
websocket. A page can't forge Origin. An Origin whose name isn't on the
list is refused, so a page on another site can't open a relay and spend
your ElevenLabs credit, even from a phone that's on your tailnet.

A caller that sends no Origin at all, meaning a script or `curl` and
never a browser, is let through, the same as over HTTP. For those the
tailnet is the whole fence, so treat the tailnet name as semi private.
Any device on it can reach every `/api/` route, and the lock screen is
the only lock behind it.

Your own scripts and the deploy watcher post into chats through
`/api/ingest` and the notice route, and each request carries
`ingest_token`. Once an owner password is set, every such script needs
the token, on the Mac itself included, because a script has no browser
session.

## Keys

Keys live in `.env`, which is set to mode 600 on every start and is
gitignored. The database stores the name of the environment variable,
never a value. Status and diagnostic endpoints return true or false. A
summoned guest starts with the inherited provider variables blanked, so
a subscription turn can't quietly fall back onto your metered key.

Blocking a guest from reading credential files happens in the two
modes with a shell, implement and run. Their deny lists name `Read(.env)`, `Read(**/.env)`,
`Read(**/.env.*)`, `Read(**/config.local.json)`, `Read(**/*.pem)` and
`Read(**/id_rsa*)`. Those rules bite only if `disallowed_tools` is
applied over the broad `Read` allow. That is Claude Code's documented
behaviour, and the repository doesn't verify it, because the guest
tests mock the SDK boundary, so they pin which rules are sent and never
see a read refused. The default investigate mode carries no path rule
at all. It denies whole tools, such as `Bash`, `Write` and `Edit`, and
leaves `Read` open, so nothing in that loadout stands between a read
only guest and a credential file it can name. No mode restricts
the paths `Grep` or `Glob` may touch. Treat the file rules as a
guardrail in two modes and a guarantee in none.
[docs/GUEST_PERMISSIONS.md](docs/GUEST_PERMISSIONS.md) has the detail.

## Outbound: the web the models reach

Models can search, fetch and render public web pages. Every URL a model
can influence leaves through a local vetting proxy that connects only
to public internet addresses, so a page can't reach this machine, the
sibling services or the local network. A model can only fetch a URL
that already appeared in the chat from a source that isn't a model.
That closes the channel where injected text asks the model to smuggle
data out inside a URL it composes. The rendering browser runs in a
separate process that holds no secrets.
[docs/WEB_RESEARCH.md](docs/WEB_RESEARCH.md) has the full model and its
limits.

## What these controls don't do

- No rate limiting. A caller that reaches the port can hammer it.
- No isolation between operating system users on a shared machine
  beyond file permissions.
- The guest tool allowlist bounds built in tools only. Any MCP server
  you mount for a guest is available to it in full, in every mode. If
  one of those servers can write, so can the guest.
- A fetched page arrives labelled untrusted, and the label informs the
  models without binding them. Page text is still input a model may act
  on.

## Operational notes

- Treat all of `data/` as sensitive. Transcripts, attachments,
  snapshots and logs all live there.
- Redact before you paste logs into an issue.

## Voice identity in room mode

- The stored anchor clips in `data/voice_anchors/` are the sensitive
  store, because they're short recordings of the people in the room.
  The directory is mode 700 and the files are mode 600. Forgetting a
  person deletes their clips from disk.
- Speaker embeddings are derived on the computer the app runs on, from
  those clips and from each utterance, and they're never sent anywhere.
  The matcher's model in `data/voice_models/` is fetched once from a
  pinned public URL and checked against its SHA-256 hash before use.
  After that first fetch, local identification makes no network calls.
  The model file carries no personal data and is never committed.
