"""The round loop: an async generator that streams each responder's reply over
SSE, persisting as it goes.

Rounds run DETACHED (see rounds.py): a background task drives this generator
to completion whatever happens to the HTTP connection, so a network drop never
kills a reply. The CancelledError/GeneratorExit handling below now fires only
on a DELIBERATE stop - the abort endpoint (barge-in / Stop button) cancelling
the round task - and persists the partial reply with a "[cut off by <User>]"
marker so the transcript stays honest and the models know to drop that thread.
"""

import asyncio
import base64
import json
import logging
import re
import time

from . import attachments as att_mod
from . import chat_memory, db, guest
from . import memory_client as memory_client_mod
from . import providers
from . import provenance as prov
from . import tools as tools_mod
from . import voice_trace
from .config import compute_cost, provenance_for

log = logging.getLogger("crossband.engine")


def sse(payload):
    return f"data: {json.dumps(payload)}\n\n"


def slugify(name):
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "model"


async def _timed_ms(coro):
    """Await `coro` and return (result, elapsed_ms), clocked from inside the
    task so the duration is the coroutine's OWN runtime. Two tasks wrapped
    this way and awaited in either order each report their own duration -
    unlike clocking around the awaits, which charges the second task only
    for the tail that outlived the first."""
    t0 = time.monotonic()
    result = await coro
    return result, (time.monotonic() - t0) * 1000


# ---------- ambient-recall prewarm ----------
#
# In voice, the words are known BEFORE the final transcript exists: the STT
# relay watches partial transcripts stream in and sees the commit flag the
# instant the user stops talking. Firing the ambient recall right then
# overlaps it with transcript finalization (measured 0.4–3s), so by the time
# the round starts the facts are usually already fetched. The store is
# strictly advisory: the round ADOPTS a prewarm only when it is fresh and
# its text matches the final transcript closely enough (_prewarm_matches);
# anything else falls back to the normal fresh recall - a miss can never be
# worse than not prewarming at all, and identical transcripts still yield
# identical facts.

PREWARM_TTL_S = 15.0
PREWARM_MIN_OVERLAP = 0.7
_recall_prewarm: dict = {}  # chat_id -> {"norm": str, "task": Task, "at": float}


def _norm_query(text):
    """Case/punctuation-insensitive form for comparing an utterance's last
    partial transcript against its final one."""
    return re.sub(r"[^a-z0-9 ]+", "", (text or "").lower()).strip()[:500]


def _prewarm_matches(prewarm_norm, final_norm):
    """The last partial usually trails the final text by trailing words or
    punctuation - accept when one normalized form is a prefix of the other
    covering >= PREWARM_MIN_OVERLAP of its length. Ambient recall is
    advisory by design ("use what bears, ignore what doesn't"), and equal
    transcripts compare equal here, so the acceptance bound above holds."""
    if not prewarm_norm or not final_norm:
        return False
    shorter, longer = sorted((prewarm_norm, final_norm), key=len)
    return longer.startswith(shorter) and len(shorter) / len(longer) >= PREWARM_MIN_OVERLAP


def _chat_memory_enabled(chat_id):
    con = db.connect()
    try:
        row = con.execute("SELECT memory_enabled FROM chats WHERE id=?",
                          (chat_id,)).fetchone()
        return bool(row and row["memory_enabled"])
    finally:
        con.close()


def prewarm_recall(chat_id, text, memory):
    """Start the ambient recall for a voice utterance at speech-end (called
    from the STT relay when the commit frame passes through). Replaces any
    prior prewarm for the chat, cancelling its task so a superseded
    utterance can't leak a stray request. Fire-and-forget: the round adopts
    the task if it matches, and memory.recall itself fails soft to []."""
    norm = _norm_query(text)
    if memory is None or not norm:
        return

    async def _job():
        if not await asyncio.to_thread(_chat_memory_enabled, chat_id):
            log.info("recall prewarm skipped (memory off): chat=%s", chat_id)
            return []
        facts = await memory.recall((text or "").strip()[:500], limit=6, origin="auto")
        log.info("recall prewarm fetched: chat=%s facts=%d", chat_id, len(facts))
        return facts

    old = _recall_prewarm.pop(chat_id, None)
    if old and not old["task"].done():
        old["task"].cancel()
    _recall_prewarm[chat_id] = {"norm": norm,
                                "task": asyncio.create_task(_job()),
                                "at": time.monotonic()}
    log.info("recall prewarm started: chat=%s norm_chars=%d", chat_id, len(norm))


async def _adopted_result(task):
    try:
        return await task
    except asyncio.CancelledError:
        return []


