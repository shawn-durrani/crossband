"""Chat-side reflection: rolling chat summaries, auto-titles, and project
memory distillation. Ported from the predecessor's memory.py - the CHAT-side
parts only. The durable user-fact ledger, summary generation, and history
search live in Membro, the companion memory service (see memory_client.py)."""

import logging
import re

from . import attachments as att_mod
from . import config as config_mod
from . import context_weight, db
from . import llm_util
from .llm_util import utility_complete_with_usage

log = logging.getLogger("crossband.chat_memory")

# chat auto-titling (see maybe_title_chat)
TITLE_MIN_MESSAGES = 2    # need at least one exchange before titling
TITLE_REFRESH_DELTA = 8   # re-title once this many messages arrive after the last title


async def _run_utility(con, chat_id, kind, prompt, cfg, max_tokens):
    """Run one utility-model call and persist its token cost so
    rolling-summary/auto-title/project-distillation spend - historically
    invisible, since these calls never go through db.insert_message - shows
    up on the Spend page. Returns the reply text, or None (missing key /
    degrade - the same "keep going without it" behavior as before; nothing
    to log since no call was actually made).

    Commits immediately after logging, on its own, rather than leaving the
    insert pending for the caller to commit alongside its own write. Every
    caller here treats a falsy-but-non-None result (empty string, or a title
    that strips down to nothing - e.g. a haiku reply that's just quote marks)
    as "no title/summary to apply" and returns early *before* its own
    `con.commit()` - which used to silently roll back the utility_usage row
    for the exact degenerate-output case this table exists to catch. The API
    call already happened and cost real money regardless of what the caller
    does with the text, so its cost record must survive independent of the
    caller's own control flow."""
    model = cfg.get("utility_model") or "claude-haiku-4-5"
    result = await utility_complete_with_usage(prompt, cfg, max_tokens=max_tokens, model=model)
    if result.text is None:
        return None
    # Priced through llm_util so this path and utility_complete_logged, the
    # scan path, cannot price the same call differently. Recorded AT WRITE
    # TIME, not recomputed from the live rate card later: a later rate-card
    # edit must never rewrite what this call's cost meant when it was made.
    cost, provenance = llm_util.price_utility_call(model, result, cfg)
    db.log_utility_usage(con, chat_id, kind, model, result.input_tokens,
                         result.output_tokens, cost, provenance=provenance)
    con.commit()
    return result.text


def fold_labels(messages, names, cfg) -> set:
    """The display labels of everyone who actually spoke in a fold chunk -
    the same names the transcript and the tag instruction use, so the
    validator below judges the summary against exactly the vocabulary the
    utility model was told to tag with."""
    out = set()
    for m in messages:
        if not (m.get("content") or "").strip():
            continue
        if m["speaker"] == "user":
            out.add(cfg["user_name"])
        else:
            out.add(names.get(m["speaker"], m["speaker"]))
    return out


def summary_attribution_ok(summary: str, labels: set) -> bool:
    """#22's structural floor: may this updated summary REPLACE the original
    turns? Compression is where who-said-what quietly dies - the fold prompt
    demands [Speaker] tags, but a prompt is guidance, and a summary that
    ignored it used to become trusted context anyway. This check is
    deliberately mechanical, not semantic: it proves the tag discipline was
    followed at all, not that every line is correctly attributed.

    - one voice folded: at least one tag naming that voice;
    - several voices folded: tags naming at least two distinct folded
      voices, so the summary cannot be single-voice mush that hands one
      participant everyone's statements.

    Tags naming nobody in the fold don't count. A failing summary is
    REFUSED: the originals stay in context (costlier, never wrong)."""
    if not labels:
        return True
    tagged = {m.group(1).strip() for m in re.finditer(r"\[([^\[\]]{1,60})\]",
                                                      summary or "")}
    hits = {l for l in labels if l in tagged}
    return len(hits) >= min(2, len(labels))


