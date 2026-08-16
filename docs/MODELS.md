# Add an open-source or local model

This app isn't limited to Claude and GPT. Because the "OpenAI" API style speaks
to **any** OpenAI-compatible endpoint, you can add:

- **Local models** running on your own machine: private, free, no API key.
- **Hosted open models** from a compatible provider: one key, many open models.

OpenAI's own open-weight models (the **gpt-oss** family) are served through
exactly these paths, locally via Ollama/LM Studio or hosted via Groq / Together
/ Fireworks / OpenRouter, so there's no separate setup for them: pick a host
below and set the model id to the gpt-oss variant it serves. (Connecting them
is uniform; only the local `gpt-oss:20b` tag arrives with a known cost, which
changes how soon it can join every round; see *Trial seats* below.)

Everything below happens on the **Models** page (open it from the sidebar, or
from the "Open-source & local models" card in the first-run setup). Add a model,
set the **API style** to *OpenAI / OpenAI-compatible*, and a **Preset** picker
appears above the Base URL field.

## The presets

| Preset | Base URL | Key needed? | Get a key |
|---|---|---|---|
| **Ollama (local)** | `http://localhost:11434/v1` | No | n/a |
| **LM Studio (local)** | `http://localhost:1234/v1` | No | n/a |
| **OpenAI (default)** | *(blank, the SDK's default)* | Yes | [platform.openai.com](https://platform.openai.com/api-keys) |
| **Groq** | `https://api.groq.com/openai/v1` | Yes | [console.groq.com/keys](https://console.groq.com/keys) |
| **Together AI** | `https://api.together.xyz/v1` | Yes | [api.together.ai/settings/api-keys](https://api.together.ai/settings/api-keys) |
| **OpenRouter** | `https://openrouter.ai/api/v1` | Yes | [openrouter.ai/keys](https://openrouter.ai/keys) |
| **Fireworks** | `https://api.fireworks.ai/inference/v1` | Yes | [fireworks.ai/account/api-keys](https://fireworks.ai/account/api-keys) |
| **Custom** | *(you fill it in)* | Depends | n/a |

Selecting a preset fills the **Base URL** and **API key env var** fields for
you. Both stay editable, and **Custom** lets you fill everything by hand.

## Local, no key: the Ollama quick path

1. **Install Ollama** from [ollama.com](https://ollama.com/download) and start it
   (the app runs a local server automatically once installed).
2. **Pull a model**, e.g. in a terminal:
   ```sh
   ollama pull llama3.1
   ```
3. On **Models → + Add model**:
   - **Display name**: whatever you like (e.g. `Llama`).
   - **API style**: *OpenAI / OpenAI-compatible*.
   - **Preset**: *Ollama (local)*, which sets the base URL to
     `http://localhost:11434/v1` and leaves the key blank (none needed).
   - **Model / version**: the name you pulled, e.g. `llama3.1`. Type it in;
     that always works. **fetch available models** currently asks for an
     `OPENAI_API_KEY` even when the endpoint is local, so on a keyless
     machine it reports that missing key instead of listing what Ollama has.
4. **Add** and enable it. It's on the roster of your next chat straight
   away, no `.env` change and no restart. It arrives as a **Trial** seat,
   though, so it won't speak in a normal round on its own: address it
   (`@llama`, or open your message with "Llama, …") to hear from it. See
   *Trial seats: why your new model stays quiet* below.

**LM Studio** works the same way: load a model, start its local server (default
port `1234`), pick the *LM Studio (local)* preset, and set the model id to a
loaded model. No key.

Servers that only implement classic chat completions (mlx_lm.server, LM
Studio, vLLM, llama.cpp) work too. The app notices a missing Responses API
on the first reply and speaks chat completions to that endpoint from then
on. Nothing to configure.

> Local servers need no authentication, but the OpenAI SDK insists on a
> non-empty key. For **chat replies** the app passes a harmless placeholder to
> keyless local endpoints, so a blank key field just works. (The default OpenAI
> endpoint still requires a real `OPENAI_API_KEY`, and a missing one there is
> reported loudly.) The **fetch available models** button doesn't do this yet:
> it asks for a key even for a local endpoint, which is why typing the model
> name is the reliable route above.

## Hosted open models, with a key

1. Pick a provider preset (Groq, Together, OpenRouter, Fireworks). The picker
   shows a **Get a key** link.
2. Create the key, then add it under the env-var name the preset shows
   (e.g. `GROQ_API_KEY`). Two easy ways:
   - **Setup wizard** for OpenAI itself (`OPENAI_API_KEY`), which validates and
     writes it for you; **or**
   - **`.env`** in the repo root for the others. Add a line like
     `GROQ_API_KEY=…` and restart with `./start.sh`.
3. Back on **Models**, set the **Model / version** to one this
   provider serves. Model ids vary by host; a few examples:
   - Groq: `llama-3.1-8b-instant`, `openai/gpt-oss-120b`
   - Together: `meta-llama/Llama-3.1-8B-Instruct-Turbo`, `openai/gpt-oss-20b`
   - OpenRouter: `meta-llama/llama-3.1-70b-instruct`, `openai/gpt-oss-120b`
   - Fireworks: `accounts/fireworks/models/llama-v3p1-8b-instruct`

   Press **fetch available models** to list what the endpoint offers (this
   needs the provider's key in `.env` already), or type the id from the
   provider's docs.

That's it for *connecting* the model: it's on your roster, and it sees the
whole shared transcript whenever it speaks. One thing still decides **when**
it speaks.

## Trial seats: why your new model stays quiet

Every model you add yourself starts as a **Trial** seat. That's a real
behaviour, not just a label: a trial seat **sits out normal rounds**. It
replies only when you address it: `@llama …`, or by opening your message
with its name ("Llama, what do you think?"). When it does reply it sees the
whole shared transcript, exactly like everyone else. Only the two built-in
seats (Claude and GPT) ship as **Onboarded**, full participants that answer
every round.

Why: the app tracks *how it knows what each model costs* (its cost
provenance), and it won't put a model whose cost it can't account for into
every round on its own. Promotion is your call; nothing upgrades itself
quietly.

Each seat shows a **Trial** or **Onboarded** badge on the **Models** page
and in **Connections** (both from the sidebar), with a
plain-English explanation of what the state means.

## Make it a full participant (Trial → Onboarded)

On **Models** (or **Connections**), find the seat and press
**Promote to Onboarded**. The button appears only on trial seats, and it's
enabled only when the app has a cost record for that exact model id. Without
one it stays visible but disabled and tells you why, and the API refuses the
same way, with `409 … no cost-provenance record … until then this model stays
a manual trial`.

### Anything local can be promoted, with no setup
 A model
served from your own machine with no API key (the Ollama and LM Studio
presets, or any `http://localhost…` / `http://127.0.0.1…` base URL you type)
gets its cost record automatically: **self-hosted, $0 marginal**. Nothing is
metering it, because nothing leaves your machine. (A declared zero is a fact,
not a missing number, and that's the whole point of the distinction.) So
`ollama pull llama3.1`, add it with the **Ollama (local)** preset, and
**Promote to Onboarded** works immediately: one click, no config editing, for
whichever model you pulled. The add form tells you this before you commit.

This is deliberately narrow, because a wrong $0 is worse than an honest
"unknown". It applies only to **loopback** endpoints (`localhost`,
`127.x.x.x`, `::1`) with **no API key**. A model on another machine on your
network, or anything behind a key, is something you might be paying for, so it
keeps needing a real price (below).

### Hosted models stay on Trial until you price them
 The
Groq / Together / OpenRouter / Fireworks ids listed above aren't in the
built-in price table, so as shipped they can't be promoted. Nothing is
broken: @mention the seat whenever you want to hear from it. Evaluating a
model that way is exactly what trial is for.

### Pricing a model in the app
 The **Model prices**
section on the **Models** page is the intended route. Pick "A published list
price", enter the provider's per-1M input and output rates, the http(s) link
you read them from, and the date they were published or checked, then **Save
rate**. The app writes the entry into `pricing` in `config.local.json` for you
(keeping a `.bak` of the previous file) and re-reads that file on the next
round, so nothing needs restarting. Then press **Promote to Onboarded**.

The form holds you to things a text editor cannot:

- a published estimate needs a checkable http(s) source and a real ISO date,
  so the figure can be re-checked when it goes stale;
- rates are bounded, so a misplaced decimal is refused as a typo rather than
  stored as a cost basis;
- aliases must be exact model ids, with no wildcards and no collision with
  another priced model;
- only "a published list price" (`rate_card_estimate`) and "local /
  self-hosted, $0" (`self_hosted_zero_marginal`) can be declared by hand. The
  provider-reported and subscription-equivalent provenances are recorded per
  turn from what a provider actually returned, so no form may assert them.

Picking the self-hosted option declares the **$0** for you and asks for a short
note instead of rates.

**Editing `config.local.json` by hand** still works, and it skips every check
above, so the figures are yours to stand behind. Entries layer over the
built-in table one model at a time, so writing your own model leaves Claude's
and GPT's cards exactly as they were. (An earlier build replaced the whole
table with whatever block it found, which meant pricing one model unpriced
every other one. That is fixed, and it is why the in-app editor exists.)

A local model is declared with a zero marginal cost:

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

For a hosted model, use the provider's published per-1M input and output rates
with `"provenance": "rate_card_estimate"` and a real `source` URL.

### How an id gets matched

Exact match first: the **Model / version** string
on the seat has to equal the key. Failing that, an entry that names your id in
its `aliases` list wins, which is how one card covers a second id you are
attesting is priced identically. Failing that, a date-stamped or build-stamped
reissue matches its base entry, so `gpt-5.5` prices `gpt-5.5-2026-01-15`. There
is no broad family fallback. A model with a genuinely new name stays unpriced
until you price it; it never inherits an unrelated family's card.
