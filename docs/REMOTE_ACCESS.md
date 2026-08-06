# Reaching the app from your phone — privately

The app is localhost-only by design. To use it from your phone **without** exposing
it to the internet or routing your conversations through any third party, put your
Mac and phone on the same private [Tailscale](https://tailscale.com) network and let
Tailscale serve the app over HTTPS. Only your own devices can reach it; nothing is
public.

## What you are agreeing to

Doing this makes **your tailnet the app's authentication boundary.** There is no
login on this app — anything that can reach it can read every conversation, spend
your API credits, and (if the coding guest is configured) act on your
repositories. That is fine, and by design, *as long as only your own devices are
on the tailnet*.

Two rules follow, and they are the whole security model:

- **Only your devices on that tailnet.** If you would not hand someone your
  unlocked laptop, do not add their device.
- **`serve`, never `funnel`.** `tailscale funnel` is the public-internet form of
  the same command and would put this app on the open web, where it has no
  business being — no auth, no rate limiting, no audit. Verify at any time with
  `tailscale serve status`: it must say **"tailnet only"**.

This app is not built to be exposed publicly, and adding a login would not change
that. Beyond the tailnet, you are on your own.

## Why HTTPS is required (not optional)

Browsers block microphone access — and therefore all of **voice mode** — outside a
"secure context" (HTTPS or localhost). Over a plain `http://<mac-ip>:8902` the mic is
dead. Tailscale Serve gives you a real HTTPS origin, which is exactly what unlocks
voice remotely. The same move also provides the security boundary, so you get both
from one step.

## Setup (about 10 minutes)

1. **Install Tailscale** on your Mac and your phone; sign in to the same account on
   both. (Free personal tier is plenty.)
2. **Find your Mac's tailnet name** — `tailscale status` shows it, e.g.
   `my-mac.tailXXXX.ts.net`.
3. **Start the app** as usual on the Mac: `./start.sh` (stays bound to 127.0.0.1).
4. **Tell the app to trust that name** — add to `.env`:
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
6. **On your phone**, open `https://my-mac.tailXXXX.ts.net` — full chat, and voice
   once you grant the mic permission.

## How the security works

- Tailscale Serve proxies to `127.0.0.1`, so the connection the app sees is
  **loopback** — but the `Host` header carries your tailnet name, which is exactly
  why step 4 is needed.
- Only devices signed into **your** tailnet can resolve or reach the tailnet name at
  all — the VPN is the authentication boundary.
- The app still refuses any Host it doesn't recognise, so a stray request with a
  different Host is rejected even if it reaches the port.
- That same Host check is applied to the **voice websockets**, not just to plain
  HTTP — which is what makes voice work over the tailnet at all.
- Browsers stamp `Sec-Fetch-Site` on ordinary HTTP requests, and a request to
  `/api/*` marked exactly `cross-site` is refused. So a page on another site that
  somehow learns your tailnet name can't drive the API from its own origin. (Only
  that literal value is refused: a page the browser calls `same-site` — another
  name under the same tailnet domain — still gets through.)
- One gap to know about: that check is HTTP middleware, which never sees websocket
  traffic, so the two voice relays (`/api/voice/tts`, `/api/voice/stt-stream`) are
  guarded by the Host check alone. A page on another site, opened on a device that
  is already on your tailnet, could connect to one and spend ElevenLabs credit on
  your key. It gets none of your chat data that way, but the quota is real — so
  treat the tailnet name as semi-private and don't post it publicly.
- Once served over the tailnet, every `/api/*` route is reachable from **any**
  device on your tailnet, not just your phone — the tailnet is the whole fence.
  If a producer on another machine posts to `/api/ingest`, set `ingest_token`
  (see [SECURITY.md](../SECURITY.md)) so that route needs a bearer token.
- Nothing is exposed to the public internet, and no provider (Meta, Twilio, etc.)
  sits in the path — your conversation goes only to the AI providers you configured.

## Note on the memory service

The companion memory service (Membro, port 8901) is **loopback-only, and cannot be
served over the tailnet the same way.** It has the same kind of Host-header guard,
but no `MMC_TRUSTED_HOSTS` equivalent: any Host that isn't `127.0.0.1`, `localhost`
or `::1` is refused with 403 unless *every* request carries
`Authorization: Bearer <MEMORY_AUTH_TOKEN>`. That token path is real for API clients
(its MCP server, `curl`), but a browser can't attach that header when you navigate
to a page — so the admin UI would 403 on your phone before it ever loaded.

Leave it Mac-only. The chat app talks to it over loopback from the Mac, so memory
keeps working normally in a tailnet-served chat: recall, summary, search and fact
writes are all unaffected. (Membro also asks for an owner password on its admin UI
even on loopback, which this app has no equivalent of.)

If you genuinely want Membro's admin UI on your phone, that's a feature request
against Membro — it would need a trusted-hosts setting so the tailnet-proxied Host
is accepted and its own password login becomes the gate — not something
documentation can describe today.