def _load_round_state(chat_id, messages, last_seen_id):
    """One speaker's DB reads, in one connection, on one worker thread: the
    cheap per-speaker row reads that deliberately stay per-speaker, plus the
    transcript delta. Returns None when the chat vanished mid-round. The
    connection opens and closes inside this function
    so it never crosses threads; the returned sqlite3.Row objects are plain
    fetched data, safe to read after close."""
    con = db.connect()
    try:
        chat = con.execute("SELECT * FROM chats WHERE id=?", (chat_id,)).fetchone()
        if not chat:
            return None
        project = None
        if chat["project_id"]:
            project = db.row_to_dict(
                con.execute("SELECT * FROM projects WHERE id=?",
                            (chat["project_id"],)).fetchone())
        roster = db.get_chat_participants(con, chat_id)
        names = db.participant_names(con)
        if messages is None:
            messages = db.get_chat_messages(con, chat_id)
        else:
            messages += db.get_messages_after(con, last_seen_id, chat_id=chat_id)
        if messages:
            last_seen_id = messages[-1]["id"]
        return {"chat": chat, "project": project, "roster": roster,
                "names": names, "messages": messages, "last_seen_id": last_seen_id,
                "shared_instructions": db.get_setting(con, "shared_instructions")}
    finally:
        con.close()


def _record_first_token_split(turn_id, chat_id, participant, t_iter_start,
                              t_provider_call, memory_summary_ms, memory_recall_ms):
    """Split the client's bundled final_to_first_token stopwatch server-side,
    the instant the round's first responder produces its first visible token.
    `t_iter_start` -> `t_provider_call` is context assembly (DB reads,
    tool-definition building, and - on the round's first responder only - the
    memory summary/recall reads); `t_provider_call` -> now is the provider's
    own raw time-to-first-token (thinking/deliberation included: see
    providers.py's reasoning-policy fix). Best-effort and content-free by
    construction (voice_trace.record_server_stage enforces the allowlist +
    numeric bounds again) - a tracing failure here must never break the
    actual voice round, so it's caught and logged, not raised."""
    if not turn_id or t_iter_start is None or t_provider_call is None:
        return
    try:
        now = time.monotonic()
        context_ms = (t_provider_call - t_iter_start) * 1000
        ttft_ms = (now - t_provider_call) * 1000
        provider = participant.get("provider", "")
        model = participant.get("model", "")
        speaker = participant.get("slug", "")
        con = db.connect()
        try:
            voice_trace.record_server_stage(
                con, turn_id, chat_id, "server_context_assembly", context_ms,
                provider=provider, model=model, speaker=speaker)
            voice_trace.record_server_stage(
                con, turn_id, chat_id, "server_provider_first_token", ttft_ms,
                provider=provider, model=model, speaker=speaker)
            if memory_summary_ms is not None:
                voice_trace.record_server_stage(
                    con, turn_id, chat_id, "server_memory_summary_wait",
                    memory_summary_ms, speaker=speaker)
            if memory_recall_ms is not None:
                voice_trace.record_server_stage(
                    con, turn_id, chat_id, "server_memory_recall_wait",
                    memory_recall_ms, speaker=speaker)
            con.commit()
        finally:
            con.close()
    except Exception:
        log.warning("voice-trace server-stage recording failed for turn %s", turn_id,
                    exc_info=True)


def _auto_participates(p):
    """A seat runs in an UNADDRESSED (full) round unless it is explicitly a
    `trial` seat - trial is manual-invoke-only. A row with no lifecycle
    recorded (older callers, bare test rosters) is treated as participating; only
    an explicit trial is gated, so nothing regresses."""
    return prov.auto_participates(p.get("lifecycle") or prov.ONBOARDED)


def pick_responders(text, chat, roster):
    """Return (ordered responder list, next_first slug).

    Explicit selection is always honored: @mentions of one or more roster
    members select exactly those, and a spoken-style leading vocative
    ("Claude and GPT, can you…" - voice can't type @) does the same. An
    explicitly-addressed seat speaks even if it is a `trial` seat - that IS the
    manual-invoke path.

    Otherwise everyone replies, rotating who opens each round - EXCEPT `trial`
    (unverified-cost) seats, which are manual-invoke-only and never run in an
    unaddressed round. Onboarded seats behave exactly as before. If a chat holds
    only trial seats, an unaddressed message selects no one until a seat is
    addressed by name or onboarded. Addressing the whole roster counts as
    manual selection when it reaches a seat a full round would exclude; when
    everyone named would run anyway, it is treated as an ordinary full round
    so opener rotation still advances."""
    if not roster:
        return [], chat["next_first"]
    slugs = {p["slug"]: p for p in roster}
    pattern = "|".join(re.escape(s) for s in slugs)
    mentioned = {m.lower() for m in re.findall(rf"@({pattern})\b", text or "", re.IGNORECASE)}
    mentioned = [slugs[s] for s in slugs if s in mentioned]
    # Explicit addressing is manual selection. Naming a strict subset always
    # selects it; naming the WHOLE roster only counts as manual selection when
    # it includes a seat a full round would exclude (a trial seat), otherwise
    # it is just a wordy full round and rotation below stays in charge. The
    # whole-roster case matters when the roster IS one trial seat: without it,
    # that chat cannot be spoken to at all (#11).
    def _explicit(selected):
        return selected and (len(selected) < len(roster)
                             or not all(_auto_participates(p) for p in selected))

    if _explicit(mentioned):
        return mentioned, chat["next_first"]  # manual selection - trial allowed
    vocative = _vocative_responders(text, roster)
    if _explicit(vocative):
        return vocative, chat["next_first"]  # manual selection - trial allowed
    # full round: only seats that auto-participate (trial seats sit it out).
    auto = [p for p in roster if _auto_participates(p)]
    if not auto:
        return [], chat["next_first"]
    order = [p["slug"] for p in auto]
    auto_slugs = {p["slug"]: p for p in auto}
    start = order.index(chat["next_first"]) if chat["next_first"] in order else 0
    rotated = [auto_slugs[order[(start + i) % len(order)]] for i in range(len(order))]
    next_first = order[(start + 1) % len(order)]
    return rotated, next_first