def transcript_text(messages, names, cfg):
    lines = []
    for m in messages:
        label = cfg["user_name"] if m["speaker"] == "user" else names.get(m["speaker"], m["speaker"])
        line = f"{label}: {m['content']}"
        for a in m.get("attachments", []):
            line += "\n" + att_mod.text_description(a)
        lines.append(line)
    return "\n\n".join(lines)


async def maybe_summarize(con, chat, messages, cfg):
    """If the un-summarized transcript is too large, fold older messages into
    the chat summary. Returns (summary, recent_messages)."""
    recent = [m for m in messages if m["id"] > chat["summary_upto"]]
    # Weight, not just text: counting `content` alone let an
    # image-heavy chat sail under the 60,000-character threshold while being
    # one of the heaviest conversations in the database. The unit stays
    # character-equivalents so `summary_threshold_chars` keeps its meaning.
    total_chars = context_weight.fold_weight(recent)
    keep = cfg["keep_recent_messages"]
    if total_chars <= cfg["summary_threshold_chars"] or len(recent) <= keep:
        return chat["summary"], recent

    names = db.participant_names(con)
    to_fold, kept = recent[:-keep], recent[-keep:]
    # Compression is where provenance quietly dies -- once the original turns
    # fold out of the live transcript, this summary becomes the only record, and it
    # gets injected back as trusted context on every later call ("Everything
    # summarized above WAS received and read", providers.py). A summary that drops
    # WHO said something lets a later model claim the human said whatever an AI
    # participant actually said (or vice versa), with no way left to check --
    # requiring an explicit speaker tag per line keeps that attribution alive
    # through compression instead of erasing it.
    prompt = (
        "You maintain a running summary of a group chat between a human and one or more "
        "AI models. Update the summary to incorporate the new messages. "
        "Preserve key facts, decisions, open questions, and each participant's stated "
        "positions. Note any attachments by name. "
        "Every retained fact, decision, or stated position MUST keep an explicit "
        f"speaker tag in square brackets - e.g. '[{cfg['user_name']}] wants the deploy "
        "delayed', '[GPT] proposed the rollback plan'. Never merge two participants' "
        "statements into one unattributed line, never restate what an AI participant "
        f"said as if [{cfg['user_name']}] said it, and if the existing summary below "
        "already has an untagged line, add the best-supported tag rather than leaving "
        "it bare. Reply with ONLY the updated summary, under 800 words.\n\n"
        f"## Existing summary\n{chat['summary'] or '(none yet)'}\n\n"
        f"## New messages to fold in\n{transcript_text(to_fold, names, cfg)}"
    )
    new_summary = await _run_utility(con, chat["id"], "summarize", prompt, cfg, 2000)
    if new_summary is None:
        # No utility model available - keep going with the full transcript.
        return chat["summary"], recent
    if not summary_attribution_ok(new_summary, fold_labels(to_fold, names, cfg)):
        # #22: the summary dropped the speaker tags, so folding it in would
        # erase who-said-what and inject the mush back as trusted context.
        # Refuse: keep the original turns (costlier, never wrong) and let a
        # later round try again. Content-free log, counts only.
        log.info("summary fold REFUSED, attribution tags missing: chat=%s "
                 "folded=%d speakers=%d", chat["id"], len(to_fold),
                 len(fold_labels(to_fold, names, cfg)))
        return chat["summary"], recent

    con.execute(
        "UPDATE chats SET summary=?, summary_upto=? WHERE id=?",
        (new_summary, to_fold[-1]["id"], chat["id"]),
    )
    con.commit()
    return new_summary, kept


