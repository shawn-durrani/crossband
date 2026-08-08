# Security

## Reporting

Report suspected vulnerabilities privately via GitHub: **Security →
Report a vulnerability** on this repository. Please do not open public
issues for security reports. Solo-maintained; acknowledgement usually
within a few days.

## The trust boundary

Who can reach the port is the outer boundary; a browser gate stands
inside it. The everyday unlock is a passkey (WebAuthn platform
authenticator; only the credential's public key is stored, per origin)
once one is enrolled, with a durable owner password (scrypt verifier in
the local store) as the fallback, and an out-of-band recovery secret
(`CROSSBAND_RECOVERY_SECRET`, or a per-start random value shown only
before enrolment) gating enrolment and reset. Sessions are opaque,
expiring, server-revocable ids in httpOnly SameSite=Strict cookies.
Passkey enrolment requires an already-unlocked session, never the lock
screen.

The gate is **enrolment-activated**: until an owner password is set,
the loopback API keeps its historical open posture (the startup banner
says so on every start), while a trusted non-loopback host is held to
the lock screen regardless. Once enrolled, every `/api` route outside
the login surface requires a session, loopback included. Stated
plainly: an install whose owner never enrols keeps the old
anything-on-loopback model.

- **Loopback by default.** The server binds `127.0.0.1` and refuses to
  bind anywhere else.
- **Tailnet only, if you widen it.** `tailscale serve` puts the UI on
  your own tailnet devices, and `CROSSBAND_TRUSTED_HOSTS` lists which
  non-loopback hosts may be served at all. An anonymous caller on a
  trusted host reaches the lock screen and nothing else. The procedure
  is manual and written out in
  [docs/REMOTE_ACCESS.md](docs/REMOTE_ACCESS.md); this repository ships
  no script for it. Never expose the port to the internet, and never
  use Tailscale Funnel.
- **Cross-site requests to `/api/*` are rejected** when the browser
  stamps them, and websocket routes check `Origin` as well as `Host`,
  since websockets are exempt from CORS and would otherwise let any page
  you visit drive the metered voice relays.
- **Host-level routes are loopback-only.** Adding, editing or testing an
  MCP server spawns a command, so those routes refuse a remote caller
  even on a trusted host. A remote caller also sees environment variable
  names without their values.

## Keys

Keys live in `.env`, which is chmod 600 on every start and gitignored.
The database stores the *name* of the environment variable, never a
value. Status and diagnostic endpoints return booleans. A summoned guest
starts with inherited provider variables blanked, so a subscription turn
cannot silently fall back onto your metered key.

**Blocking a guest from reading credential files is implement mode
only.** That mode's deny list names `Read(.env)`, `Read(**/.env)`,
`Read(**/.env.*)`, `Read(**/config.local.json)`, `Read(**/*.pem)` and
`Read(**/id_rsa*)`. Those rules bite only if `disallowed_tools` is
applied over the broad `Read` allow, which is Claude Code's documented
behaviour and not something this repository verifies: the guest tests
mock the SDK boundary, so they pin which rules are sent and never see a
read refused. **The default investigate mode carries no path rule at
all**: it denies whole tools (`Bash`, `Write`, `Edit` and the rest) and
leaves `Read` unrestricted, so nothing in that loadout stands between a
read-only guest and a credential file it can name. Neither mode
path-restricts `Grep` or `Glob` either. Treat the file rules as a
guardrail in one mode, not a guarantee in either. See
[docs/GUEST_PERMISSIONS.md](docs/GUEST_PERMISSIONS.md).

## What these controls do not do

- No rate limiting: a caller that reaches the port can hammer it.
- No isolation between OS users on a shared machine beyond file
  permissions.
- The guest tool allowlist bounds built-in tools only. Any MCP server
  you mount for a guest is available to it in full, in both modes. If
  one of those servers can write, so can the guest.
- Fetched pages are guarded against SSRF with per-redirect revalidation,
  but a page you ask a model to read is still untrusted text that a
  model will act on.

## Operational notes

- Treat `data/` as sensitive in its entirety: transcripts, attachments,
  snapshots and logs all live there.
- Redact before pasting logs into an issue.