def _vocative_responders(text, roster):
    """Spoken addressing: when the message OPENS by naming members
    ("Claude and GPT, …", "GPT-OSS: you can sit this one out"), select those.
    Deliberately conservative - every leading token must be a roster name (an
    optional greeting word aside), otherwise it's not treated as addressing:
    "Hey guys, …" or "Okay, let's try" never match."""
    head = (text or "").strip()[:80]
    # Strip leading greeting/filler BEFORE the comma split - spoken addressing
    # is usually "Hey, GPT, can you…", and splitting at the first comma used to
    # leave just "Hey": no name found, full round, and the un-addressed models
    # answered anyway.
    head = re.sub(r"^(?:(?:hey|hi|ok|okay|so|um|uh|yeah|no)[,.!\s]+)+", "", head,
                  flags=re.IGNORECASE)
    head = head.split(",", 1)[0].split(":", 1)[0]
    tokens = [t.strip(" .!?") for t in re.split(r"\s+(?:and|&)\s+|\s*,\s*", head) if t.strip()]
    if not tokens:
        return []
    # No raw-count-vs-roster guard here: one seat answers to two forms (slug
    # and display name), so "GPT-OSS and GPT, ..." is two tokens for a
    # one-seat roster and a count check would reject a valid vocative (#11's
    # sibling). Non-name tokens already reject inside the loop below, and
    # duplicates collapse there, so the guard bought nothing else.
    def norm(s):
        # Voice transcription mangles compound names ("GPT-OSS" → "GPT OSS",
        # "gpt oss") - compare with separators stripped so spoken forms match.
        return re.sub(r"[\s.\-_]+", "", s.lower())

    by_name = {}
    for p in roster:
        by_name[norm(p["slug"])] = p
        if p.get("name"):
            by_name[norm(p["name"])] = p
    greeting = re.compile(r"^(?:hey|hi|ok|okay|so|um|uh)\s+", re.IGNORECASE)
    picked = []
    for t in tokens:
        p = by_name.get(norm(greeting.sub("", t).strip()))
        if p is None:
            return []  # any non-name token → not a vocative, full round
        if p not in picked:
            picked.append(p)
    return picked


async def run_turn(chat_id, responders, next_first, settings, memory,
                   mcp=None, turn_id=None):
    """A conversational turn: one round. A Claude Code guest summoned this round
    no longer speaks as the round's final turn - it runs DETACHED as a background
    job (backend/guestjobs.py) and hands its result back through a narrator on
    its own schedule. So a turn is now exactly one round; the guest's lifecycle
    is independent of it (a barge-in or a new message can't cancel an in-flight
    guest run, and the chat never parks waiting on one).

    `turn_id`: the client's own voice-trace correlation id, when this turn came
    from a live voice session (routers/chats.py's SendIn.turn_id). None for a
    normal text send - no server-side stage split is recorded then, since
    there's no client-side turn to correlate it with."""
    async for chunk in run_round(chat_id, responders, next_first, settings,
                                 memory, mcp=mcp, turn_id=turn_id):
        yield chunk


def make_handback(settings, memory, mcp):
    """Build the guest→conversation hand-back a completed job calls. The
    guest's reply is already persisted as the freshest message; ONE narrating
    model relays it ("select one synthesized user-facing response rather than
    repeating the report") instead of the whole roster each restating the same
    finding, voice and text both, one mechanism. Rotation still advances
    normally (`pick_responders`' own next_first), so who narrates varies round
    to round rather than always being the same seat. guestjobs decides WHEN: a
    blocker the instant the chat is idle, a routine result after a natural
    pause."""
    async def handback(chat_id, kind):
        from . import rounds
        if rounds.active(chat_id) is not None:
            return  # the conversation resumed on its own - reply is already in view
        con = db.connect()
        chat = con.execute("SELECT * FROM chats WHERE id=?", (chat_id,)).fetchone()
        roster = db.get_chat_participants(con, chat_id) if chat else []
        con.close()
        if not chat or not roster:
            return
        responders, next_first = pick_responders("", dict(chat), roster)
        if not responders:
            return
        responders = responders[:1]  # one synthesized reply, not a chorus
        gen = run_round(chat_id, responders, next_first, settings, memory,
                        mcp=mcp, is_handback=True)
        rounds.start(chat_id, gen)
    return handback


