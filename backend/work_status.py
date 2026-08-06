"""Shared policy for ongoing-work status: prove work is still happening
before the chat goes silent, using a STRUCTURED status event (a trusted
activity label, never a hardcoded filler sentence and never the model's own
prose) that rides the live SSE/events stream and is NEVER persisted into a
chat message or folded into the model's own reply text.

Two independent call sites use this:

- backend/providers.py's tool-await loop (`_tool_batch_events`) — ordinary
  web/GitHub/MCP tool calls, and the (fast) `summon_claude_code` queuing call
  itself. This covers what would otherwise be dead air INSIDE an active
  round, in both text and voice mode: the event rides the SAME SSE stream as
  the model's own reply, so no separate voice pipeline is needed to prove
  liveness there (this deliberately does NOT add spoken acknowledgements yet;
  that is a separate product decision, recorded in DECISIONS.md).
- backend/guestjobs.py's detached job lifecycle — the delegated Claude Code
  guest's OWN multi-minute run, which continues after the round that
  summoned it has already finished. That needs its own periodic check-in,
  broadcast fully ephemerally over the existing guest-job events channel
  (never a chat message).

Deliberately engine-level, not prompt-only — a model can forget or skip a
spoken acknowledgement (the live incident behind this: dead air with no
explanation); a timer cannot. Kept as one shared module so both call sites
apply the exact same thresholds and activity taxonomy rather than drifting
apart.

This supersedes an earlier fixed-wording design (see DECISIONS.md): the
payload is now a small closed vocabulary of trusted, honest activity labels,
never the raw tool name or arguments and never anything the model wrote, so a
status chip can never leak reasoning or render an unsafe string."""

# Tools that resolve near-instantly against local/in-process state — no
# acknowledgement is warranted ("do not add a preamble for trivial/
# instantaneous internal work"). Everything else defaults to "may create
# noticeable delay", including any MCP tool and summon_claude_code's own
# (fast) queuing call — so a tool added later is announced by default rather
# than silently missing the courtesy.
FAST_TOOLS = frozenset({"get_diagnostic", "recall_memory", "search_history", "save_memory"})

# First check-in threshold and repeat cadence, from the original acceptance
# criteria: "If it exceeds 20 seconds... subsequent check-ins are at least 30
# seconds apart". The initial "start" event still fires at ZERO delay
# (unchanged); this pair only governs the ONGOING re-announce cadence for
# work that keeps running past the threshold.
CHECKIN_THRESHOLD_S = 20.0
CHECKIN_INTERVAL_S = 30.0


# ---------- trusted activity labels ----------------------------------------

# Built-in tool name -> a short, honest, human-facing label of WHAT is
# happening. Static and curated — never derived from the tool's raw name/
# arguments or the model's own prose, so the label can never leak reasoning
# or an unsafe/unbounded string. Extend this when a new built-in tool is
# added; an unmapped built-in tool falls back to GENERIC_TOOL_LABEL, same as
# an unmapped MCP server, so a forgotten entry degrades to "honest but vague"
# rather than silence or a raw tool-name leak.
TOOL_LABELS = {
    "web_search": "Searching the web",
    "fetch_page": "Reading a page",
    "fetch_youtube_transcript": "Reading a video transcript",
    "transcribe_audio_url": "Transcribing audio",
    "fetch_reddit_thread": "Reading a Reddit thread",
    "read_github_issues": "Checking GitHub",
    "read_github_file": "Checking GitHub",
    "read_github_pr": "Checking GitHub",
    "file_github_issue": "Checking GitHub",
    "comment_github_issue": "Checking GitHub",
    "reopen_github_issue": "Checking GitHub",
    "edit_github_issue": "Checking GitHub",
    "summon_claude_code": "Delegating to Claude Code",
}

# Fallback for a tool with no specific label — either a built-in tool missing
# from TOOL_LABELS above, or an MCP tool whose server has no configured label
# (config.py's mcp_servers[name]["label"]; see McpManager.activity_label).
# Deliberately generic rather than a guess: Crossband never learns what a
# third-party MCP server actually does beyond what its own operator tells it
# (backend/mcp_client.py's payload-agnostic contract) — an honest "working on
# it" beats fabricating a specific-sounding label for an unknown tool.
GENERIC_TOOL_LABEL = "Working on it"

# The delegated Claude Code guest job (backend/guestjobs.py) has no per-call
# tool batch to derive a label from — it's one long-running visit — so its
# ping uses a fixed label keyed on mode instead of the tool table above.
GUEST_JOB_LABELS = {
    "investigate": "Investigating the code",
    "implement": "Building the change",
}
GUEST_JOB_LABEL_DEFAULT = "Still working"


def is_announceable(tool_names):
    """True when at least one tool name in this batch is NOT in the fast/
    trivial set — i.e. this batch warrants a start acknowledgement and
    ongoing check-ins. An empty batch is never announceable."""
    return any(name not in FAST_TOOLS for name in tool_names)


def activity_label(tool_name, mcp=None):
    """Trusted, human-facing label for ONE tool call. Built-in tools resolve
    from TOOL_LABELS. An MCP tool (`mcp__<server>__<tool>`) resolves from
    that SERVER's own configured label via `mcp.activity_label` when an
    McpManager is supplied — the operator's own trusted description of what
    their server does, since Crossband itself never learns that. Anything
    else (an unmapped built-in tool, an MCP tool with no configured label, or
    no manager supplied) falls back to GENERIC_TOOL_LABEL."""
    if tool_name in TOOL_LABELS:
        return TOOL_LABELS[tool_name]
    if mcp is not None and tool_name.startswith("mcp__"):
        parts = tool_name.split("__", 2)
        if len(parts) >= 2:
            label = mcp.activity_label(parts[1])
            if label:
                return label
    return GENERIC_TOOL_LABEL


def batch_activity(tool_names, mcp=None):
    """The ONE label representing an announceable batch of concurrently
    dispatched tool calls (several may run at once within one turn) —
    the first non-FAST_TOOLS name's label, mirroring is_announceable's own
    "any" semantics so the two always agree on what triggered the
    announcement. Callers should only use this when is_announceable() is
    True; on an all-fast batch it falls back to GENERIC_TOOL_LABEL, which the
    caller should never actually emit."""
    for name in tool_names:
        if name not in FAST_TOOLS:
            return activity_label(name, mcp=mcp)
    return GENERIC_TOOL_LABEL


def guest_job_label(mode):
    """Fixed label for a still-running guest job's periodic ping, keyed on
    its mode (investigate/implement) — see GUEST_JOB_LABELS above."""
    return GUEST_JOB_LABELS.get(mode, GUEST_JOB_LABEL_DEFAULT)
