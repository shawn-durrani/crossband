# Crossband

Crossband is a group chat with several AI models in it at once.
Claude, GPT, a hosted open model through Groq or OpenRouter, or a
local one through Ollama or LM Studio: any of them can take a seat.
They all read the same transcript, see each other's messages, and can
agree or disagree. It runs on your own computer, and from there it can
listen and talk, remember what was said last week, and let more than
one person join in.

```
    you ──┐
 Claude ──┤
    GPT ──┼──▶  one transcript, on your computer  ──▶  search, pages, memory, voice
  local ──┘
```

The name comes from radio. A crossband repeater receives on one band
and sends on another. The app does the same between model APIs that
each only understand a conversation between two parties.

## Why it exists

Every chat app is you and one model. Crossband puts several in the
room and lets them talk to each other, which is where the interesting
disagreements happen. And because it runs at home, it can do things a
hosted chat can't: hear who's speaking, keep a memory across
conversations, and call a coding agent into your own repo.

## Get it running

You need Python 3.12 or newer, Node 20 or newer, and one model to talk
to. That's an Anthropic key, an OpenAI key, or a local model through
Ollama or LM Studio, which needs no key at all.

```sh
git clone https://github.com/shawn-durrani/crossband.git
cd crossband
./start.sh
```

Open http://127.0.0.1:8902. The first run takes a few minutes, because
`start.sh` creates the Python environment, installs the dependencies
and builds the web app. After that it only redoes a step when something
changed, and it won't start a second copy on the same port. If 8902 is
taken, set `CROSSBAND_PORT`.

Add your keys through the setup wizard in the app, or put them in
`.env`. The app checks each key works before it saves it, and never
shows a key back to you.

Voice needs one more key. Put your ElevenLabs key in `.env` as
`ELEVENLABS_API_KEY` and restart the app. Each model then speaks in its
own voice. Speech goes through the backend, so the key never reaches
the browser. If you don't add a key, voice is off and everything else
still works.

## What you can do

**Put several models in one conversation.** A provider's API only
knows two speakers, so the app shows each model its own past turns as
the assistant and everyone else's as the user, each labelled with who
said it. The models share a set of tools: web search, fetching a page,
viewing a page the way a browser renders it, Reddit and YouTube,
GitHub issues, and memory. Every tool result goes into the transcript,
where you and every model can see it.

**Let a model stay quiet.** If a model's whole reply is `[pass]`, the
app removes it before anyone sees or hears it, so a model with nothing
to add doesn't have to invent an angle. The first model to answer a
direct question can't pass.

**Talk, and hear the answers.** Voice works from your phone too, once
the app is on your own Tailscale network. Every voice the app has
learnt has a place on the Voices page, where you can listen to the
stored clips, fix a name, move a recording to the right person, or
forget someone.

**Have more than one person in the room.** Room mode is for when more
than one person is talking to the models. When someone speaks, the app
compares the voice with the voices it's learnt. That check happens on
the computer the app runs on, in well under a second. If it knows the
voice, it puts that person's name on the turn, so the models know who
said what. If it doesn't, it asks who's joined. A turn from one person
is transcribed once. Only a turn where two people talked over each
other gets a second transcription, to untangle who said what. The
small model that does the checking is about 38MB, downloaded once, and
then works offline. Set `CROSSBAND_VOICE_ID_ENABLED=false` to turn it
off.

**Call in a coding agent.** You can call Claude Code into the chat as
a guest. It joins for one turn, works in its own copy of your repo (a
git worktree), and by default it can only read. If you turn on
implement mode, it can branch, run the tests, push, and open a pull
request. It can never merge.