async def run_round(chat_id, responders, next_first, settings, memory,
                    mcp=None, is_handback=False, turn_id=None):
    """Async generator: stream each responder's reply as SSE strings, persisting
    as we go. Client disconnect persists the in-flight partial with a cut-off
    marker (see module docstring). `is_handback` marks a round spawned by a
    guest job's hand-back: a guest summoned inside such a round still runs but
    does NOT itself narrate, which bounds auto-rounds to one follow-up.
    `turn_id`: see run_turn's docstring - threaded through to _run_round_inner
    so the round's first responder can record the server-side
    context-assembly/provider-TTFT stage split."""
    cfg = settings.as_cfg()
    handback = make_handback(settings, memory, mcp)
    live = {"participant": None, "content": "", "tools": [], "usage": None}

    def persist_live(interrupted=False):
        p = live["participant"]
        if not p or not live["content"]:
            return None
        content = live["content"]
        if interrupted:
            content += f"\n\n[cut off by {cfg['user_name']}]"
        usage_json = None
        if live["usage"]:
            u = dict(live["usage"])
            u["model"] = p["model"]
            # a guest turn reports its real cost itself; API speakers price
            # from the local table
            # The seat's endpoint is part of pricing it: a keyless loopback seat
            # is self-hosted, so it costs a declared $0 rather than "unknown"
            # even when no rate card names its model.
            seat_cost = {"base_url": p["base_url"], "api_key_env": p["api_key_env"]}
            u["cost"] = u.get("cost") or compute_cost(p["model"], u,
                                                      cfg["pricing"], **seat_cost)
            # Record the cost provenance AT TURN TIME so a later
            # rate-card edit can never rewrite this row's history. A resident
            # API turn is priced from the local table → a rate_card_estimate,
            # never a billed figure (that is only a provider-reported cost).
            u.setdefault("cost_provenance",
                         provenance_for(p["model"], cfg["pricing"], **seat_cost))
            usage_json = json.dumps(u)
        con = db.connect()
        # The single centralized insert path - also wakes any
        # connected client via the global events bus, same as an out-of-band
        # deploy notice does.
        msg = db.insert_message(con, chat_id, p["slug"], content,
                                usage_json=usage_json, tool_events=live["tools"])
        con.close()
        live["participant"] = None
        live["content"] = ""
        live["tools"] = []
        live["usage"] = None
        return msg

    try:
        async for chunk in _run_round_inner(chat_id, responders, next_first, cfg,
                                            live, persist_live, memory, mcp,
                                            handback, is_handback, turn_id):
            yield chunk
    except (GeneratorExit, asyncio.CancelledError):
        # Client disconnected mid-reply - keep the partial, marked. This
        # persist stays SYNCHRONOUS on purpose: inside cancellation
        # cleanup a fresh `await` is itself immediately cancelled, and the
        # partial would be lost instead of persisted. One blocking fsync on
        # a user-initiated abort is the right trade.
        persist_live(interrupted=True)
        raise


