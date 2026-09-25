# Adding an open model, local or hosted

Crossband isn't limited to Claude and GPT. The OpenAI API style talks
to any endpoint that speaks the same protocol, so you can add a local
model running on your own computer, which is private, free and needs
no key, or a hosted open model from a compatible provider, where one
key opens many models.

OpenAI's own open-weight models, the gpt-oss family, come through the
same two paths: locally through Ollama or LM Studio, or hosted through
Groq, Together, Fireworks or OpenRouter. There's no separate setup for
them. Pick a host and set the model id to the gpt-oss variant it
serves. Only the local `gpt-oss:20b` tag arrives with a known cost,
which decides how soon it can join every round, and
[Trial seats](#trial-seats-why-your-new-model-stays-quiet) says why.

Everything here happens on the Models page. Open it from the sidebar,
or from the "Open-source & local models" card in the first-run setup.
Add a model, set the API style to OpenAI / OpenAI-compatible, and the
form gains a Preset picker beside the Base URL field.

## The presets

| Preset | Base URL | Key needed? | Get a key |
|---|---|---|---|
| Ollama (local) | `http://localhost:11434/v1` | No | n/a |
| LM Studio (local) | `http://localhost:1234/v1` | No | n/a |
| OpenAI (default) | blank, the SDK's default | Yes | [platform.openai.com](https://platform.openai.com/api-keys) |
| Groq | `https://api.groq.com/openai/v1` | Yes | [console.groq.com/keys](https://console.groq.com/keys) |
| Together AI | `https://api.together.xyz/v1` | Yes | [api.together.ai/settings/api-keys](https://api.together.ai/settings/api-keys) |
| OpenRouter | `https://openrouter.ai/api/v1` | Yes | [openrouter.ai/keys](https://openrouter.ai/keys) |
| Fireworks | `https://api.fireworks.ai/inference/v1` | Yes | [fireworks.ai/account/api-keys](https://fireworks.ai/account/api-keys) |
| Custom | you fill it in | Depends | n/a |

Picking a preset fills the Base URL and the API key env var for you.
Both stay editable, and Custom lets you fill everything in by hand.

## Local, with no key

[Ollama](https://ollama.com/download) runs open models on your own
computer and serves them on a local port.

1. Install Ollama and start it. The app runs a local server on its own
   once installed.
2. Pull a model in a terminal.
   ```sh
   ollama pull llama3.1
   ```
3. On the Models page, press Add model. Give it a display name, say
   `Llama`. Set the API style to OpenAI / OpenAI-compatible and the
   Preset to Ollama (local), which sets the base URL to
   `http://localhost:11434/v1` and leaves the key blank. For Model /
   version, type the name you pulled, `llama3.1`. Typing it always
   works. The fetch available models button asks for an
   `OPENAI_API_KEY` even when the endpoint is local, so on a keyless
   computer it reports the missing key and lists nothing.
4. Add it and enable it. It's on the roster of your next chat straight
   away, with no `.env` change and no restart. It arrives as a Trial
   seat, though, so it won't speak in a normal round on its own.
   Address it, with `@llama` or by opening your message with "Llama,
   …", to hear from it.

LM Studio works the same way. Load a model, start its local server on
its default port, 1234, pick the LM Studio (local) preset, and set the
model id to a loaded model. No key.

A server that only speaks classic chat completions, such as
mlx_lm.server, LM Studio, vLLM or llama.cpp, works too. The app notices
the missing Responses API on the first reply and speaks chat
completions to that endpoint from then on. There's nothing to
configure.

A local server needs no key, but the OpenAI SDK insists on a non-empty
one. For chat replies the app passes a harmless placeholder to a
keyless local endpoint, so a blank key field works. The default OpenAI
endpoint still needs a real `OPENAI_API_KEY`, and a missing one there
is reported loudly. The fetch available models button doesn't pass the
placeholder yet, which is why typing the model name is the route that
works.

## Hosted, with a key

1. Pick a provider preset, Groq, Together, OpenRouter or Fireworks. The
   picker shows a Get a key link.
2. Create the key, then add it under the env var name the preset shows,
   such as `GROQ_API_KEY`. The setup wizard does this for OpenAI's own
   `OPENAI_API_KEY`, checking the key and writing it for you. For the
   others, add a line like `GROQ_API_KEY=…` to `.env` in the repo root
   and restart with `./start.sh`.
3. Back on the Models page, set Model / version to one the provider
   serves. Ids vary by host, and a few examples follow.
   - Groq: `llama-3.1-8b-instant`, `openai/gpt-oss-120b`
   - Together: `meta-llama/Llama-3.1-8B-Instruct-Turbo`,
     `openai/gpt-oss-20b`
   - OpenRouter: `meta-llama/llama-3.1-70b-instruct`,
     `openai/gpt-oss-120b`
   - Fireworks: `accounts/fireworks/models/llama-v3p1-8b-instruct`

   Press fetch available models to list what the endpoint offers, which
   needs the provider's key in `.env` already, or type the id from the
   provider's docs.

That connects the model. It's on your roster, and it sees the whole
shared transcript whenever it speaks. Whether it speaks unasked is a
separate matter, and
[Trial seats](#trial-seats-why-your-new-model-stays-quiet) covers it.

## Thinking models: turning the reasoning trace off

Qwen3 and models like it write a hidden reasoning block before the
visible answer, and you wait through all of it for the first word,
which is worst in voice. The Thinking on a local endpoint setting on
the seat turns it off.

There's no single field for this across servers, so pick the one your
server documents.

| choice | what it sends | who wants it |
|---|---|---|
| Default | nothing | anything already fast, or any hosted model |
| `chat_template_kwargs` | `chat_template_kwargs.enable_thinking = false` | vLLM, SGLang, mlx_lm |
| `enable_thinking` | top-level `enable_thinking = false` | Qwen-style API servers |
| `think` | `think = false` | Ollama |
| `/no_think` hint | appends `/no_think` to the system prompt | builds that honour no real switch |

The last one is a prompt trick and not a request field, so it's never
applied unless you choose it. The setting appears only on a seat with
its own base URL, because OpenAI's own endpoint has no such field. If
your server rejects the choice, the turn fails and names it, so you
never have to wonder whether it applied.

## Keeping a local model loaded

Ollama unloads a model from memory five minutes after its last request.
A quiet chat crosses that gap, and the next turn pays the reload before
the first word, which is seconds to minutes on a large model.

The Keep model loaded field on an Ollama seat names the window to hold
it instead. It takes an Ollama duration, such as `30m`, `1h` or `24h`,
or `-1` to hold the model until you say otherwise.

Crossband talks to Ollama through its OpenAI-compatible API, and that
API carries no keep-alive field at all. The window travels Ollama's
own route, a near-empty keep-alive call just before each of the seat's
requests, in the shape Ollama's docs use to unload a model. The seat's
real requests, tools and all, are unchanged. Leave the field empty and
Ollama's own five-minute unload applies. Point it at a non-Ollama
endpoint and the seat keeps speaking as it did, and the log names the
setting and says it didn't apply.

## A stronger model for one chat

Say "think harder" or "research more" in a chat, and each seat it moves
can also switch to a stronger model for the rest of that chat. "Think
harder" moves the seats it names, or every seat when it names none.
"Research more" moves every seat. A new chat starts back on the model
each seat's settings name.

The app finds the stronger model at the moment you ask, in four steps.

1. It asks the seat's own provider for the models your key can use,
   the same list the Models page shows.
2. It keeps the models the price card prices that fit the chat. The
   chat has to fit the model's context window. A chat carrying images
   or PDF files needs a model the provider says takes them. A seat
   thinking harder needs a model that takes a thinking level.
3. It runs one web search through your search engines, Tavily or
   Brave, and reads the results without opening a page. The utility
   model ranks the models from those results alone, and any model no
   result names is left out. Price never decides the order.
4. The seat moves only to a model ranked higher than the one it's on.

Before the switch takes effect, a line in the chat says what moved,
where the ranking came from, and what a turn costs each way. For
example:

> Claude moves from Claude Sonnet 5 to Claude Opus 5 for this chat, set
> by Alex. A web search ranked it the strongest Claude model the app
> can use here (example.org). A turn like Claude's last few here is
> about $0.02 now and about $0.06 on Claude Opus 5, rate-card
> estimates. Back to normal returns Claude to Claude Sonnet 5.

The figures price the seat's last few turns in this chat at both
rates. With no turns yet, the line gives both models' prices per
million tokens. A long chat also pays once to store its history on the
new model, and the line says what that costs each way. The running-cost
line names the model as well. The seat is told which model it's on and
who asked, and its name, persona and voice stay the same.

When the app can't find a stronger model, the seat stays where it is
and a line says why. That happens with no search engine set up, a
model list the app couldn't read, a model the price card can't price,
nothing that fits the chat, or a search that didn't settle it. A model
that ranks higher but has no price is named and never chosen.

Say "back to normal" and every seat returns to its configured model,
along with its thinking depth and research mode. Name one seat and
only that seat returns. If you change a seat's model in its settings,
your choice wins in every chat. If the provider refuses the stronger
model, the seat goes back by itself and the chat says so.

Local seats and seats on a custom endpoint never switch.
`model_step_up` in [CONFIG.md](CONFIG.md#models) turns the whole thing
off.

## Trial seats: why your new model stays quiet

Every model you add yourself starts as a Trial seat, and a trial seat
sits out normal rounds. It replies only when you address it, with
`@llama …` or by opening your message with its name ("Llama, what do
you think?"). When it does reply it sees the whole shared transcript,
like everyone else. Only the two built-in seats, Claude and GPT, ship
as Onboarded, full participants that answer every round.

The reason is cost. The app tracks how it knows what each model costs,
which it calls the cost provenance, and it won't put a model whose cost
it can't account for into every round on its own. Promotion is your
call, and nothing upgrades itself quietly. Each seat shows a Trial or
Onboarded badge on the Models page and in Connections, both from the
sidebar, with a plain explanation of what the state means.

## Making it a full participant

On Models, or on Connections, find the seat and press Promote to
Onboarded. The button appears only on a trial seat, and it's enabled
only when the app has a cost record for that exact model id. Without
one it stays visible but disabled and tells you why, and the API
refuses the same way, with a 409 that says there is no cost-provenance
record and the model stays a manual trial.

### Anything local can be promoted with no setup

A model served from your own computer with no API key, through the
Ollama and LM Studio presets or any `http://localhost…` or
`http://127.0.0.1…` base URL you type, gets its cost record on its
own: self-hosted, $0 marginal. Nothing is metering it, because nothing
leaves your computer. A declared zero is a fact, where an unknown is a
missing number. Run `ollama pull llama3.1`, add it with the Ollama
(local) preset, and Promote to Onboarded works at once, for whichever
model you pulled. The add form tells you so before you commit.

The rule is narrow, because a wrong $0 is worse than an unknown. It
applies only to loopback endpoints (`localhost`, `127.x.x.x`, `::1`)
with no API key. A model on another computer on your network, or
anything behind a key, is something you might be paying for, so it
keeps needing a real price.

### Hosted models stay on Trial until you price them

The Groq, Together, OpenRouter and Fireworks ids in the examples aren't
in the built-in price table, so as shipped they can't be promoted.
Nothing is broken. Mention the seat whenever you want to hear from it,
and that's what trial is for.

### Pricing a model in the app

The Model prices section on the Models page is the way in. Pick "A
published list price", enter the provider's per-million input and
output rates, the http(s) link you read them from, and the date they
were published or checked, then press Save rate. The app writes the
entry into `pricing` in `config.local.json` for you, keeping a `.bak`
of the previous file, and reads the file again on the next round, so
nothing needs restarting. Then press Promote to Onboarded.

The form holds you to things a text editor can't.

- A published estimate needs a checkable http(s) source and a real ISO
  date, so the figure can be checked again when it goes stale.
- Rates are bounded, so a misplaced decimal is refused as a typo and
  never stored as a cost basis.
- Aliases must be exact model ids, with no wildcards and no collision
  with another priced model.
- Only "a published list price" (`rate_card_estimate`) and "local /
  self-hosted, $0" (`self_hosted_zero_marginal`) can be declared by
  hand. The provider-reported and subscription-equivalent provenances
  are recorded per turn from what a provider returned, so no form may
  assert them.

Picking the self-hosted option declares the $0 for you and asks for a
short note in place of rates.

Editing `config.local.json` by hand still works, and it skips every
check in the list, so the figures are yours to stand behind. Entries
layer over the built-in table one model at a time, so pricing your own
model leaves Claude's and GPT's cards as they were. A local model is
declared with a zero marginal cost.

```json
{
  "pricing": {
    "llama3.1": {
      "input": 0.0, "output": 0.0,
      "provenance": "self_hosted_zero_marginal",
      "as_of": "2026-07-25",
      "source": "local (Ollama, self-hosted)"
    }
  }
}
```

For a hosted model, use the provider's published per-million input and
output rates with `"provenance": "rate_card_estimate"` and a real
`source` URL.

### How an id is matched

An exact match wins first, when the Model / version string on the seat
equals the key. Failing that, an entry that names your id in its
`aliases` list wins, which is how one card covers a second id you're
saying is priced the same. Failing that, a date-stamped or
build-stamped reissue matches its base entry, so `gpt-5.5` prices
`gpt-5.5-2026-01-15`. The stamp is at least four digits. A point
release like `claude-sonnet-5-1` is a new model, and it stays unpriced
until it has a row of its own or you price it. There's no broad family fallback. A model with a
new name stays unpriced until you price it, and it never inherits an
unrelated family's card.