**Remember across conversations.** Memory is optional. With
[Membro](https://github.com/shawn-durrani/membro) running, the room
remembers from one conversation to the next. Without it, everything
else works and nothing is remembered.

**See what it costs.** Spend is counted in three columns that are
never added together: what was metered on an API key, what a
subscription covered and would have cost if metered, and unknown. A
model with no known price stays unpriced.

## What it isn't

Crossband is built for one household with one owner. There's one
login, not accounts for many people. It isn't a hosted service, and
there's nothing to sign up for. It needs a model to talk to, so bring
a key or run a local one. It's maintained by one person and was built for
that person's own use first. Issues and pull requests are welcome,
and response times vary.

## Configuration

`config.json` holds the defaults that ship with the repo.
`config.local.json` holds your own machine's settings and is gitignored.
An environment variable starting `CROSSBAND_` overrides both. Every
setting is listed in [docs/CONFIG.md](docs/CONFIG.md) with its default,
and a test fails if one is missing from that list. The descriptions
there are written by hand.

The app stopped reading the older `MMC_` prefix in v0.3. An `MMC_`
variable whose `CROSSBAND_` name is missing stops the app at startup,
with the exact rename printed, so nothing changes without you knowing.
See [Upgrading a pre-v0.2 install](#upgrading-a-pre-v02-install).

## Remote access

Out of the box, the app answers only on the computer it runs on. To use it from
your phone, put it on your tailnet, which is the private network
Tailscale makes between your own devices. Add your computer's tailnet
name to `CROSSBAND_TRUSTED_HOSTS`, restart the app, then run:

```sh
tailscale serve --bg https / http://127.0.0.1:8902
```

That gives the app an HTTPS address that only your devices can reach.
It has to be HTTPS, because a browser won't give the microphone to a
plain HTTP page. There's no script for this.
[docs/REMOTE_ACCESS.md](docs/REMOTE_ACCESS.md) writes out every step.

Never open the port to the internet, and never use Tailscale Funnel.
The app is built to be reached over your own tailnet and nowhere else.
Anyone who can reach the port still needs the owner password or a
passkey, but that's the second lock, not the first. Once you enrol a
passkey, the lock screen asks for it first and keeps the password as
the fallback. Read [SECURITY.md](SECURITY.md) before you widen anything.

## Documentation

[docs/README.md](docs/README.md) lists every document by what you're
trying to do. The short version:

- [ARCHITECTURE.md](ARCHITECTURE.md): the design decisions that are
  settled.
- [docs/CONFIG.md](docs/CONFIG.md): every setting, with its default.
- [docs/MODELS.md](docs/MODELS.md): adding a model, local ones included.
- [docs/GUEST_PERMISSIONS.md](docs/GUEST_PERMISSIONS.md): what a
  summoned coding agent may and may not do. Read it before you turn on
  implement mode.
- [docs/REMOTE_ACCESS.md](docs/REMOTE_ACCESS.md): the tailnet setup,
  step by step.
- [docs/OPERATIONS.md](docs/OPERATIONS.md): keeping it running.
- [docs/COST_TELEMETRY.md](docs/COST_TELEMETRY.md): reading your own
  cache and cost numbers.
- [docs/TESTING.md](docs/TESTING.md): what the test suites guarantee.

## Upgrading a pre-v0.2 install

The app was called Sideband while it was being built, and v0.2 renamed
the three things that still said so:

- environment variables, from `MMC_` to `CROSSBAND_`
- the launchd label, from `dev.sideband.server` to `dev.crossband.server`
- the guest diagnostics MCP server, from `sideband-diag` to
  `crossband-diag`

To move an install across, rename the `MMC_` lines in your `.env`. From
v0.3 the app refuses to start until you do, and prints each rename it
needs. If you run the app under the supervisor, the launchd job that
keeps it up, run `bash ops/install-supervisor.sh` again. It retires the
old launchd label itself.

One old name stays. The source tag the app sends to Membro is
`multi-model-chat`. <!-- secret-scan: allow: the legacy source tag is a documented fact -->
Membro uses that tag to tell which conversation a memory came from, so
renaming it would split every open chat's memory history in two, and
you'd gain nothing you can see. The tag only shows up in Membro's admin
facts view, like a database table name would.

## Licence

[MIT](LICENSE).
