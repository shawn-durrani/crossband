"""Strict verdict parsing (Refs : "only a schema-valid verdict ... should
affect release; malformed output should follow the separately defined
timeout/failure policy").

Rejects anything that isn't exactly one JSON object with the four required
keys and valid enum values, and — for a non-allow verdict — requires the
cited evidence_quote to actually appear in the DATA section it claims to come
from (a hallucinated citation is treated as malformed, not trusted)."""

import json
import re

from eval_critic.schema import EVIDENCE_SECTIONS, VERDICTS, Fixture, ParsedVerdict

_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)
_FIRST_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


def _section_text(fixture: Fixture, section: str) -> str:
    if section == "summary":
        return fixture.standing_summary
    if section == "card":
        return "\n".join(str(c.get("content", "")) for c in fixture.ambient_cards)
    if section == "conversation":
        return "\n".join(str(t.get("content", "")) for t in fixture.recent_conversation)
    return ""


def parse_verdict(raw: str, fixture: Fixture) -> ParsedVerdict:
    text = (raw or "").strip()
    stripped = _FENCE_RE.sub("", text).strip()

    obj = None
    try:
        obj = json.loads(stripped)
    except (json.JSONDecodeError, TypeError):
        m = _FIRST_OBJECT_RE.search(stripped)
        if m:
            try:
                obj = json.loads(m.group(0))
            except (json.JSONDecodeError, TypeError):
                obj = None

    if not isinstance(obj, dict):
        return ParsedVerdict(verdict=None, malformed=True,
                              malformed_reason="not a single JSON object", raw=text)

    required = ("verdict", "claim_span", "evidence_section", "evidence_quote")
    missing = [k for k in required if k not in obj]
    if missing:
        return ParsedVerdict(verdict=None, malformed=True,
                              malformed_reason=f"missing key(s) {missing}", raw=text)

    verdict = obj.get("verdict")
    if verdict not in VERDICTS:
        return ParsedVerdict(verdict=None, malformed=True,
                              malformed_reason=f"verdict {verdict!r} not in {VERDICTS}", raw=text)

    section = obj.get("evidence_section")
    if section not in EVIDENCE_SECTIONS:
        return ParsedVerdict(verdict=None, malformed=True,
                              malformed_reason=f"evidence_section {section!r} not in "
                                                f"{EVIDENCE_SECTIONS}", raw=text)

    claim_span = str(obj.get("claim_span") or "")
    quote = str(obj.get("evidence_quote") or "")

    if verdict != "allow" and not claim_span:
        return ParsedVerdict(verdict=None, malformed=True,
                              malformed_reason="non-allow verdict cited no claim_span "
                                                "(the offending claim must be localized —  "
                                                "needs this for surgical correction)", raw=text)

    if verdict == "contradicted":
        # A contradiction MUST point at the specific fact it conflicts with —
        # "none" isn't a valid contradiction source.
        if section == "none" or not quote:
            return ParsedVerdict(verdict=None, malformed=True,
                                  malformed_reason="contradicted verdict cited no evidence "
                                                    "section/quote", raw=text)
    if verdict != "allow" and section != "none":
        # "unsupported" may legitimately cite "none" (checked everywhere, found
        # nothing) OR point at a source it's explaining away (e.g. the
        # lexically-similar-but-irrelevant-card case) — either way, if a real
        # section is named the quote must actually be grounded there.
        source_text = _section_text(fixture, section)
        if quote not in source_text:
            return ParsedVerdict(verdict=None, malformed=True,
                                  malformed_reason=f"evidence_quote not found verbatim in "
                                                    f"{section!r} section (unsupported "
                                                    "citation — likely hallucinated)", raw=text)

    return ParsedVerdict(verdict=verdict, claim_span=claim_span, evidence_section=section,
                          evidence_quote=quote, malformed=False, raw=text)
