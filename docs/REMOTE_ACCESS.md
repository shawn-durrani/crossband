# Reaching the app from your phone, privately

The app is localhost-only by design. To use it from your phone **without** exposing
it to the internet or routing your conversations through any third party, put your
Mac and phone on the same private [Tailscale](https://tailscale.com) network and let
Tailscale serve the app over HTTPS. Only your own devices can reach it; nothing is
public.

## What you are agreeing to

Doing this makes **your tailnet the app's authentication boundary.** There is no
login on this app, so anything that can reach it can read every conversation, spend
your API credits, and (if the coding guest is configured) act on your
repositories. That is fine, and by design, *as long as only your own devices are
on the tailnet*.

Two rules follow, and they are the whole security model:

- **Only your devices on that tailnet.** If you would not hand someone your
  unlocked laptop, do not add their device.
- **`serve`, never `funnel`.** `tailscale funnel` is the public-internet form of
  the same command and would put this app on the open web, where it has no
  business being: no auth, no rate limiting, no audit. Verify at any time with
  `tailscale serve status`: it must say **"tailnet only"**.

This app is not built to be exposed publicly, and adding a login would not change
that. Beyond the tailnet, you are on your own.

## Why HTTPS is required (not optional)

Browsers block microphone access, and therefore all of **voice mode**, outside a
"secure context" (HTTPS or localhost). Over a plain `http://<mac-ip>:8902` the mic is
dead. Tailscale Serve gives you a real HTTPS origin, which is exactly what unlocks
voice remotely. The same move also provides the security boundary, so you get both
from one step.

## Setup (about 10 minutes)

1. **Install Tailscale** on your Mac and your phone; sign in to the same account on
   both. (Free personal tier is plenty.)
2. **Find your Mac's tailnet name.** `tailscale status` shows it, e.g.
   `my-mac.tailXXXX.ts.net`.
3. **Start the app** as usual on the Mac: `./start.sh` (stays bound to 127.0.0.1).
4. **Tell the app to trust that name.** Add to `.env`:
   ```
   MMC_TRUSTED_HOSTS=my-mac.tailXXXX.ts.net
   ```
   and restart. (Without this the app's DNS-rebinding guard would refuse the proxied
   request. Loopback keeps working regardless.)
5. **Serve it over HTTPS via Tailscale:**
   ```
   tailscale serve --bg https / http://127.0.0.1:8902
   ```
   Tailscale terminates TLS at `https://my-mac.tailXXXX.ts.net` and forwards to
   loopback.
6. **On your phone**, open `https://my-mac.tailXXXX.ts.net` for full chat, and
   voice once you grant the mic permission.

## How the security works

- Tailscale Serve proxies to `127.0.0.1`, so the connection the app sees is
  **loopback**. The `Host` header still carries your tailnet name, which is exactly
  why step 4 is needed.
- Only devices signed into **your** tailnet can resolve or reach the tailnet name at
  all, so the VPN is the authentication boundary.
- The app still refuses any Host it doesn't recognise, so a stray request with a
  different Host is rejected even if it reaches the port.
- That same Host check is applied to the **voice websockets**, not just to plain
  HTTP, which is what makes voice work over the tailnet at all. They check
  `Origin` as well; see two bullets down.
- Browsers stamp `Sec-Fetch-Site` on ordinary HTTP requests, and a request to
  `/api/*` marked exactly `cross-site` is refused. So a page on another site that
  somehow learns your tailnet name can't drive the API from its own origin. (Only
  that literal value is refused: a page the browser calls `same-site`, meaning
  another name under the same tailnet domain, still gets through.)
- That `Sec-Fetch-Site` check is HTTP middleware, which never sees websocket
  traffic, so the two voice relays (`/api/voice/tts`, `/api/voice/stt-stream`)
  carry their own copy of *both* checks in `backend/routers/voice.py`: the Host
  allowlist **and** an `Origin` check. A browser sets `Origin` itself and page
  JS cannot forge it, so an `Origin` whose host is not itself on the allowlist
  is refused. A page on another site, even opened on a device already on your
  tailnet, therefore cannot open a relay and spend ElevenLabs credit on your
  key. (Earlier versions of this app checked Host alone, and this page used to
  describe that gap. It is closed.) Clients that send no `Origin` at all, which
  means non-browser callers such as `curl` or a script, are still allowed,
  matching the HTTP middleware's posture: the tailnet remains the fence for
  those, so keep treating the tailnet name as semi-private.
- Once served over the tailnet, every `/api/*` route is reachable from **any**
  device on your tailnet, not just your phone, because the tailnet is the whole
  fence.
  If a producer on another machine posts to `/api/ingest`, set `ingest_token`
  (see [SECURITY.md](../SECURITY.md)) so that route needs a bearer token.
- Nothing is exposed to the public internet, and no provider (Meta, Twilio, etc.)
  sits in the path. Your conversation goes only to the AI providers you configured.

## Note on the memory service

The companion memory service
([Membro](https://github.com/shawn-durrani/membro), port 8901) binds loopback by
default, and it now documents its own supported path onto a tailnet. This page
previously said that was impossible and would need a feature request. It is
not: Membro grew both halves, and its own docs are the reference.

- **`MEMORY_TRUSTED_HOSTS`** is Membro's equivalent of `MMC_TRUSTED_HOSTS`: a
  comma-separated list of the non-loopback hosts allowed to reach its login
  surface. An anonymous tailnet caller reaches the lock screen and nothing
  else.
- **`MEMORY_TAILSCALE_SERVE=1`** in Membro's own `.env` makes its `start.sh`
  run `scripts/tailscale-serve.sh` at every startup, publishing it over
  `tailscale serve` (never `funnel`) on its own HTTPS port,
  `MEMORY_TAILSCALE_PORT`, default `8443`. Membro takes a dedicated port rather
  than a path under the tailnet root deliberately, because its admin UI links
  absolute paths: under a `/membro` prefix those would resolve against whatever
  is served at the root, which on a machine also serving Crossband is this app.
- **On macOS** the Tailscale CLI is usually installed but not on `PATH`. Point
  Membro at the one inside the app bundle with `MEMORY_TAILSCALE_BIN`.
- **Its login is real.** Membro asks for an owner password on the admin UI even
  on loopback, which this app has no equivalent of, so the password rather than
  the tailnet is what gates it. Serving it more widely does not change its
  loopback binding or its credentials.

Read Membro's own `SECURITY.md` and `docs/TUNING.md` before turning any of that
on; the settings above are its, not Crossband's, and it owns their behaviour.

None of it is required here. Crossband talks to Membro over loopback from the
same Mac, so in a tailnet-served chat recall, summary, search and fact writes
all keep working whether or not you also serve Membro on the tailnet.
