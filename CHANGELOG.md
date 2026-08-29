# Changelog

House convention: one entry per user-visible change, newest first. Keep an
entry to a short paragraph; the issue holds the detail.

## Unreleased

New entries land as one file each in `changelog.d/`; a release folds
them in here, newest first.

- Facts mined from web-touched rounds now reach the review hold they were
  built for (#268). The round's web stamp was written to the message row,
  but the query behind the memory handoff never selected the column, so
  the stamp never left the app and the memory service treated every
  web-informed reply as ordinary transcript. The stamp now rides the
  handoff, and an end-to-end test walks a stamped row all the way to the
  wire so the seam cannot reopen quietly.

- Voice corrections can no longer be eaten by a bad sync pass (#273). A
  move or delete of a learned clip is replayed into the memory service so
  the correction cannot resurrect through a rebuild. When the service
  answered the clip lookup with an error (a stale token's 401, a 500),
  the replay read that as "already converged" and consumed the
  correction permanently. An unreadable lookup now leaves the correction
  pending and the next pass retries it. The person-sync watermark also
  advances properly once the service reports change stamps, so each pass
  asks only for what changed.

- A crash loop can no longer destroy your restore points (#275). Every
  startup snapshots the database, retention keeps the newest 14 copies,
  and the service manager restarts a crashing app every few seconds - so
  about two minutes of crash loop used to evict every pre-crash snapshot
  at exactly the moment one was needed. A snapshot byte-identical to the
  newest one is now discarded, so restarts that change nothing keep the
  history intact.

- A chat that carried more than 20 files on one message can reach memory
  again (#271). The memory service caps attachments per message at 20
  and rejects the whole handoff past that, so one bulk file drop wedged
  its chat: the handoff retried the same rejected payload on every
  leave, and nothing after that message was ever ingested. Over-limit
  messages now ride the wire in chunks the service accepts, and every
  file still lands.

- The Spend page now counts the background model work behind room mode
  and voice (#232). Five of the eight cheap-model calls the app makes
  were spending real money and appearing nowhere: the ones that read a
  turn for a spoken command, an introduction, a name correction, a
  reasoning-depth change, or a suspected wrong speaker. Only the title,
  summary and distillation calls were counted. Your utility total will
  step up as a result, and on a busy room-mode chat it may step up a
  lot. That is spend you were already paying, now visible.

- The running total in the chat header is now the same number the export
  picker shows (#231). It was worked out in the browser from the message
  list, so it could only ever see messages. Voice cost sat outside it and
  the background model work behind titles, summaries and room mode was
  missing entirely. The two screens could show different amounts for one
  chat. The header now reads the figure the server sends, so the token
  count and the dollar amount will both look higher, and the voice cost
  appears once rather than beside a total that excluded it. It refreshes
  when you open a chat and at the end of each round.

- Closed a gap in the voice relays. On an install that has a trusted
  host configured and has never enrolled an owner password, anything
  that could reach that host could open the two voice relays without
  passing the lock screen, and spend against your ElevenLabs key. Every
  other API route already refused those callers. The relays now refuse
  them too. Loopback is unchanged.

  Only the tailnet could reach it, and no released version is affected:
  the browser gate did not exist in 0.1.0, 0.1.1 or 0.2.0. If you have
  been running from main and have not enrolled a password, enrol one.
  That closes it on every surface and brings voice back on your phone.

- Chats holding a large PDF or several images reply faster (#229). The
  files were being re-read and re-encoded once for every model in the
  round, every round, which stalled everything else the app was doing
  at that moment. They are now read once and reused. On a three-model
  round over one 20MB PDF that is about half a second back per round.

- A rate card you save now prices the very next round (#230). Saved
  cards were reaching the pricing screen and nothing else, so rounds
  kept using whatever the app read at startup. Because the cost source
  is recorded at the moment a reply is written, those rows could not be
  corrected afterwards by fixing the card. Hand-edited prices in
  `config.local.json` reach a round now too.

- The Spend page no longer shows $0.00 for cache writes on a model you
  priced yourself (#230). It was reading the built-in price list rather
  than yours. Some historical cache-write figures will go up as a
  result, because they were understated rather than free.

- Documentation corrections (#233). The web research page said a
  rendered view shows text and not a screenshot, which stopped being
  true when the screenshot shipped, and it sat in the list of that
  feature's limits. Three documents said there is no component test
  infrastructure, while CI has been running a render smoke on every
  change. The setup guide's list of API keys left out Reddit and one of
  the two GitHub token names. The contributor guide gave one of the
  three frontend checks as though it were all of them.

- Startup now names a missing Reddit key like every other missing key
  (#233). It was the only capability the report could not see.

- A stored voice that outlives its human backing must earn your ear
  again (#221). Each automatically stored clip now records the match
  confidence it banked at. When rotation has replaced every recording a
  person stood behind, those scores decide: weak ones pause the voice
  from naming or seating anyone until you listen and confirm (people
  already seated in a live chat keep being identified), strong ones
  keep working with a note in Remembered voices. A voice with no human
  backing at all keeps its existing audition ask regardless of scores.

- The voice hygiene audit now also hears "not a voice" (#219). Each
  stored clip is checked for actual speech during the audit; a noise
  clip is set aside under its own reason, shown distinctly in
  Remembered voices ("not a voice" rather than "sounded like someone
  else"), and existing installs clean themselves on their next audit
  with no manual hunting.

- Non-speech audio can no longer be stored as anyone's voice sample
  (#218). The clip acceptance gate now includes the speech check, so no
  path into the voice store - accumulation, harvesting, cold start,
  introduction or correction - can bank noise, and loud static can no
  longer displace genuine quiet speech from a stored voice.

- A spoken self-introduction now overrules the voice guess on its own
  turn (#220). When someone introduces a remembered name but the turn's
  audio had already been confidently labelled as a different remembered
  person, the introduced name wins: the turn is relabelled, the wrong
  automated seat is withdrawn, the contested audio leaves the wrongly
  fed voice record, and a merge question tells you the two stored
  voices are colliding. Labels you corrected by hand, and seats placed
  by a person, are never unwound.

- Storing a voice sample now demands more certainty than naming a turn
  (#222). A borderline match keeps its label, but its audio is only
  added to the person's stored voice when the match also clears a new
  banking bar (`voice_id_banking_extra`, default 0.1 above the naming
  threshold). One wrong name can no longer feed the very voice record
  that produced it.

- Non-speech audio can no longer be named as a person (#217). Every
  utterance now passes a local speech check before the voice matcher may
  ask whose voice it is, so a static burst can no longer match a
  remembered voice, seat an absent person, bank itself as their sample,
  or start a cold-start bank. The identity pulse reports the new state
  as "not a voice".

- A cited source with nothing fetched behind it is flagged (#213). A
  "the docs say…" claim in a reply that ran no tools gets the same
  quiet chip as a misquote: the claim may still be right from training
  or memory, so the chip says unverified, never wrong. Any tool row in
  the reply skips the check, and it never retries or blocks.
  `CROSSBAND_CITATION_CHECK=false` turns it off.

- Misquotes of chat members now show up where they happen (#211). The
  attribution audit covers third-person claims too ("Claude said …",
  checked against Claude's own turns, with a they-to-I flip so faithful
  reports never flag), returns its findings to the round, and each
  flagged reply carries a quiet amber chip quoting the claim that has no
  word-for-word match. The chip is a prompt to check, never a misquote
  verdict: paraphrase and summary-folded history look the same. Audit
  log lines move to WARNING so a default deploy records them; they stay
  content-free, since the claim text lives only on your own message row.

- A reply that mostly restates what the chat already holds is dropped
  (#210). After a seat's reply completes, a word-overlap check compares
  it against the seat's own previous message and the replies already
  given this round. A near-copy is dropped and the seat re-runs once,
  told to add something new or pass; a copy on the retry is suppressed
  the way an insisted pass is, and a pass there is always accepted.
  Verbatim-leaning on purpose: quoting to answer, short agreements,
  repeat requests, tool-using replies and true paraphrase all stay
  untouched. Voice rounds only log, since the reply has been spoken by
  the time it can be judged. The scan runs in process after streaming
  ends, so reply latency is unchanged. `CROSSBAND_ECHO_GUARD=false`
  turns it off.

- Voice has one engine selector (#202). A new `voice_provider` setting
  names which engine serves speech. `auto` (the default) is exactly
  today's behaviour: ElevenLabs when its key is set, no voice when it is
  not. `elevenlabs` makes that choice explicit. `local` is reserved for
  a future local engine and, until one ships, selects nothing - even
  with a cloud key present, so choosing local can never quietly mean
  cloud. Every voice surface, the benchmark panel included, now resolves
  through this one choke point.

- Ollama seats can keep their model loaded between turns (#203). Ollama
  unloads a model five minutes after its last request, so a quiet chat
  costs a reload before the next first word. A new **Keep model loaded**
  setting on an Ollama seat names the window - `30m`, `1h`, `24h`, or `-1`
  for indefinitely - and Crossband now asks Ollama to hold the model in
  memory around each of that seat's requests. Left empty (the default)
  nothing changes, and Ollama's own five-minute unload still applies. It
  speaks Ollama's native keep-alive call, because the field cannot ride
  the OpenAI-compatible requests the seat already uses. See
  [docs/MODELS.md](docs/MODELS.md).

- Other apps' passkeys no longer crowd crossband's unlock sheet
  (#204). The fleet's apps share the browser's localhost passkey scope,
  so the sheet used to offer every app's key here. The gate now tells
  the browser exactly which keys are crossband's own. The trade, made
  deliberately: the lock screen now names those key ids to anyone who
  can reach it - an id is a serial number for the key, not a secret,
  and it cannot unlock anything.

- The page renderer is boxed in by the operating system too (#148). On
  macOS the render worker now runs inside an OS sandbox profile: no
  network except the vetting proxy's port, no writes outside its
  throwaway profile folder, and no reads of your data, `.env` or
  `~/.ssh`. It sits on top of the existing containment rather than
  replacing any of it, and a machine that refuses the profile renders
  exactly as before, saying so once. `scripts/sandbox_probe.py` proves
  the profile on a new machine.

- The seats can be raced on identical scripted cases (#94). A Benchmark
  panel on the Models page runs your chosen models through fixed cases
  and compares stage timings side by side: text replies, speech-to-text
  on a spoken fixture, each seat's own voice, and the full
  listen-think-speak pipeline. Synthetic by design - no microphone, no
  playback, results labelled as not-live-turn numbers - with generated
  audio saved for your own ears and deletable per run. See
  docs/BENCHMARK.md.

- "Just answer this one quickly" works as said (#105 slice 2). A spoken
  depth instruction scoped to the next reply ("think hard about just
  this next one") applies to exactly that reply and reverts by itself.
  It outranks the standing depth for that one call and posts no mode
  notice; the seat is told it is a one-off, so it never adopts it as a
  mode. A round that dies before the seat speaks keeps the override for
  the next round. Standing instructions behave exactly as before.

- A quiet voice can be turned UP (#163). Voice volume is now a relative
  weight (up to 300%): boosting one voice plays it at full volume and
  quietens the others proportionally, since a browser cannot push audio
  past full. With every seat at or below 100% nothing changes - the old
  turn-a-loud-voice-down behaviour is untouched. Set it once per voice
  on the Models page; the device volume sets the overall level.

- Rendered pages stop paying politeness per subresource, and a stuck
  render can no longer leak browsers (#153). The egress proxy paces
  connection BURSTS, not connections: a page's dozen same-host
  subresources no longer queue tens of seconds of spacing inside the
  render budget or delay the next fetch to that host. And the render
  worker now runs in its own process group, so the deadline kill takes
  Chromium and the Playwright driver with it instead of orphaning them
  at several hundred MB each.

- A rendered page load now has a whole-page transfer budget (#148,
  second half). The egress proxy grew a budgeted view listener: every
  connection a `view_page` render opens carries a per-view key, and all
  of them share one 30MB budget (`browse_page_budget_mb`), so a page
  cannot pull without bound through many small connections. Per-
  connection caps and the worker deadline stay as they were; plain
  fetches are untouched. The macOS worker sandbox half of #148 stays
  open - it needs the target machine.

- The view_page tool row carries a screenshot of what was viewed (#149).
  The rendered-viewing worker captures a viewport PNG; it lands in the
  ordinary attachment store keyed to the assistant message, and the tool
  chip shows it when expanded - so you can see exactly what the model
  saw, not just the extracted text. Capture is best-effort and bounded:
  a failed or oversized shot never costs the view its text. Feeding
  screenshots to vision models stays out of scope.

- A verification interstitial reads as one clean line (#150, first
  half). When a rendered page turns out to be a human-verification
  challenge ("Just a moment..."), view_page now says so in a single
  sentence pointing at the paste-into-chat path, instead of dumping the
  interstitial's own text into the transcript. None of the challenge's
  URLs can enter the seen-URL ledger. Completing challenges stays out
  of scope by design; the renderer-identity question stays open on the
  issue.

- Reasoning depth answers to your voice, per seat, per chat (#105 slice
  1). "Slow down and think harder", "take your time Claude", "quick
  answers from now on", "back to normal": a cheap prefilter plus the
  same utility-model confirmation room commands use turns natural
  phrasing into a persistent per-seat depth for that chat - no memorised
  incantation, no automatic de-escalation. A named seat moves alone; no
  name moves every seat. Each change lands a system notice stating the
  trade ("replies here will take longer"), and the seat itself is told
  its current depth so it answers honestly when asked. Model tier is
  deliberately not spoken-controllable yet.

- Promised deliverables arrive in the same reply, voice included (#80).
  A participant gets exactly one message per round, so "I'll put the
  full list in my next message" was a promise the app made impossible
  to keep - one morning produced four such promises and no list. Seats
  are now told that truth plainly. In voice, a reply can carry a spoken
  summary and then a full written deliverable below a [written] line:
  the written part lands in the transcript under a labelled divider and
  is never read aloud, so "too long to say" no longer means "defer".

- The Connections page shows repo access per surface (#86). One table
  says, per repo, whether the models' GitHub tools reach it (and which
  owner/repo that means) and whether the coding guest can open it in an
  isolated worktree, read-only or with writes. MCP servers are named as
  live-machine surfaces, kept apart from repo rows. The room asserted
  contradictory access facts for days because nothing displayed them.
  The guest's repo map now also re-reads from config per round, like the
  GitHub map already did, so edits apply without a restart.

- A guest resume whose session transcript is gone now retries fresh
  (#17). continue_last resumes Claude Code's previous visit by id; a
  cleaned ~/.claude or a new machine made that resume fail the whole
  guest turn. The visit now restarts fresh instead, saying so in its
  first line. Other launch failures still fail loudly.

- GitHub repo config edits apply without a restart (#24). The
  `github_repos` map was read once at boot, so after a repo rename the
  models' allowed-repo list and the integrations tile stayed stale
  until the server restarted. The map is now re-read from disk at each
  round start and status read, the same way pricing already reloads.

- The voice tick stops posing as a live verdict (#139). A green tick
  beside a person means their profile holds enough approved audio to
  recognise them; it said turns "are named automatically", which read
  as recognition of the turn being spoken even while that turn sat on
  "Identity pending". The tick's copy now scopes itself to the profile
  and points at each turn's own label for the live answer.

- A chosen participant voice now survives seat edits and voice starts
  (#161). Saving an unrelated seat edit re-sent the editor's stale
  blank voice selector, clearing an assignment made after the editor
  opened; the next voice start then re-rolled a different voice from
  the provider's floating list order. A save now carries the voice
  only when the selector actually changed, and the fallback pool is
  sorted, so identical accounts assign identical voices.

- A wedged seat can no longer hold a round hostage (#168). A seat that
  produces no stream event at all for three minutes - no text, no tool
  event, no liveness check-in - is errored and the round moves on, with
  any partial reply kept. Provider SDK defaults allowed about ten
  silent minutes, during which /send returned 409 and the voice gate
  stayed armed. Every stream event resets the bound, so slow healthy
  replies and long tool runs are never cut.

- Voice stalls now leave server-side evidence (#171). The client posts
  a content-free beacon when the round guard force-clears a dead round,
  or when speech strands for ten seconds behind a gated microphone. The
  server logs it at WARNING, so data/service.log shows the stall at the
  default log level, diagnosable from a phone. Round streams also time
  out after 90 seconds without bytes and recover through the normal
  reattach path, instead of pending forever on a half-open connection.

- Seats carry three conduct rules from the field (#172). Never claim a
  dispatch without its tool result in the same reply. Never promise a
  merge: Claude Code opens pull requests, and the user reviews and
  merges. Voice attribution heads are metadata, never a topic to raise
  unprompted or to return to after being told to drop it.

- A summoned Claude Code visit that cannot start now says so in the
  chat (#170). If Claude Code was switched off after the summons, or
  another visit was still running, the summons was dropped in silence
  while a seat had already promised the guest was coming. The drop now
  lands as a system notice, so nobody keeps waiting for it.

- The "two microphones live" banner stops accusing your own reconnect
  (#167). A phone reconnect registers a fresh capture session while the
  dead one lingers server-side for up to 40 seconds, so the banner
  counted the same microphone twice. The client now ends its own
  previous session the moment it is handed a new id, and dead sockets
  are reaped within about 20 seconds. A real second device still shows
  the banner exactly as before.

- Seats told to stand back while Claude Code works now pass properly
  (#169). The delegation note said to pass with a bare "…", a token
  nothing recognises, so obedient seats' ellipsis replies landed as
  real messages and voice rendered them as dead air. The note now
  names the real [pass] token, pinned to the engine's own constant.

- Voice says "Thinking…" while the models work, and a dead round can no
  longer trap the microphone (#165). The generation wait used to render
  as "Listening…", which invited speech the gated mic then discarded or
  turned into an accidental round-kill; a new working state names that
  wait honestly on every surface. A round silent for 60 seconds with
  nothing playing now force-clears the turn gate, ending voice and
  starting again begins from a clean gate, and Stop can abort the
  detached-round replay. A send refused because a round is still
  running is held and retried instead of dropped.

- Local thinking models can be told to skip the reasoning trace (#159).
  A Qwen3-family seat on an OpenAI-compatible server emitted a hidden
  reasoning block before its first visible token, and no setting reached
  it: reasoning effort only speaks Anthropic's and OpenAI's own dialects.
  Seats with their own base URL now carry a Thinking control on the
  Models page. It names the mechanism the server documents, so nothing
  is guessed from a model id, and the `/no_think` prompt hack is offered
  as an explicit last resort rather than injected. Default stays empty
  and sends no new field. An endpoint that rejects the choice fails the
  turn by name instead of silently ignoring it.

- The speaker model is named and its swap path documented (#154).
  VOICE_ID.md now says what the matcher runs (NVIDIA's TitaNet-Small
  via sherpa-onnx), that the thresholds are calibrated to it, and that
  stored voices survive a model swap because fingerprints are rebuilt
  from the kept clips. `GET /api/voice/health` reports the live model's
  file, hash prefix, and whether the built-in pin was overridden.

- Responses-route discovery survives the 404 arriving as a connection
  reset (#151). Servers that close without draining the request body
  reset transcript-sized requests every time, so the #144 fallback never
  engaged in real chats. A connection-level failure on an unclassified
  custom endpoint now earns a one-token chat ping: alive means classify
  and fall back, dead means the original error stays loud.

- The web research surface is documented (#145): docs/WEB_RESEARCH.md
  covers the tools, the containment model, the one-line install for
  rendered viewing, and the limits - including that human-verification
  challenges stay closed and pasting is the path for gated sources.
  SECURITY.md gains the outbound story, ARCHITECTURE.md maps the new
  modules, and the docs index and README feature line catch up.

- OpenAI-compatible servers without the Responses API now work (#144).
  The first reply on such a seat discovers the missing route and replays
  through classic chat completions in the same turn; later replies skip
  straight there. This covers mlx_lm.server, LM Studio, vLLM and
  llama.cpp, whose baseline is chat completions. The default OpenAI
  endpoint never falls back, so a real 404 there stays loud.

- Web content now carries its provenance everywhere it goes (#138,
  fourth slice). Fetched and rendered pages arrive marked as untrusted
  quoted data naming their domain, so every model in the room can see
  what it is reading. A round that read the web stamps its replies with
  the source domains; the stamp rides into the memory service (contract
  1.3), which holds facts born from those turns for your review - a
  public page cannot write memory by phrasing a sentence well, and an
  explicit save cannot slip past the same hold. On an older memory
  service the stamp is ignored (the previous baseline) and one log line
  says so.

- Models can view rendered pages (#138, third slice): a new `view_page`
  tool runs the page in a real browser and returns the visible text and
  its links, numbered - for app-style sites `fetch_page` reads as thin
  or empty. The render is contained: a separate worker process holding
  no keys or tokens, every request (subresources included) forced
  through the vetting egress proxy, WebRTC's proxy-bypass disabled,
  downloads refused, a fresh throwaway profile per view, and a hard
  deadline that kills the worker. Requires Playwright plus a one-time
  `playwright install chromium`; without them the tool is not offered
  and nothing else changes.

- A model can no longer invent the URL it fetches (#138, second slice).
  `fetch_page` and `transcribe_audio_url` now accept only URLs that
  already appeared in this chat from a non-model source: your messages,
  search results, links inside already-fetched pages, transcripts, text
  attachments, machine notices. This closes the channel where a hostile
  page instructs a model to smuggle private context out inside a URL it
  composes: models choose among URLs that exist, they never author one.
  A blocked fetch says so plainly and points at web_search.

- Every URL a model chooses to fetch now leaves the machine through a
  local vetting egress proxy (#138, first slice). The proxy resolves a
  host once, connects only to publicly routable addresses, and caps
  transfer size, so DNS rebinding between check and fetch reaches
  nothing local. `fetch_page` gains a decoded-size cap and reports the
  final URL after redirects; Reddit fetches refuse redirects that
  leave reddit.com.

- A live microphone anywhere is now visible everywhere (#134). If a
  capture session is running in another window or device, every
  surface shows a banner naming it - louder when it is a second mic in
  the same chat, which doubles every utterance - with an End button
  that stops it remotely: tracks off, no reconnect, ever. This closes
  the field case where voice was turned off in one window while an
  orphaned session elsewhere kept hearing the room.

- Replies no longer stall after a still-learning guest speaks (#133).
  The voice-identity background work (clip banking and the bank-hygiene
  audit) ran on the same thread pool that dispatches your messages, so
  a guest whose voice was still being learnt could starve the round and
  leave the room stuck "listening". That work now runs on its own
  bounded pool, and the audit runs at most once per 20-second window -
  deferred, never dropped.

- Guest turns now tell memory who spoke, not just a name string
  (membro#33 final slice, contract 1.2). A confidently attributed
  guest turn carries the person record, the matcher's real score, and
  how the identity was established (introduced, voice-matched,
  by-elimination, or your correction) - so facts a guest states link
  to the right person automatically when the identity is strong, and
  never on a weak guess. The matcher's per-turn score is now stamped
  into the label it has always written.

- Your voice corrections now reach the durable home (membro#33,
  slice 3). Moving a clip to the right person, deleting one, or
  merging duplicate people is recorded and replayed against membro on
  the next sync - so a fixed mis-attribution can never come back from
  backup, and a correction made while membro is down just waits for
  the next pass.

- The message list now has a render test (follow-through on today's
  blank-chat fix): the real chat view renders against realistic
  fixture messages in the test suite and CI, so a crash-on-render can
  never again reach production unseen. Verified to catch today's bug.

- Fixed: every existing chat rendered as a blank screen (a regression
  shipped earlier today with the discard affordance - two undefined
  names in the message list crashed the whole app the moment a chat
  with messages rendered). The frontend now lints for exactly this
  (no-undef, gating npm test and CI), so a free identifier can never
  reach a build again.

- Learned voices now have a durable home (membro#33, slice 2). A
  background sync uploads accepted clips to membro's person records
  (the first pass after this deploy backfills everyone), rebuilds
  people a fresh install doesn't hold, and obeys forget marks - one
  forget, in either app, deletes the stored audio in both. Membro
  down or unconfigured changes nothing: identification is local and
  never waits.

- Voices is a page of its own (#91), linked from the sidebar. It holds
  everything the app knows by voice and every control over it, with the
  room a full page gives it: listen to the stored clips, fix names and
  spellings, move a recording to the right person, confirm an auditioned
  bank, or forget someone. The models menu keeps a one-line
  pointer where the panel used to live. Voice sessions keep running
  while the page is open (the #69 strip covers it like any page).

- A model can honourably stay silent (#98). A reply of exactly [pass]
  is removed entirely - nothing shown, nothing spoken, nothing
  remembered - so "if you have nothing to add, pass" is finally a rule
  the app can mean. The guard: the first responder to your direct
  question, and any seat you addressed by name, may not pass - a pass
  there is refused and the seat answers on a single retry (and if it
  insists, it is suppressed and the other seats still run). Voice
  holds text-to-speech until a reply provably is not a pass, so a pass
  is never read aloud.

- Voice survives the models menu (#69). Opening any full page (models,
  connections, spend) no longer reads as the end of the call: capture
  and playback continue, and a compact strip keeps mute, end and the
  way back in reach at every width.
- A real mute at desktop width (#67). One unmistakable control in the
  voice dock: the mic track is disabled at the source, the models keep
  talking, and the muted state reads at a glance (amber, on every
  voice surface - dock, call screen and strip agree).
- Numbers stay digits (#66). The voice-mode brevity instruction was
  read as "spell numbers as words", which leaked into the persistent
  transcript ("issue sixty-three"). One rule now, no voice
  special-case: #61, PR 57, port 8902 - text-to-speech reads digits
  naturally, and the transcript must match what is spoken.
- Voice failures say why (#21). The realtime-transcription fallback
  banner names its cause (relay error, socket error) and logs it; a
  reply cut off mid-playback says so instead of printing the iPhone
  silent-switch checklist on a desktop Mac, and the generic playback
  failure names your platform's own hardware.

- A voice bank nobody vouched for must earn your ear (#83). The #65
  phantom banks were internally consistent and passed every automated
  check; their one common shape was crossing the sufficiency line with
  no introduction and no correction. Such a bank now asks you to listen
  and confirm in the remembered-voices panel, and a new one is paused -
  it can neither name nor seat anyone until you confirm (or someone
  introduces themselves, which vouches it). Banks that were already
  sufficient keep working while flagged.

- The discard banner now hands you the erase link (#111). When a
  discarded voice turn already reached memory, the banner links
  straight to Membro's danger zone, prefilled with that exact message
  and a preview - instead of telling you a copy exists and leaving you
  to find it. Same-host link, so it works from the phone.

- A captured voice turn can be discarded by its owner (#106). Live
  capture can transcribe audio never meant for the chat; hovering your
  own voice turn now offers a discard that removes it from the chat
  and from everything the models see from then on. The confirm copy
  states what cannot be undone: replies that already exist stay, and a
  copy that already reached memory stays there, since the ledger is
  append-only and membro-side deletion is its own owner surface. The
  audit line is content-free.

- No control with a mic icon can leave the microphone running (#108).
  The header chip that shapes reply style stops dressing as a voice
  kill switch: it is now "concise replies", icon-free, and says
  plainly that it does not touch the microphone. While a voice
  session is live, the header always shows a red End voice control
  that stops capture completely - tracks, socket, context, timers -
  without opening the dock. Found live: the owner clicked the
  mic-iconed chip, left the tab, and the browser kept recording.

- A monologue stays one message, and no utterance can send twice
  (#104, #85). The #60 noise caps were ending the whole TURN at 12/20
  seconds, chopping genuine long speech into separate messages - and
  those oversized commits regularly outran the flat 5s transcription
  patience, falling to the slow batch path (the ~57s stalls) whose
  result could then race a late realtime transcript into a doubled
  turn. Now a cap ends only the SEGMENT: the audio commits, the text
  buffers, and the turn stays open until a real silence gap - bounded
  at 60 seconds total, so the zero-gap noise case #60 was built for
  still always sends. Transcription patience scales with the audio
  committed, and a per-commit ledger - with the server stamping each
  transcript with the commit it answers - makes exactly one
  transcription win per utterance, whichever path delivers it.

- The lock screen tells the truth about passkeys, and passkeys get
  names (#87, #88). "Never enrolled" and "enrolled at a different
  address" both used to render as a silently missing passkey button,
  which reads as broken - the field case was days of "I thought I set
  it up" over a store that was simply empty. The lock screen now says
  which it is, naming the address that does hold one. And each passkey
  takes an owner-editable label ("MacBook Touch ID", "iPhone") with
  enrolled and last-used dates, so the mobile and desktop credential
  stop being indistinguishable twins.

- Learned voices are backed up (#33). voice_anchors/ was in no backup
  at all: losing the data directory forgot every learned voice while
  the chats survived. Every snapshot cycle now writes a
  voices-<stamp>.tar beside the database copy - owner-only, same
  retention, same optional mirror - and restoring is untarring it back
  into data/.

- Who-said-what survives compression, enforced (#22). The rolling
  summary that replaces old turns must keep the [Speaker] tags the fold
  demands: a summary that drops them is refused outright and the
  original turns stay in context (costlier, never wrong), so one agent
  can no longer read another's point back as its own after a fold. The
  live transcript each agent receives was already fully labelled; the
  fold was the one door provenance could quietly die through.

- An explicit introduction outranks an implicit voice match (#81). Two
  changes from the first real contamination. A confident voice match may
  re-identify an introduction silently only when the introduced name is
  a plausible spelling of the matched person's; otherwise the new person
  is seated under their own name and the resemblance becomes a merge
  question. And while anyone in the room is still unlearnt, the naming
  bar rises (`voice_id_pending_extra`), so a borderline resemblance to a
  remembered person defers instead of stealing the unlearnt person's
  turns. That is what lets their own voice bank its first clip. This deliberately reverses the fourteenth field test's silent
  collapse for dissimilar names; variant spellings keep collapsing
  silently.

- The machine-producer contract is documented (#59): docs/PRODUCERS.md
  is the complete public interface for a deploy watcher, scheduler or
  any local tooling that consumes slash commands and reports back -
  trust rules, the acknowledgement contract, notice conventions, the
  bearer credential, and the operational expectations the fleet's own
  outages taught. No behaviour change.

- Claude Code's findings are spoken when it returns (#64). The hand-back
  narrator's round finishes with the very message that announces it, so
  the voice client always arrived after the round was gone and the
  narration landed as silent text - the "you'll have to repeat yourself"
  gap from the first field days. The last round's buffer now stays
  discoverable until the next round starts, and a voice-active client
  replays it aloud exactly once. The replay can only ever be triggered
  by a participant's own message: notices, guest job output and external
  events can never resurrect an old round out loud.

- Fix a contaminated voice without deleting anything (#90). The first
  real audition found recordings of one person filed under another. The
  panel can now create a person by name, who starts unlearnt with no
  voice needed. It can move a recording to the person it actually
  belongs to, leaving the audio untouched, clearing a stale set-aside,
  and re-learning both voices from what they hold. And it can record
  another spelling of a name beside the display name, whether a
  misspelling worth keeping or the phonetic form. Owner-reassigned recordings say "reassigned by you" in the
  list. The AI-participant boundary (#77) holds at every new door.

- Hear what a remembered voice was built from (#68). Each person in the
  remembered-voices panel now lists their stored recordings - how each
  was earned in plain English, when, how long, whether the hygiene audit
  set it aside - with play and per-recording delete. Elimination-earned
  clips carry a "listen closely" flag, because that capture path is the
  one that has banked the wrong person before. Playback never changes
  anchor state; deleting a recording re-derives the voice from what
  remains, and deleting the last one leaves the person known but
  unlearnt. Recordings are served only to the authenticated owner.

- A stopped deploy watcher stops looking identical to a queued deploy
  (#58). Machine tooling now acknowledges each slash command it reads
  (the notice route gains `ack_command_id`); a command nobody acks
  within `slash_ack_timeout_s` (default 2 minutes, 0 = off) gets one
  system line saying nothing picked it up, and a restart inside the
  window re-arms the timer. Crossband still assigns no meaning to any
  command - it only learns whether SOMETHING read it.

- An AI participant can never be seated as a person in the room (#65).
  Agents are addressed by name in nearly every spoken sentence, and one
  introduction-shaped mishearing ("This is Claude...") could seat the
  agent on the roster - after which by-elimination learning banked HUMAN
  voices under the agent's name until the phantom was a remembered voice,
  and forgetting it just let the still-pending seat mint a fresh one.
  Participant names now get the same spelt-by-ear protection the owner's
  name has (variants like "Clyde" for "Claude" included), the seating
  chokepoint refuses the exact names whatever path asked, and
  `scripts/repair_participant_voices.py` repairs the phantom voices and
  seats an affected install already has.

- Deploy notices reach the chat again on a password-protected install
  (#62). The browser gate locked out the machine side-channel the moment
  a password was enrolled: the deploy watcher's progress notices - and
  any `/api/ingest` producer - got 401s, so a working deploy was
  indistinguishable from a dead one. The existing `ingest_token` is now
  the machine credential for both routes: a valid bearer passes the gate
  on exactly those two paths, an invalid or missing one is still
  rejected, and browser-session protection is unchanged everywhere else.

- Stable guest names on the memory wire, and the owner's name harder to
  mishear into a guest (#56). Two fixes from the first real multi-human
  sessions. A guest's name now crosses to the memory service as the one
  stable identity name, never the cosmetic preferred spelling, which
  stays a display concern. So renaming how a name is shown can no longer
  split one person's history into two guests in the ledger. And a
  transcription of the owner's own name up to two letters off is now
  recognised as the owner everywhere a name could join the roster,
  instead of minting a phantom guest.

- Auto (hands-free) voice turns no longer wait forever for silence that
  never comes (#60). Sustained background noise - road noise, wind, a
  fan - could keep the mic reading "still speaking" indefinitely, so a
  turn was never sent. A turn now bounds itself: past 12s it finalizes
  at the next real gap in the audio (even a brief one), and past 20s it
  finalizes unconditionally either way. Ordinary quiet-room pauses,
  barge-in, and push-to-talk are unchanged - the cap only ever fires
  after the normal silence timeout would already have.

- The room button in the voice tray is now an indicator (#28). The room
  switches itself on whenever it is needed - a voice it recognises or
  cannot place, a spoken introduction, a "group mode" command - so a
  button there suggested a press was required when it never was. The
  tray now simply shows the state: "room on · N", "listening" or
  "solo", each with a plain one-line explanation. The manual switches
  live on in the voice settings drawer ("switch on now" and "switch off
  for this chat") for first sessions, for when voice recognition is
  unavailable, and for anyone who cannot speak a command. Nothing
  changed underneath: the same durable switch, the same spoken
  commands.

- A remembered voice is now recognised the moment they speak, even in a
  room that is already listening for several people (#28). Before this,
  turning room mode on quietly narrowed recognition to the people
  already listed as present - so a household member the app knew
  perfectly well could talk all evening and never be named, because
  they were never on the list and could not get on it without being
  named. Recognition now checks everyone the app remembers, and a
  recognised person joins the room on their first turn. Everything
  stays on this device and nothing new runs while you are speaking.

- A brand-new guest can now be learnt while you are in the room (#28).
  Learning a new voice by elimination used to require them to be the
  only person present. Now it is enough that everyone else in the room
  is already recognisable: a clear turn that matches nobody known is
  put towards the one person still being learnt, labelled with their
  name and marked "learning this voice". The same cautions hold - never
  when voices overlap, never over a confident match, and poor audio is
  still rejected.

- An introduction under an unfamiliar spelling now sticks to the right
  person (#28). If someone the app remembers is introduced under a
  spelling too different for any spelling rule ("Samantha" for a
  remembered "Sam" is fine; a wholly different rendering was not), the
  app now checks the introduction's own voice. That includes when room
  mode is already on, where it previously judged leftover audio from
  before the room opened, and could even bank a guest's words as the
  owner's voice. That door is closed. The new spelling is kept on the
  person's record, so it is transcribed and resolved correctly from
  then on. And saying "Matteo is the spelling but it's pronounced
  Mateo" now records both forms on one person without overriding any
  name you have set yourself.

- The voice panel now answers "is it still learning?" and "why wasn't
  that turn named?" (#28). Each remembered voice shows whether it is
  still growing or refreshing in place, how many clips it holds and when
  it last learned something - numbers that previously existed only in a
  file on disk. And a turn that goes unnamed says which kind of unnamed:
  too short to judge, heard but not recognised, or too close between two
  known voices. Both are read from values the app already had, so
  nothing new runs while you are speaking.

- The AI seats now see who is speaking on the turn they are answering
  (#28). The voice check finished in time, but its label could only be
  written after your message row existed - and the reply starts rendering
  in that same instant, so the models read "identity pending" on the very
  turn your screen already showed as confirmed. They were reading a frozen
  copy while the browser got a live update. The finished result now rides
  the message as it is saved, so the name is there before anything reads
  it. Nothing waits on it: if the check has not finished, behaviour is
  exactly as before.

- A forgotten voice can now be re-learnt just by talking (#28). If you
  cleared your voice records, every way back in needed something you no
  longer had: being recognised needs stored clips, the introduction flow
  needs an introduction-shaped sentence, and correcting a name needs a
  name on the turn to correct. So the app deferred on every turn, learnt
  nothing, and told the AI seats "identity pending" over and over. Now,
  when room mode is on and you are the only person in the room, a turn
  the app cannot place is worked out by elimination - there is nobody
  else it could be - so it is banked towards learning your voice and the
  turn is labelled with your name, marked "learning this voice". After a
  few turns your voice is remembered again and ordinary recognition
  takes over. It is deliberately narrow: never with two people in the
  room, never when two voices overlap on one turn, and never with room
  mode off. The label is still one tap from being corrected, and no
  cloud call is involved.
- The voice dock is one panel with two rows, not four floating layers
  (#28). The status line used to be an ever-growing run of text that
  spilled out of its tray as soon as a second voice existed. It is now
  one chip per person: a green tick beside the name once their voice is
  remembered, "learning 4s" while it is still being learnt, and plain
  while nothing has been heard. The chips wrap, collapsing into a "+2"
  past four, with the live speed reading kept small on the right. The controls you touch per turn (microphone mode, the
  room toggle, send, stop, end) stay on one row; the pause and speed
  sliders, which you set once, move behind a settings button along with
  the matcher and mode readouts. Nothing was removed. On a phone the
  whole thing is a single line - "Listening · Alex ✓ +1" - that opens on
  a tap.
- Fix the cause of "identity pending" on the owner's own voice (#28).
  Two faults compounded. A tap-correction naming you could mint a SECOND
  person holding copies of your own voice clips, so the matcher found two
  perfect matches for one voice and honestly refused to choose - which
  read as "it never recognises me". And the hygiene audit that exists to
  catch exactly that duplication spent its one attempt per bank shape
  while the model was still warming up, so it never actually ran. Now: a
  correction that names you (by spelling OR by voice match) feeds your
  existing record instead of creating a twin, and the audit waits for the
  matcher to be ready before counting its attempt.

- Your own voice is now shown as recognised, not hidden (#28). The app
  has always quietly checked spoken turns against your remembered voice
  - it is how a solo chat stays solo - but it kept the result to
  itself, so the AI seats would say "identity pending" about the one
  person already identified. When the on-device check is confident it
  is you, the turn now carries your name with a "voice confirmed"
  marker: the seats see it in any mode (room on, ambient, or solo) and
  can answer "who is speaking?", and the turn shows a small tick as
  quiet reassurance. Nothing else changes - your voice still never
  switches room mode on, turns with no label render exactly as they
  always have, and uncertain turns stay honestly uncertain.
- Fix a voice-identification deadlock (#28). A two-part "enough voice
  learnt" rule shipped one build earlier also demanded a quota of short
  clips, which instantly marked every already-learnt voice as not-yet-
  learnt - and because an unlearnt voice is never matched, and only a
  match banks more voice, nothing could recover. Learning is back to the
  seconds bar; short-clip readiness is a separate progress hint, and a
  confident match on a longer turn quietly banks a short slice of its own
  audio so quick interjections become recognisable without any ceremony.
- The cloud no longer guesses who is speaking (#28). Voice
  identification is now local or honestly uncertain, full stop: the
  on-device matcher names a turn, or the turn stays unnamed - no cloud
  pass ever assigns a name, so the field-tested failure where a solo
  speaker was mis-named by the cloud fallback is structurally
  impossible. The only cloud transcription left in room mode runs when
  people talk over each other, to untangle who said what - so room
  mode's cloud voice spend is now just that, instead of a second listen
  per uncertain turn. If the local matcher is unavailable, turns simply
  go unnamed and room mode switches on only by hand (introduction,
  spoken command, or the toggle): degraded means manual, never wrong.
- Quick interjections become recognisable (#28). A voice now counts as
  learned only once its stored clips include a couple of SHORT ones
  (a word or two), kept in their own best-N pool so long sentences can
  no longer crowd them out - and the learning progress shown under
  Remembered voices says which half is still missing. Matching also
  accepts shorter utterances than before, where the audio is clearly
  voiced.
- Stored voices now audit themselves (#28). Whenever a voice bank
  changes, every clip is checked against every person. A clip that
  sounds more like someone else is set aside: kept on disk, shown as
  "clips set aside", and excluded from matching. Two people whose stored
  voices sit close together are flagged in the voice health strip
  ("Alex and Sam sound close - matching is stricter"), and the matcher
  automatically demands a wider winning margin between exactly those
  two. This is the guard against the field-tested
  cross-contamination that once let one person's turns be confidently
  labelled as another.
- Names now arrive with the words, not after them (#28). The app
  starts identifying a speaker the moment they pause - before the
  transcript is even final - so in the common case the name is attached
  by the time the turn appears, instead of a beat later. The head start
  is a content-free hint; nothing extra is recorded or sent anywhere,
  and a hint that turns out stale (the speaker kept going) is simply
  discarded.
- Voice identification's limits are now documented for strangers
  (docs/VOICE_ID.md): where the trigger phrases were grown, the English
  bias and what degrades, every tuning knob (threshold, margin, the
  two-part sufficiency bar - all configurable now), similar-sounding
  households, and the scale bounds (roster cap 6 by default,
  single-owner by design).
- Naming is law (#28). A name you set - by renaming a remembered voice
  or just saying it ("her name is spelt Samantha") - is now locked and
  wins everywhere a name appears: the labels on spoken turns, the "In
  the room" chip, what the AIs are told, what memory records, and the
  transcriber's spelling hints. No automatic step can change it back.
  Spelling variants of a name you already know are recognised as the
  same person instead of creating a duplicate with a blank voice
  memory; when the app is not sure, it asks ("Is Sal the same person as
  Sam?") instead of guessing. Renaming one person onto another's name
  offers to merge them - their stored voices combine, the best clips
  are kept, and both names keep working. Forgetting someone still
  sticks: the same name heard later starts fresh.
- The voice health strip (#28). The voice dock and the mobile call
  screen now show a compact readout of what voice identification is
  doing. Four things: whether the on-device matcher is ready, fetching
  its model, or falling back to the cloud; whether the room is on, solo,
  or ambient-listening; each remembered voice's learning progress; and
  how the last spoken turn was identified ("local · 227ms",
  "cloud · 1.9s"). The readout is fed by a new content-free endpoint - states,
  counts and milliseconds only, never names or words - and costs the
  live voice path nothing.
- The room-mode toggle is now durable and honest (#28). Switching it on
  acts exactly like saying "group mode": it sticks to the chat, puts you
  on the roster so voices are identified by the fast on-device matcher,
  and the models are told the true state. Previously the toggle only
  affected the current session, silently skipped the fast matcher, and
  the models would say room mode was off right after you turned it on.
- Room mode is now ambient - no trigger needed (#28). In a voice
  session, every turn gets a quiet on-device voice check: your own voice
  changes nothing, a remembered voice switches room mode on and is named
  automatically, and a clear voice the app cannot place asks who is
  speaking. Because known voices are identified locally at no extra cost,
  the second transcription (and its doubled voice spend) now runs only
  for overlapping speech or a voice the local matcher cannot place. Say
  "solo mode" to keep a session private - that preference sticks until
  you turn room mode back on.

- Room mode obeys spoken and typed commands (#28). Saying "group mode,
  please" (or "room mode on", "multi-user mode") now actually switches
  room mode on, and "solo mode" / "room mode off" / "just me now"
  switches it off - previously the AIs would verbally agree while the
  app did nothing. Detection rides the same background check as spoken
  introductions, so live voice latency is untouched, and a cheap
  confirmation step means talking ABOUT the mode ("is group mode on?")
  changes nothing. Switching off also clears the "In the room" chip and
  ends the doubled transcription in a live session. The AIs are now
  told the current room-mode state and roster each round, so "is group
  mode on?" gets a true answer instead of a guess.
- Room mode identifies known voices locally, part 2 (#28). When more
  than one person is in the room, a known voice is now recognised
  on-device in a fraction of a second - so the models see the right name
  on the turn itself, instead of waiting a second or two for the second
  listen and often reading the turn as you in the meantime. The common
  case where one known person is speaking no longer needs a second
  transcription at all; the second listen still runs whenever two voices
  are present or the match is uncertain, so crosstalk and unknown-voice
  handling are unchanged. The small speaker model (~38MB) is fetched once
  to the data directory, verified, and then runs fully offline - nothing
  about a voice ever leaves the machine. Purely additive: with the model
  or its library absent, or with `CROSSBAND_VOICE_ID_ENABLED=false`, room
  mode behaves exactly as before. Live voice latency is untouched -
  everything here happens on the background pass.
- Room mode label latency, part 1 (#28, night test 4). The name check
  runs a second or two behind the words, so the first AI to answer used
  to read a fresh spoken turn before its name existed and assume it was
  you. A turn whose name is still on the way now reads honestly as
  "Identity pending (in the room)" - the models are told the name is
  still being worked out and not to guess - and any AI speaking later in
  the same round picks up the resolved name the moment it lands. The
  check itself lost its avoidable delays too: labels attach the instant
  the second listen returns instead of on a half-second polling step,
  spend bookkeeping happens after the labels rather than in front of
  them, and the remembered-voice samples that preface every check are
  cached instead of re-read from disk on every spoken turn. Live voice
  latency is untouched - everything here happens on the background pass.
- Room mode arming fixes from the third field test (#28). Two spoken
  triggers that silently did nothing now work. A handover with no name
  ("I'm going to hand over to a guest") switches room mode on and asks
  who the guest is, never inventing a name. A guest introducing
  themselves ("I'm Samantha, Alex's wife, also known as Sam") switches
  it on and adds them under their proper name, keeping the short form
  ("also known as", "call me") as their preferred display name. Every
  introduction check now leaves one plain log line saying what it
  decided, so a silent failure can no longer be mistaken for the check
  not running. And remembered voices can now switch room mode on by
  themselves. When a session starts with room mode off in a household
  with remembered voices, the first couple of spoken turns are also
  transcribed a second time, listening for a known voice or a second
  speaker. Those sessions therefore transcribe their first couple of
  utterances twice. A recognised voice switches room mode on and joins
  the roster with no introduction needed.
- Room mode, phase 4 (#28): honesty about people talking over each
  other, and three fixes from the second field test. When two voices
  land in one spoken turn, the turn now says so: "Two voices at once -
  some words may be missing". On a single microphone the quieter
  person's overlapped words are often simply gone. The models see the
  same note, so they can ask the quieter person to repeat, and such
  turns are never saved to memory as any one person's words. When
  the two voices took turns cleanly rather than overlapping, a
  best-effort split shows who said which words (shown only when the
  second listen agrees with the live transcript; your message text is
  never rewritten). The models are also told plainly what a voice label
  is - text produced by a second listen, not audio they can hear - so a
  seat can no longer claim it "can tell from the voice". Introductions
  stop storing relationship words as names: "this is me, Sam, Shawn's
  wife" now yields a person named Sam, never "Wife" - a
  relationship-only introduction matches a remembered person if the
  sentence names one, and otherwise the app just asks who it is. A
  still-learning voice now shows its progress (seconds heard toward the
  bar) in the remembered-voices panel and the room chip, so waiting is
  an informed choice. And room-mode sessions now capture the mic with
  the browser's single-voice noise tuning switched off (it can muffle
  the second speaker); solo sessions are untouched, and each session's
  capture profile is logged so the experiment can be judged on field
  data.
- Room mode, phase 3 (#28): attribution lands everywhere it matters.
  Voice labels now attach to exactly the turn that was spoken (a quick
  interjection can no longer be labelled onto a neighbouring turn), and
  the models finally SEE the labels. A turn confidently matched to a
  named person reads as that person "(in the room)" in every model's
  view of the chat. An uncertain turn reads as an unidentified speaker:
  never guessed, never silently credited to you. Names stop
  drifting: your own name always comes from the `user_name` setting
  (never from what the transcriber heard), each remembered voice gets
  an editable preferred spelling (Models -> Remembered voices, pencil
  icon), and everyone's names are fed to the live transcriber so it
  spells them consistently. When a chat is saved to memory, guests'
  statements are recorded as that guest - and membro quarantines them
  for review - while anything the app is not sure about is marked as an
  unknown guest rather than being filed as a fact about you.
- Room mode, phase 2 (#28): voices get names, and the introduction is
  the trigger. Saying "my wife Alex is here" (no toggle needed) flips
  room mode on for the chat, adds Alex to the roster, and starts
  learning her voice; "Alex has left" removes her. Voices are
  remembered: a few seconds of each person's clear speech is stored on
  this computer (owner-only files, deletable from Models -> Remembered
  voices with a Forget button that deletes the audio), so a known
  person is recognised in later sessions with no introduction. Turns
  are labelled with names, and below the learning bar a label stays
  marked uncertain. An unrecognised voice raises a "someone new is
  speaking - who?" prompt you answer by just saying the name. A
  background cross-check can flag a turn whose content reads like
  someone else, but it never changes the label. Tap the name on a turn
  to correct it, which also teaches the right voice. An "In the room" chip shows who
  the app is telling apart, which is also the cue that multi-voice
  processing (double transcription spend) is on. The live conversation
  still waits on none of this. Roster size is capped (default 6,
  `CROSSBAND_ROOM_ROSTER_MAX`).
- Room mode, phase 1 (#28): a per-session toggle in the voice controls
  for when more than one person is in the room. While on, each spoken
  turn also goes through a second, diarising transcription pass in the
  background; turns where another voice appears get small unnamed
  "Voice 1" / "Voice 2" chips a moment later. The live conversation is
  untouched - nothing waits on the pass, and with the toggle off the
  voice pipeline is exactly what it was. Stated plainly: telling voices
  apart transcribes the audio twice, so voice minutes roughly double
  while the toggle is on. Labels are best effort for now; naming the
  voices is the next phase.
- A browser gate (#25): an owner password (scrypt verifier, recovery
  secret for enrolment/reset, opaque revocable sessions) now protects
  the UI and API. Enrolment-activated: nothing changes until you set a
  password from the app; after that, every surface asks for it and a
  tailnet caller only ever sees the lock screen. Set
  `CROSSBAND_RECOVERY_SECRET` in `.env` so enrolment and reset work
  without terminal access.
- Passkey unlock (#25): enrol a Touch ID / Face ID passkey from the
  Integrations console and the lock screen offers it first, password
  one click behind. Passkeys are per web address (`localhost` and the
  tailnet name enrol separately; an IP address cannot hold one, so
  `127.0.0.1` keeps the password form), and the tailnet passkey syncs
  to your other devices via your keychain.

## v0.2.0 (2026-08-07)

The rename release: the Sideband-era identifiers are retired.

**Migrating an existing install:** rename the `MMC_` lines in your
`.env` to `CROSSBAND_` (until v0.3 the old names still work and every
use logs the exact rename at startup), and rerun
`bash ops/install-supervisor.sh` if you use the supervisor - it boots
out the old `dev.sideband.server` label and installs
`dev.crossband.server` itself.

- Environment variables moved from `MMC_*` to `CROSSBAND_*`, with a
  one-release fallback and per-variable startup warnings.
- The launchd label is `dev.crossband.server`; the installer migrates a
  pre-rename install automatically.
- The guest diagnostics MCP is `crossband-diag`, so the tool id in the
  tool-activity strip reads `mcp__crossband-diag__get_diagnostic`.
- The app's loggers moved from `mmc.*` to `crossband.*`; if you grep
  `service.log` by logger name, update the pattern.
- Guest worktrees and temporary git refs now use crossband-guest
  namespaces. Old-namespace leftovers are still reclaimed until v0.3:
  stale worktree directories are swept (registered with git or not) and
  orphaned refs/mmc-guest refs are deleted at the next visit to that
  repo. One limit: a guest session begun before the upgrade cannot be
  resumed with continue_last, because its transcript is keyed by the
  old working directory; summon a fresh visit instead.
- The source tag sent to Membro stays `multi-model-chat`, now
  documented as deliberately permanent: Membro keys conversation
  identity on it, and renaming it would fork every open chat's memory
  history.

## v0.1.1 (2026-08-07)

- A chat whose only seat is a trial (unverified-cost) model can now be
  spoken to: explicit addressing reaches trial seats even when it
  covers the whole roster, in both typed and spoken forms. Previously
  such a chat completed rounds with no speakers and no error.
- Source comments and config examples now tell the truth about the
  guest's two modes, and every code_mcp example carries the required
  env key (copying the old example produced a mount that died at
  spawn with nothing telling you why).
- UI and error copy now uses plain punctuation instead of em-dashes,
  matching the docs. Placeholder glyphs (a bare em-dash standing for an
  empty value) are unchanged.
- `./start.sh` now serves the app on a first run. It creates `.env` from
  the example, warns about whichever keys are missing, and starts the
  server, instead of exiting and asking you to rerun it after the venv
  and the frontend build were already done.

## v0.1.0 (2026-08-06)

First public release, under the name Crossband.

- Group chat with several AI models in one shared transcript: each
  provider sees the conversation projected into its own two-party
  format, so seats can address and disagree with each other.
- Detached rounds: generation runs as a background task writing to a
  replayable buffer, so a dropped connection never kills a reply.
- Prompt-cache-aware prompt assembly, with stable content before the
  breakpoint and per-round content after it.
- Shared tools every seat can call: web search, page and Reddit and
  YouTube fetch, GitHub issues and PRs, memory recall and save, and
  self-diagnostics. Results persist and are visible to every
  participant.
- Claude Code as a summonable guest: its own git worktree, read-only by
  default, opt-in implement mode that branches, tests, pushes and opens
  a PR but can never merge.
- Voice in and out through the backend, so the key stays server-side,
  with content-free latency instrumentation.
- Cost accounting with provenance: metered, subscription-equivalent and
  unknown are never summed, and pricing fails closed rather than
  guessing a rate for an unknown model.
- Optional memory through the Membro HTTP contract; absent, the app
  works and forgets.
- Local models via Ollama and LM Studio presets, with no key required.