async def _run_round_inner(chat_id, responders, next_first, cfg, live,
                           persist_live, memory, mcp=None,
                           handback=None, is_handback=False, turn_id=None):
    memory_summary_cache = None  # fetched at most once per round
    ambient_cache = None  # ambient recall for the latest user message, once per round
    spoken = []  # who already replied THIS round - later speakers are told
    # The transcript is read IN FULL once per round; every later speaker
    # only fetches the delta (id > last seen) - normally just the previous
    # speaker's persisted reply, plus any mid-round external inserts, so
    # nothing a full re-read would have shown is ever missed. Guest-CLI
    # availability (a filesystem PATH scan) and the memory probe (an HTTP
    # call) are likewise resolved once per round, not once per speaker.
    messages = None
    last_seen_id = 0
    guest_ok = None
    memory_up = None
    for idx, participant in enumerate(responders):
        # Server-side half of the client's final_to_first_token stopwatch
        # - only meaningful for the round's FIRST responder (the one the
        # client is actually timing; the round is sequential, so a later
        # speaker's provider call starts well after that measurement ends).
        t_iter_start = time.monotonic() if (turn_id and idx == 0) else None
        memory_summary_ms = memory_recall_ms = None
        # The speaker's DB reads run in a worker thread - sqlite's
        # blocking I/O (and its up-to-5s busy_timeout under lock contention)
        # must not pin the event loop that is simultaneously relaying live
        # voice websockets and SSE streams.
        state = await asyncio.to_thread(_load_round_state, chat_id, messages,
                                        last_seen_id)
        if state is None:
            break
        chat = state["chat"]
        project = state["project"]
        roster = state["roster"]
        names = state["names"]
        messages = state["messages"]
        last_seen_id = state["last_seen_id"]
        # Use the existing summary + un-folded recent transcript; the fold runs as a
        # background task after each round (prompt caching keeps the larger prefix
        # cheap), so a round never stalls mid-conversation waiting on a summary.
        summary = chat["summary"]
        # slash commands are notes to tooling - never part of what models read
        transcript = [m for m in messages
                      if m["id"] > chat["summary_upto"]
                      and not (m["speaker"] == "user"
                               and m["content"].lstrip().startswith("/"))]
        # This warning flips with memory-service health while `summary_upto`
        # stays put - so unlike a real summary fold it does NOT change the
        # transcript, and concatenating it into `summary` (the CACHED stable
        # block) made every flip re-write the whole prefix at Anthropic's 1.25x
        # cache-write rate. It is the same defect as the earlier prompt-cache
        # regression (volatile content parked in the cached stable block), on a
        # path that earlier fix never looked at, and it fires exactly when
        # memory is already degraded - so a memory outage doubled as a
        # prompt-cache outage. It rides the volatile tail now; the model reads
        # the same sentence.
        write_warning = bool(memory is not None and memory.any_write_failed())
        voice_mode = bool(chat["voice_mode"])
        round_cfg = dict(cfg)
        round_cfg["memory_write_warning"] = (
            "a recent memory save failed, so some facts from a just-finished chat "
            "may not be recorded yet - don't assume they're stored."
        ) if write_warning else ""
        round_cfg["shared_instructions"] = state["shared_instructions"]
        round_cfg["round_predecessors"] = list(spoken)  # dynamic, this round only
        round_cfg["chat_id"] = chat_id  # summon_claude_code queues per chat
        memory_on = bool(chat["memory_enabled"])
        web_on = bool(chat["web_enabled"])
        code_on = bool(chat["code_enabled"])
        names.setdefault(guest.SLUG, guest.NAME)  # past guest turns replay named
        names.setdefault("system", "System")  # tooling notices, labeled
        for m in messages:  # external events: "build-watcher (external feed)"
            if m["speaker"].startswith("ext:"):
                names.setdefault(m["speaker"],
                                 f"{m['speaker'][4:]} (external feed)")

        # Is summon_claude_code already claimed (queued this round, or a
        # detached job still running from an earlier one)? If so it is not
        # offered as a tool this turn - the strongest form of "don't duplicate
        # it" - and every participant's system prompt gets a note explaining
        # why, so nobody narrates or re-proposes it either (voice included).
        claim = guest.claimed(chat_id)
        round_cfg["delegation_note"] = guest.delegation_note(claim)

        # get_diagnostic: always offered, no per-chat toggle - same reasoning
        # as the guest's unconditional MCP mount: it carries no secret and
        # reaches nothing beyond this process's own in-memory/db state, so
        # gating it behind web_on/code_on would only reintroduce the
        # escalate-to-a-guest step get_diagnostic exists to remove.
        tool_defs = list(tools_mod.diagnostics_tool_definitions())
        if web_on:
            tool_defs += tools_mod.tool_definitions(round_cfg)
        if guest_ok is None:  # one PATH scan per round, not per speaker
            guest_ok = guest.available(round_cfg)
        if code_on and guest_ok and not claim:
            tool_defs += tools_mod.code_tool_definitions(round_cfg)
        if code_on and tools_mod.github_available(round_cfg):
            tool_defs += tools_mod.github_tool_definitions(round_cfg)
        if mcp is not None and mcp.tool_definitions():
            # external MCP tools: offered whenever a configured server is
            # connected; the manager rides in round_cfg for run_tool dispatch
            tool_defs += mcp.tool_definitions()
            round_cfg["_mcp"] = mcp
        tool_memory = None
        if memory_up is None and memory_on and memory is not None:
            memory_up = await memory.probe()  # once per round
        if memory_on and memory is not None and memory_up:
            if memory_summary_cache is None:
                # Summary + ambient recall, concurrently. The ambient recall
                # keys on the latest user message and prepares the per-fact
                # detail the summary won't hold - models kept forgetting to
                # reach for it themselves (the tool remains for deliberate
                # digging; origin="auto" keeps the two distinguishable in the
                # memory service's access log and live view).
                q = next((m["content"] for m in reversed(messages)
                          if m["speaker"] == "user"), "").strip()[:500]
                # Each read clocks ITSELF (they run concurrently, so awaiting
                # one and then clocking the other from the outside only
                # measures the tail that outlived the first await, which
                # under-reports it). These attribute "how long did each read
                # take", not an additive total, so a slow companion memory
                # service shows up as its own diagnostic stage instead of
                # being folded invisibly into final_to_first_token.
                #
                # Adopt a speech-end prewarm when it is fresh and its
                # text matches this round's final transcript; otherwise run
                # the normal fresh recall (and cancel the stale task). For an
                # adopted task _timed_ms clocks the round's RESIDUAL wait -
                # near zero when the prewarm already finished - which is what
                # server_memory_recall_wait should honestly report.
                pre = _recall_prewarm.pop(chat_id, None)
                adopted = None
                if pre and q and (time.monotonic() - pre["at"]) <= PREWARM_TTL_S \
                        and _prewarm_matches(pre["norm"], _norm_query(q)):
                    adopted = pre["task"]
                elif pre and not pre["task"].done():
                    pre["task"].cancel()
                # content-free: which path this round's ambient recall took
                log.info("ambient recall: %s (chat=%s)",
                         "adopted-prewarm" if adopted is not None
                         else ("prewarm-mismatch" if pre else "no-prewarm"),
                         chat_id)
                summary_task = asyncio.create_task(_timed_ms(memory.get_summary()))
                if adopted is not None:
                    recall_task = asyncio.create_task(_timed_ms(_adopted_result(adopted)))
                else:
                    recall_task = asyncio.create_task(_timed_ms(
                        memory.recall(q, limit=6, origin="auto"))) if q else None
                memory_summary_cache, memory_summary_ms = await summary_task
                facts = []
                if recall_task:
                    facts, memory_recall_ms = await recall_task
                ambient_cache = tools_mod._format_facts(facts, 2500) if facts else ""
            round_cfg["memory_summary"] = memory_summary_cache
            round_cfg["memory_ambient"] = ambient_cache
            tool_defs += tools_mod.memory_tool_definitions(round_cfg["user_name"])
            tool_memory = memory
        tool_defs = tool_defs or None

        live["participant"] = participant
        live["content"] = ""
        live["tools"] = []
        live["usage"] = None
        yield sse({"type": "speaker_start", "speaker": participant["slug"]})
        t_provider_call = time.monotonic() if t_iter_start is not None else None
        try:
            async for kind, payload in providers.stream_reply(
                participant, roster, transcript, names, round_cfg, project, summary,
                voice_mode, tools=tool_defs, memory=tool_memory,
            ):
                if kind == "text":
                    if t_provider_call is not None:
                        # First visible token - record the split once,
                        # best-effort (a trace-write failure must never break
                        # the actual round), then never again this participant.
                        # On a worker thread - this write races the
                        # first delta reaching the client otherwise.
                        await asyncio.to_thread(
                            _record_first_token_split,
                            turn_id, chat_id, participant, t_iter_start,
                            t_provider_call, memory_summary_ms, memory_recall_ms)
                        t_provider_call = None
                    live["content"] += payload
                    yield sse({"type": "delta", "speaker": participant["slug"], "text": payload})
                elif kind == "usage":
                    live["usage"] = payload
                elif kind == "tool":
                    live["tools"].append(payload)
                    yield sse({
                        "type": "tool_activity",
                        "speaker": participant["slug"],
                        "tool": payload["tool"],
                        "input_json": json.dumps(payload["input"]),
                        "output_text": payload["output"],
                    })
                elif kind == "work_status":
                    # A structured liveness event (never text) - proves the
                    # round is alive over the SAME SSE stream the reply
                    # rides, WITHOUT touching live["content"]. This is the
                    # fix for the earlier persistence bug: the old
                    # text-shaped version was folded into live["content"] and
                    # ended up baked into the persisted assistant message.
                    # Never buffered, never replayed, never part of any DB
                    # row - a client that isn't connected right now simply
                    # never sees it, which is the point.
                    yield sse({
                        "type": "work_status",
                        "speaker": participant["slug"],
                        "phase": payload["phase"],
                        "label": payload["label"],
                    })
        except (GeneratorExit, asyncio.CancelledError):
            raise  # client disconnected - run_round persists the partial reply
        except Exception as e:
            yield sse({"type": "error", "speaker": participant["slug"], "message": str(e)})
            if not live["content"]:
                live["participant"] = None
                continue
        if not live["content"] and not live["tools"]:
            # model finished without text or tool calls (some local reasoning
            # models occasionally emit only reasoning) - say so, never vanish
            yield sse({"type": "error", "speaker": participant["slug"],
                       "message": "returned an empty reply (no text, no tool calls) - try again"})
            live["participant"] = None
            continue
        # The between-speakers persist (INSERT + fsync) runs on a worker
        # thread. The abort-path persist in run_round deliberately stays
        # synchronous: it executes inside GeneratorExit/CancelledError
        # cleanup, where a fresh await would itself be cancelled and the
        # partial reply would be LOST instead of persisted with its marker.
        msg = await asyncio.to_thread(persist_live)
        if msg:
            yield sse({"type": "speaker_end", "speaker": participant["slug"], "message": msg})
            spoken.append(participant["name"])

    # Guest job: if anyone summoned Claude Code this round, kick it off as
    # a DETACHED background job instead of speaking inline. The round finishes
    # now; the guest keeps working independently and hands its result back later
    # through a narrator (guestjobs + make_handback). A guest summoned inside a
    # hand-back round runs but does not itself narrate (bounds the follow-ups).
    summons = guest.take(chat_id)
    if summons:
        started = _launch_guest_job(chat_id, summons, cfg, handback,
                                    narrate=not is_handback)
        if started:
            yield sse(started)

    con = db.connect()
    con.execute("UPDATE chats SET next_first=? WHERE id=?", (next_first, chat_id))
    con.commit()
    con.close()
    # Reflect off the critical path after the round: pre-compute the rolling
    # summary and refresh a stale auto-title, ready for the next round.
    task = asyncio.create_task(post_round_reflect_job(chat_id, cfg))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    yield sse({"type": "done"})


