"""The citation check (#213): a cited source needs a tool row behind it.

The 2026-08-23 field failure: a seat wrote "Akai's own docs say" with no
fetch anywhere in the reply. The tools note already asks seats to cite the
sources they relied on; this makes the inverse visible. Same class as the
dispatch-claims rule (#172): a checkable real-world claim with nothing
behind it, on the citation side.

Informational only, mirroring the attribution audit: a hit becomes a quiet
chip on the message row, never a retry and never a block. A model may
legitimately cite from training or memory; the chip only says nothing was
fetched this turn. Any tool row in the reply skips the check entirely: the
seat did go and check something, and matching citations to specific tool
outputs would need semantics this layer refuses.

Pure module: the engine consumes it, tests drive it without I/O.
"""

import re

# Narrow on purpose: explicit the-source-says shapes only. "Check the docs"
# or "the docs cover this" name a place to look, so they never match.
_CITE_RES = [
    re.compile(r"\b(?:[A-Z][\w.-]*'s\s+)?(?:own\s+)?"
               r"(?:docs|documentation)\s+says?\b", re.IGNORECASE),
    re.compile(r"\bthe\s+manual\s+says?\b", re.IGNORECASE),
    re.compile(r"\brelease\s+notes\s+says?\b", re.IGNORECASE),
    re.compile(r"\baccording\s+to\s+(?:the\s+)?(?:[A-Z][\w.-]*'s\s+)?"
               r"(?:own\s+)?(?:docs|documentation|manual|website|spec|"
               r"changelog|release\s+notes)\b", re.IGNORECASE),
]

MAX_FINDINGS = 3


def _sentence_around(text, pos):
    start = max((text.rfind(b, 0, pos) for b in ".?!\n"), default=-1) + 1
    ends = [i for i in (text.find(b, pos) for b in ".?!\n") if i != -1]
    end = min(ends) + 1 if ends else len(text)
    return text[start:end].strip()


def uncited_claims(reply_text, tool_events):
    """[{kind: "citation", claim}] for each citation-shaped sentence in a
    reply that ran no tools, deduplicated, capped at MAX_FINDINGS."""
    if tool_events or not reply_text:
        return []
    seen = set()
    out = []
    for pat in _CITE_RES:
        for m in pat.finditer(reply_text):
            sentence = _sentence_around(reply_text, m.start())[:160]
            if not sentence or sentence in seen:
                continue
            seen.add(sentence)
            out.append({"kind": "citation", "claim": sentence})
            if len(out) >= MAX_FINDINGS:
                return out
    return out
