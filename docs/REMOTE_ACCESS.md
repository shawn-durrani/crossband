# Using the app from your phone

You can use the app from your phone, voice included, without putting
it on the internet and without your conversations passing through
anyone else's service. Out of the box it answers only on the computer
it runs on. The setup takes about ten minutes, and there's no script
for it, so the steps are by hand.

You need [Tailscale](https://tailscale.com), which makes a private
network between your own devices. That network is called a
[tailnet](https://tailscale.com/docs/concepts/tailnet), and only
devices signed in to your account are on it. One of its commands,
[`tailscale serve`](https://tailscale.com/docs/features/tailscale-serve),
gives something running on your Mac an
[HTTPS](https://developer.mozilla.org/en-US/docs/Glossary/HTTPS)
address that only your tailnet can reach. In short: run the app as
usual, have Tailscale serve it, tell the app to expect its new name,
and open that name on your phone.

## What you're agreeing to

Once the app is on your tailnet, the tailnet is its outer boundary,
and any device on it can reach the app's lock screen. Behind that
screen are your conversations, your API credit, and your repositories
if you've set up the coding guest. The lock screen asks for your owner
password, or for your
[passkey](https://passkeys.dev/docs/intro/what-are-passkeys/) once
you've added one. If you haven't set a password yet, a device on your
tailnet is asked to set one and can do nothing else. Setting one needs
the recovery secret from the Mac.

The lock screen is a second lock behind the tailnet, and it doesn't
make it safe to open the port any wider. Keep to these rules.

- Only your own devices go on that tailnet. If you wouldn't hand
  someone your unlocked laptop, don't add their device.
- Use `tailscale serve`, never `tailscale funnel`.
  [Funnel](https://tailscale.com/docs/features/tailscale-funnel) is the
  public version of the same command, and it would put the app on the
  open internet with no rate limiting, no audit log, and a login page
  facing the whole world. Check any time with `tailscale serve status`,
  which must say "tailnet only".

The app isn't built to face the internet, and having a login doesn't
change that. Past the tailnet, you're on your own.

## Why it has to be HTTPS

A browser will only give the microphone to a page it loaded over
HTTPS, or from the computer's own address, `127.0.0.1`. On plain HTTP,
at an address like `http://<mac-ip>:8902`, the mic is off and
everything else works. Tailscale serve gives the app a real HTTPS
address, which is what lets voice work from your phone. The same
command is what keeps the app off the internet, so one step does both.

## Setup

The steps use the
[`tailscale` command](https://tailscale.com/docs/reference/tailscale-cli).
On a Mac it lives inside the Tailscale app, at
`/Applications/Tailscale.app/Contents/MacOS/Tailscale`, and isn't on
your `PATH`, so run it by that full path.

1. Install Tailscale on your Mac and on your phone, and sign in to the
   same account on both. The free personal plan is enough.

2. On the Mac, check that both devices are on the tailnet.

   ```sh
   tailscale status
   ```

   You should see your Mac and your phone in the list.

3. Start the app on the Mac, as usual.

   ```sh
   ./start.sh
   ```

   It listens on `127.0.0.1:8902`, the Mac's own address, and stays
   there through every step. Tailscale will pass requests on to it.

4. Have Tailscale serve it over HTTPS.

   ```sh
   tailscale serve --bg https / http://127.0.0.1:8902
   ```

   Tailscale prints "Available within your tailnet:" and the address,
   something like `https://my-mac.my-tailnet.ts.net/`. That's your
   Mac's tailnet name. From now on Tailscale handles the HTTPS side at
   that name and hands each request to the app at `127.0.0.1`.

5. Tell the app to expect that name. Add this line to `.env`, with
   your own name in it, and restart the app.

   ```
   CROSSBAND_TRUSTED_HOSTS=my-mac.my-tailnet.ts.net
   ```

   Without it, the app refuses everything that arrives under the new
   name, and the phone sees one line:
   "This app serves localhost (and configured trusted hosts) only."
   The Mac's own address keeps working either way.

6. Check that it's tailnet only.

   ```sh
   tailscale serve status
   ```

   The line with your address must say "tailnet only".

7. On your phone, open the address.

   ```
   https://my-mac.my-tailnet.ts.net
   ```

   If you haven't set an owner password yet, the app asks you to set
   one now, and to prove it's you with the recovery secret. That secret
   is `CROSSBAND_RECOVERY_SECRET` in `.env` if you set one, or the
   random one the app printed in its startup output on the Mac. If a
   password is already set, you get the lock screen, and the password
   unlocks it.

Once you're in, allow the microphone when the browser asks, and voice
works as it does on the Mac. To unlock with Face ID or Touch ID next
time, add a passkey from the phone under Settings, then Passkeys. A
passkey belongs to the address it was made at, so one you added on the
Mac won't offer itself at the tailnet name.

## Where a request goes

Tailscale serve takes each request at the HTTPS address and passes it
to the app at `127.0.0.1`, so the app sees a connection from the Mac
itself. The request still carries the name it was sent to, in a field
every web request has, called
[Host](https://developer.mozilla.org/en-US/docs/Web/HTTP/Reference/Headers/Host).
The app checks that field on every request and refuses any name it
doesn't know. The check is there because a hostile website can point
its own name at `127.0.0.1`, and a browser would then treat that site
as your app. The Host field still names the other site, so the app
says no.

Only devices signed in to your tailnet can look up or reach the
tailnet name, so the tailnet is the outer fence. Inside it, the lock
screen is what asks who you are, with the owner password or a passkey.

```mermaid
flowchart LR
  P(["You, on your phone"])
  subgraph mac["Your Mac"]
    S("Tailscale serve")
    H{"Is the Host name<br/>one the app expects?"}
    R["Refused"]
    L["Lock screen"]
    A["Crossband: your chats,<br/>voice and tools"]
  end
  P -- "https://my-mac.my-tailnet.ts.net,<br/>over your tailnet" --> S
  S -- "passes it to 127.0.0.1:8902,<br/>Host name kept" --> H
  H -- "yes" --> L
  H -- "no" --> R
  L -- "owner password<br/>or passkey" --> A
  classDef node fill:#d4d4d8,stroke:#757575,color:#18181b
  classDef hero fill:#38bdf8,stroke:#0284c7,color:#18181b,stroke-width:2px
  classDef bad fill:#fca5a5,stroke:#dc2626,color:#18181b
  class P,S,H,L node
  class A hero
  class R bad
  style mac fill:transparent,stroke:#757575,color:#757575
```

The app also checks where each request came from, refuses a request
sent by a page on another website, and holds the two voice relays to
the same rule. Your own scripts post into chats with a token of their
own. [SECURITY.md](../SECURITY.md#how-a-request-from-the-tailnet-is-checked)
has the detail.

Nothing faces the internet, and no messaging provider such as Meta or
Twilio sits in the path. Your conversation goes only to the AI
providers you've set up.

## The other apps, on the same tailnet

Crossband is one of a family of apps that all run on your computer,
and each of them answers only on that computer out of the box. Membro,
the memory, and Spendglass, the spending view, go on the tailnet the
same way as Crossband: `tailscale serve` on a port of their own, never
Funnel, with their own lock screen behind it. Each app's own docs say
how, and its settings are its own. Start with
[Membro's front page](https://github.com/shawn-durrani/membro#readme) and
[Spendglass's front page](https://github.com/shawn-durrani/spendglass#readme).

None of it is needed for memory to work from your phone. Crossband
talks to Membro over the computer's own address, so recall, the profile
and saving facts all work from your phone whether or not Membro is on
the tailnet.