GUEST_CONTEXT_MESSAGES = 10   # recent-transcript tail shown to the guest
GUEST_CONTEXT_CHARS = 4000


def _launch_guest_job(chat_id, summons, cfg, handback, narrate=True):
    """Prepare a summoned guest's context + resume handle and start it as a
    DETACHED job. Returns the `guest_job` SSE dict to push to the round's
    attached client (so its status chip appears immediately), or None when the
    guest can't run (code disabled after summons, or a job already in flight).
    The job itself runs independently of this round - see guestjobs.start."""
    from . import guestjobs
    con = db.connect()
    chat = con.execute("SELECT * FROM chats WHERE id=?", (chat_id,)).fetchone()
    names = db.participant_names(con)
    messages = db.get_chat_messages(con, chat_id)
    con.close()
    if not chat or not chat["code_enabled"]:
        return None
    if guestjobs.active_for_chat(chat_id):
        return None  # one guest job per chat - block, don't queue

    lines = []
    for m in messages[-GUEST_CONTEXT_MESSAGES:]:
        who = cfg["user_name"] if m["speaker"] == "user" else names.get(m["speaker"], m["speaker"])
        lines.append(f"{who}: {m['content']}")
    context = "\n".join(lines)[-GUEST_CONTEXT_CHARS:]

    # continue_last: resume the guest's previous visit in this chat - the
    # stored session_id re-enters that session with its full working context,
    # e.g. "now implement the plan you just made". A session is bound to one
    # repo's worktree, so it only resumes when the repos agree: on a mismatch
    # the explicit repo wins, the resume is dropped, and the chat is told -
    # silently overriding stranded a guest in the wrong checkout.
    resume = None
    repo = summons["repo"]
    resume_note = ""
    # On resume, reuse the prior visit's worktree key so the guest lands in the
    # SAME cwd - the CLI's session lookup is keyed by working directory, so a
    # fresh isolated path would strand the resume without its working context.
    # None → a fresh, uniquely-keyed worktree.
    session_key = None
    if summons.get("continue_last"):
        for m in reversed(messages):
            if m["speaker"] == guest.SLUG and m.get("usage_json"):
                u = json.loads(m["usage_json"])
                if u.get("session_id"):
                    prior_repo = u.get("repo")
                    if prior_repo and prior_repo != repo:
                        resume_note = (
                            f'(continue_last ignored: the previous visit was in '
                            f'"{prior_repo}" but this summon targets "{repo}" - '
                            f'sessions are bound to one repo, so this is a fresh '
                            f'session without the earlier working context)\n\n')
                    else:
                        resume = u["session_id"]
                        repo = prior_repo or repo
                        session_key = u.get("worktree_key")
                    break

    job = guestjobs.start(
        chat_id, summons["task"], repo, context, cfg,
        mode=summons.get("mode", "investigate"), resume=resume,
        model=summons.get("model", ""), effort=summons.get("effort", ""),
        requested_by=summons.get("requested_by", "unknown"),
        narrate=narrate, handback=handback, prefix=resume_note,
        session_key=session_key, ref=summons.get("ref", ""))
    if not job:
        return None
    return {"type": "guest_job", "chat_id": chat_id, "id": job.id,
            "status": "running", "kind": "", "task": job.task_desc,
            "repo": job.repo, "mode": job.mode, "step_count": 0}


