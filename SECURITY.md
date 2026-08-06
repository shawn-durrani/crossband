# Security

## Reporting

Report suspected vulnerabilities privately via GitHub: **Security →
Report a vulnerability** on this repository. Please do not open public
issues for security reports. Solo-maintained; acknowledgement usually
within a few days.

## The trust boundary

**Crossband has no authentication.** Anything that can reach the port
can use the app: read every transcript, spend your API keys, and summon
a coding agent. So the security model is entirely about who can reach
the port.

- **Loopback by default.** The server binds `127.0.0.1` and refuses to
  bind anywhere else.
- **Tailnet only, if you widen it.** `scripts/tailscale-serve.sh` puts
  the UI on your own tailnet devices; `MMC_TRUSTED_HOSTS` lists which
  non-loopback hosts may be served at all. Never expose the port to the
  internet, and never use Tailscale Funnel.
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
cannot silently fall back onto your metered key, and cannot read `.env`
or `config.local.json` even under a broad read permission.

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
