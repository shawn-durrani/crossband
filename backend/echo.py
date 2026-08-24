"""The echo guard (#210): restatement gets one retry, then suppression.

The 2026-08-23 field failure: a seat delivered a near-copy of content the
chat already held, and the 2026-08-17 incident record had already found
that no echo or restatement guard exists in the live path. The prompt layer
forbids restating and has been through two live iterations
(test_silence_rule.py, test_silence_default.py); prose alone does not hold.
Same shape as passes.py: make the rule a move the APP enforces - one retry
with the guard stated, then suppression.

Scope, deliberately narrow: word-shingle overlap catches copied and lightly
reworded restatement. A compressed paraphrase of the same content passes it;
semantic judgement stays with the model. Quoted material is stripped before
judging, so quoting a line in order to answer it is never a hit.

Pure module: the engine consumes it, tests drive it without I/O.
"""

import re

# A reply shorter than this is never judged. Short agreements ("Yep - what
# Claude said.") are a sanctioned move in the round-position rule, and tiny
# texts make an overlap ratio meaningless.
MIN_REPLY_CHARS = 240

# Word shingles: small enough to survive light rewording, large enough that
# shared stock phrases do not count as restatement.
SHINGLE_WORDS = 4

# At least this fraction of the reply's own shingles must already appear in
# ONE reference text. Half a long reply being verbatim-present is
# restatement even when the rest is fresh framing ("Anyway, as I said: ...").
CONTAINMENT_THRESHOLD = 0.55

# Below this many shingles the ratio is too coarse to trust.
MIN_SHINGLES = 10

OWN_LABEL = "your own previous message"

RETRY_NOTE = (
    "Your reply was not delivered: it mostly restated {what}, and a "
    "restatement is never worth a turn. Reply again with something "
    "genuinely new - a fact, a correction, a real disagreement - or reply "
    "with exactly [pass] and the round simply moves on without you. On "
    "this retry a pass is always accepted."
)

# A user turn that asks for repetition makes restating the point of the
# round. Deliberately broad: skipping the guard for one round costs almost
# nothing, while enforcing against a requested repeat fights the user.
_REPEAT_REQUEST_RE = re.compile(
    r"\b(repeat|again|once more|one more time)\b", re.IGNORECASE)


def requested_repeat(user_text):
    return bool(_REPEAT_REQUEST_RE.search(user_text or ""))


# Explicitly quoted material is the seat engaging with a line, not
# restating it: markdown blockquote lines, and short spans inside straight
# or curly double quotes.
_BLOCKQUOTE_RE = re.compile(r"^[ \t]*>.*$", re.MULTILINE)
_QUOTED_SPAN_RE = re.compile(r"[\"“][^\"“”]{1,400}[\"”]")


def _strip_quoted(text):
    text = _BLOCKQUOTE_RE.sub(" ", text or "")
    return _QUOTED_SPAN_RE.sub(" ", text)


def _shingles(text):
    words = re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).split()
    n = SHINGLE_WORDS
    return {" ".join(words[i:i + n]) for i in range(len(words) - n + 1)}


def find_restated(reply, references):
    """The label of the first reference `reply` mostly restates, else None.

    `references` is a list of (label, text) pairs. Each reference is judged
    on its own: overlap against a concatenation would let two half-matches
    fake one restatement."""
    judged = _strip_quoted(reply or "")
    if len(judged.strip()) < MIN_REPLY_CHARS:
        return None
    reply_shingles = _shingles(judged)
    if len(reply_shingles) < MIN_SHINGLES:
        return None
    for label, text in references:
        ref = _shingles(text)
        if not ref:
            continue
        contained = len(reply_shingles & ref) / len(reply_shingles)
        if contained >= CONTAINMENT_THRESHOLD:
            return label
    return None


def references_for(transcript, self_slug, roster_slugs, names):
    """(label, text) pairs to judge a completed reply against: the seat's
    own most recent visible message, and each roster reply that followed the
    newest user turn (the replies this round has already collected).
    Metadata rows (system notices, external feeds) are never references."""
    refs = []
    own = [m for m in transcript if m.get("speaker") == self_slug]
    if own:
        refs.append((OWN_LABEL, own[-1].get("content") or ""))
    user_ids = [m["id"] for m in transcript if m.get("speaker") == "user"]
    if user_ids:
        last_user = user_ids[-1]
        for m in transcript:
            if (m["id"] > last_user and m.get("speaker") != self_slug
                    and m.get("speaker") in roster_slugs):
                who = names.get(m["speaker"], m["speaker"])
                refs.append((f"{who}'s reply just above", m.get("content") or ""))
    return refs