# Keep strong references so fire-and-forget tasks aren't garbage-collected mid-run.
_background_tasks: set = set()


def spawn(coro):
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task


async def post_round_reflect_job(chat_id, cfg):
    """Off the critical path after every round (so the next round never stalls):
    (a) pre-compute the rolling summary ready for next time; (b) refresh the
    auto-title if the chat has grown past TITLE_REFRESH_DELTA since it was last
    titled. Both are cheap watermark-guarded no-ops until their delta is hit.

    The title check runs on EVERY round completion, decoupled from the fold -
    an always-active chat that never triggers the summary fold (or the leave/
    sweep reflection pass) would otherwise outrun its title forever.
    maybe_title_chat still no-ops on user-renamed chats (title_upto == -1) and
    still uses the cheap utility model. Best-effort."""
    try:
        # The full-transcript read - the heavy part - runs on a worker
        # thread so a voice turn arriving right after "done" doesn't compete
        # with it for the event loop. The maybe_* calls keep their own
        # connection for the small watermark writes (their LLM call is async
        # and can't live inside a thread).
        def _read():
            con = db.connect()
            try:
                row = con.execute("SELECT * FROM chats WHERE id=?",
                                  (chat_id,)).fetchone()
                if not row:
                    return None, None
                return dict(row), db.get_chat_messages(con, chat_id)
            finally:
                con.close()
        chat, messages = await asyncio.to_thread(_read)
        if chat:
            con = db.connect()
            try:
                await chat_memory.maybe_summarize(con, chat, messages, cfg)
                await chat_memory.maybe_title_chat(con, chat, messages, cfg)
            finally:
                con.close()
    except Exception:
        log.exception("post-round reflection for chat %s failed", chat_id)


REFLECT_SWEEP_S = 300  # background sweep cadence
REFLECT_IDLE_S = 180   # a chat must be this quiet before the sweep reflects it


