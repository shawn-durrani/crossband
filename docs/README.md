# Crossband documentation: start here

Every document in the repo, what it's for, and the order to read them
in, split by what you're trying to do. Each page is written so that
you, or an AI assistant you paste it to, can do the task it covers.

## I want to run Crossband

1. [README.md](../README.md): what the app is, the quick start, and
   which keys to get. The setup wizard opens itself on first run and
   does the key setup with you.
2. [docs/MODELS.md](MODELS.md): every model you can run. The built-in
   seats, the one-click presets (Ollama, LM Studio, Groq, Together,
   OpenRouter, Fireworks), the path that needs no key, and how to price
   a model so it can join every round.
3. [docs/CONFIG.md](CONFIG.md): every setting, in one place. The four
   config layers, all keys with defaults, and
   `config.local.json.example` to copy from. You only need it once
   something in README.md says "configured in `config.local.json`".
4. [docs/VOICE_ID.md](VOICE_ID.md): how the app tells voices apart in
   room mode, where that falls short in a house it wasn't tuned in, and
   what to change. How room mode hears what you say, the English bias,
   every tuning knob, similar-sounding voices, and the scale bounds.
5. [docs/BENCHMARK.md](BENCHMARK.md): the benchmark on the Models page.
   The same scripted cases through the seats you pick, stage timings
   side by side, saved audio for your own ears, and what the numbers
   can't tell you.
6. [docs/WEB_RESEARCH.md](WEB_RESEARCH.md): the web tools and what
   contains them. What a hostile page can't do, the one-line Chromium
   install that turns on rendered viewing, and the limits.
7. [docs/REMOTE_ACCESS.md](REMOTE_ACCESS.md): the whole app, voice
   included, from your phone over Tailscale, with nothing open to the
   internet.
8. [docs/OPERATIONS.md](OPERATIONS.md): keeping a live install up. The
   launchd supervisor, the logs, restart on crash, surviving a reboot,
   and backups.
9. [docs/PRODUCERS.md](PRODUCERS.md): the machine side-channel. It's
   for your own deploy watcher or scheduler talking to chats. Slash
   commands out, notices and events in, the acknowledgement contract,
   the bearer credential, and what a producer must never do.

[Membro](https://github.com/shawn-durrani/membro) is the optional
memory service beside the app, and its own README.md covers its install.
Crossband works fully without it and lights up the memory features when
it appears.

## I want to let the AIs work on my code

- [What you can do, in README.md](../README.md#what-you-can-do): the
  short version. A summoned Claude Code guest works in its own
  worktree, and can only read unless you opt into implement mode.
- [docs/GUEST_PERMISSIONS.md](GUEST_PERMISSIONS.md): what a summoned
  guest may do, mode by mode. It says why the bounds live in code, and
  it's the page to read before you widen anything.

## I want to understand or change the code

1. [ARCHITECTURE.md](../ARCHITECTURE.md): the map. The projection
   trick, the round loop, the provider adapters, the guest, cost
   provenance, and the frontend's pure-module rule. It opens with which
   module owns what.
2. [CONTRIBUTING.md](../CONTRIBUTING.md): setup, the keyless test
   suites, the rules that matter, and how work lands.
3. [docs/TESTING.md](TESTING.md): what every suite guards, backend and
   frontend, and why both run without keys.
4. [docs/COST_TELEMETRY.md](COST_TELEMETRY.md): what your chats cost and
   where the money goes, how to read the cache log line, and how to
   check your own numbers before and after a change.
5. [docs/LATENCY.md](LATENCY.md): where the wait before a reply goes,
   how to measure it on your own install, and the two ways the numbers
   mislead.

### The eval harnesses, by kind

A guard runs on every change, needs no key, passes or fails, and pins a
rule so it can't drift. CI presses it, so it never needs a button.

- [eval_silence/README.md](../eval_silence/README.md): the guard for
  the speak-or-pass rule in group chats. Five hand-graded scenarios
  that hold the rule steady as the prompt text changes.

A measurement runs by hand when a decision needs numbers. It produces a
report to read, and it can cost money or touch your own data. Each
page says what it costs and what it touches.

- [eval_critic/README.md](../eval_critic/README.md): the critic
  question. Can a cheap critic catch a made-up memory fact in a draft
  reply? Made-up scenarios, a real model, API spend per run.
- [eval_attribution/README.md](../eval_attribution/README.md): the
  transcript shape question. Which shape lets a seat say who said
  what, including about itself? Made-up chats, a real model, API spend
  per run.
- [eval_recall/README.md](../eval_recall/README.md): whether the facts
  a round fetches from memory earn their wait. Your own turns and your
  own membro, so its corpus stays on the machine.
- [eval_intent/README.md](../eval_intent/README.md): the spoken
  instruction question. Does the one model call that reads every turn
  hear what you meant, and does it hear at least as much as a baseline
  of fixed phrase lists? Made-up turns, the utility model, API spend
  per run.

## Safety, security, history

- [SECURITY.md](../SECURITY.md): the trust boundary. Who can reach the
  port is the outer boundary, and an enrolment-activated browser gate
  (passkey first, owner password as the fallback) stands inside it. How
  to report a problem is there too.
- [CODE_OF_CONDUCT.md](../CODE_OF_CONDUCT.md): the short version and
  the standard it adopts.
- [CHANGELOG.md](../CHANGELOG.md): what changed for you, in plain
  English.
- [ACKNOWLEDGEMENTS.md](../ACKNOWLEDGEMENTS.md): who and what the app
  builds on.

## For an AI session working in the repo

[CLAUDE.md](../CLAUDE.md) is the entry point and loads on its own, and
it links here. The process rules live in
[CONTRIBUTING.md](../CONTRIBUTING.md), so follow them from there. Every
document in `docs/`, and each eval harness README.md, has an entry in
the index, and a test fails when one is missing.