async def maybe_title_chat(con, chat, messages, cfg):
    """Give the chat a short, content-summarized title using the cheap utility
    model. Runs after every round (engine.post_round_reflect_job) and from the
    leave/reflection pass: once early (upgrading the first-message placeholder),
    then again when the chat has grown materially since it was last titled. The
    per-round call keeps an always-active chat from outrunning its title.
    Cheaply no-ops via title_upto/TITLE_REFRESH_DELTA until the delta is hit.
    Never overwrites a user-renamed chat.

    title_upto: 0 = auto/placeholder (eligible); >0 = LLM-titled through that
    message id; -1 = user-renamed (locked). Returns the new title, or None."""
    upto = chat.get("title_upto") or 0
    if upto < 0:
        return None  # user named it - leave it alone
    real = [m for m in messages if (m["content"] or "").strip()]
    if len(real) < TITLE_MIN_MESSAGES:
        return None
    last_id = messages[-1]["id"]
    if upto > 0 and (last_id - upto) < TITLE_REFRESH_DELTA:
        return None  # already titled; too little new content to re-title

    names = db.participant_names(con)
    full = transcript_text(messages, names, cfg)
    excerpt = full if len(full) <= 1600 else full[:1200] + "\n…\n" + full[-400:]
    prompt = (
        "Write a very short title for this conversation - 2 to 4 words, ideally under "
        "~28 characters, so it fits a narrow chat sidebar. Name the actual topic as "
        "tersely as a file name; drop articles and filler. No quotes, no trailing "
        "punctuation, no 'Chat about' / 'Discussion of' prefixes. Reply with ONLY the "
        "title.\n\n" + excerpt
    )
    title = await _run_utility(con, chat["id"], "title", prompt, cfg, 16)
    if not title:
        return None  # no utility model / empty - keep the existing title
    title = title.strip().strip('"\'“”‘’')
    if not title:
        # Quote-marks-only reply (e.g. '""') strips down to "" here - bail
        # before .splitlines()[0], which raises IndexError on an empty
        # string. The utility_usage row for this call is already committed by
        # _run_utility regardless of this early return.
        return None
    title = title.splitlines()[0][:40].strip()
    if not title:
        return None
    con.execute("UPDATE chats SET title=?, title_upto=? WHERE id=?",
                (title, last_id, chat["id"]))
    con.commit()
    return title


async def distill_project_memory(con, chat, project, messages, cfg):
    """Fold this chat's new messages into the project's memory notes."""
    new_msgs = [m for m in messages if m["id"] > chat["distilled_upto"]]
    if not new_msgs:
        return project["memory"]

    names = db.participant_names(con)
    # Same reasoning as maybe_summarize above -- this note text is also
    # injected back as trusted "## Project instructions + project memory" context
    # for every chat in the project, so it needs the same speaker tag to survive.
    prompt = (
        "You maintain the shared memory notes for a project that groups several chats "
        "between a human and one or more AI models. Update the notes with anything "
        "durable from the conversation excerpt below: facts about the human and their goals, "
        "decisions made, conclusions reached, preferences expressed, and ongoing threads. "
        "Tag every durable item with who it came from, in square brackets - e.g. "
        f"'[{cfg['user_name']}] wants weekly status emails', '[Claude] recommended "
        "Postgres over SQLite' - never state an AI participant's suggestion or "
        f"conclusion as if [{cfg['user_name']}] said it. "
        "Drop chit-chat. Keep the notes organized with short markdown headings and bullets, "
        "under 600 words total. Reply with ONLY the updated notes.\n\n"
        f"## Current project memory\n{project['memory'] or '(empty)'}\n\n"
        f"## New conversation excerpt (chat: {chat['title']})\n"
        f"{transcript_text(new_msgs, names, cfg)}"
    )
    new_memory = await _run_utility(con, chat["id"], "distill", prompt, cfg, 1500)
    if new_memory is None:
        return project["memory"]

    con.execute("UPDATE projects SET memory=? WHERE id=?", (new_memory, project["id"]))
    con.execute(
        "UPDATE chats SET distilled_upto=? WHERE id=?", (new_msgs[-1]["id"], chat["id"])
    )
    con.commit()
    return new_memory