def sweep_candidates(con, idle_s=REFLECT_IDLE_S, limit=10):
    """Chats the periodic sweep should reflect: quiet for a while, with content
    past a watermark (un-ingested messages, or enough new ones to (re)title).
    Exists because the leave hook only fires on chat-SWITCH in the UI - close
    the tab (or end a phone voice session) and titles stick as first-message
    placeholders and the transcript never reaches memory. leave_chat_job is
    watermark-guarded, so sweeping a candidate twice is a cheap no-op."""
    from .chat_memory import TITLE_MIN_MESSAGES, TITLE_REFRESH_DELTA
    now = db.now()
    rows = con.execute(
        "SELECT c.id FROM chats c "
        "JOIN (SELECT chat_id, MAX(id) AS last_id, COUNT(*) AS n FROM messages "
        "      GROUP BY chat_id) m ON m.chat_id = c.id "
        "WHERE c.updated_at <= ? AND ("
        "  (c.memory_enabled = 1 AND m.last_id > c.ingested_upto) "
        "  OR (c.title_upto = 0 AND m.n >= ?) "
        "  OR (c.title_upto > 0 AND m.last_id - c.title_upto >= ?)"
        ") ORDER BY c.updated_at DESC LIMIT ?",
        (now - idle_s, TITLE_MIN_MESSAGES, TITLE_REFRESH_DELTA, limit))
    return [r["id"] for r in rows]


async def reflection_sweep_loop(get_cfg, memory):
    """Every REFLECT_SWEEP_S, run the leave pass over settled chats that still
    have unreflected content. Cancelled on app shutdown."""
    while True:
        await asyncio.sleep(REFLECT_SWEEP_S)
        try:
            con = db.connect()
            ids = sweep_candidates(con)
            con.close()
            for cid in ids:
                await leave_chat_job(cid, get_cfg(), memory)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("reflection sweep failed; retrying next interval")


async def leave_chat_job(chat_id, cfg, memory):
    """The /distill leave hook, run in the background so leaving is instant:
    (a) chat-side reflection - rolling summary, auto-title, project distill;
    (b) memory-service handoff - ingest new messages past the watermark, then
        trigger the service's reflection pass. Failures are recorded on the
        memory client and surfaced in /api/state."""
    memory_enabled = False
    try:
        con = db.connect()
        chat = con.execute("SELECT * FROM chats WHERE id=?", (chat_id,)).fetchone()
        if not chat:
            con.close()
            return
        chat = dict(chat)
        memory_enabled = bool(chat["memory_enabled"])
        messages = db.get_chat_messages(con, chat_id)
        await chat_memory.maybe_summarize(con, chat, messages, cfg)
        if chat["project_id"]:
            project = db.row_to_dict(con.execute(
                "SELECT * FROM projects WHERE id=?", (chat["project_id"],)).fetchone())
            if project:
                await chat_memory.distill_project_memory(con, chat, project, messages, cfg)
        await chat_memory.maybe_title_chat(con, chat, messages, cfg)
        con.close()
    except Exception:
        log.exception("chat-side leave pass for chat %s failed", chat_id)

    # The episodic handoff respects the chat's memory toggle: a memory-off chat
    # never reaches the service's tapes.
    if memory is None or not memory_enabled:
        return

    def _get_new_messages():
        c = db.connect()
        upto = c.execute("SELECT ingested_upto FROM chats WHERE id=?",
                         (chat_id,)).fetchone()
        if upto is None:
            c.close()
            return []
        msgs = [dict(r) for r in c.execute(
            "SELECT id, speaker, content, created_at FROM messages "
            "WHERE chat_id=? AND id>? ORDER BY id", (chat_id, upto[0]))]
        by_id = {m["id"]: m for m in msgs}
        if msgs:
            rows = [dict(r) for r in c.execute(
                "SELECT a.* FROM attachments a JOIN messages m ON a.message_id=m.id "
                "WHERE m.chat_id=? AND m.id>? ORDER BY a.id", (chat_id, upto[0]))]
            for a in rows:
                # ship the actual bytes - the memory service stores files whole
                # so nothing that traveled with a chat is ever lost to memory
                if a["size"] > memory_client_mod.MAX_ATTACH_BYTES:
                    log.warning("attachment %s (%d bytes) exceeds the handoff "
                                "cap - kept locally, not mirrored to memory",
                                a["filename"], a["size"])
                    continue
                try:
                    data = base64.standard_b64encode(
                        open(att_mod.file_path(a), "rb").read()).decode()
                except OSError:
                    log.warning("attachment file missing for %s - skipped",
                                a["filename"])
                    continue
                by_id[a["message_id"]].setdefault("attachments", []).append(
                    {"filename": a["filename"], "mime": a["mime"],
                     "data_b64": data})
        c.close()
        return msgs

    def _advance(last_id):
        c = db.connect()
        c.execute("UPDATE chats SET ingested_upto=? WHERE id=?", (last_id, chat_id))
        c.commit()
        c.close()

    await memory.handoff_chat(chat_id, _get_new_messages, _advance)
