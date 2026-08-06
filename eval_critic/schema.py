"""Fixture + result data model for the  critic eval harness.

A fixture captures exactly the prompt components the critic would have at
generation time in the live path — recent conversation, standing memory
summary, ambient recalled cards, and the draft response being checked — plus
the ground truth the harness scores against. See eval_critic/README.md for
the full schema description and the evidence-precedence policy.
"""

from dataclasses import dataclass, field

VERDICTS = ("allow", "contradicted", "unsupported")
EVIDENCE_SECTIONS = ("summary", "card", "conversation", "none")
FAILURE_POLICIES = ("fail_open", "fail_closed")


class FixtureError(ValueError):
    """A fixture file/object failed schema validation."""


@dataclass
class Fixture:
    id: str
    category: str
    expected_verdict: str
    draft_response: str
    # Prompt components available at generation time (architectural
    # constraint from : the critic must see BOTH the summary and cards).
    recent_conversation: list[dict] = field(default_factory=list)
    standing_summary: str = ""
    ambient_cards: list[dict] = field(default_factory=list)
    # Ground truth / bookkeeping.
    expected_evidence_span: str = ""
    # Optional: which section + exact quote a correct critic would cite. Not
    # required from a real critic (it finds its own citation) — used by the
    # mock critic (eval_critic/mock.py) and by fixture self-tests to build/
    # check a fully-grounded example answer.
    expected_evidence_section: str = ""
    expected_evidence_quote: str = ""
    is_control: bool = False
    author_model_family: str = "unspecified"
    notes: str = ""

    @staticmethod
    def from_dict(d: dict, source: str = "<unknown>") -> "Fixture":
        missing = [k for k in ("id", "category", "expected_verdict", "draft_response")
                   if not d.get(k)]
        if missing:
            raise FixtureError(f"{source}: fixture missing required field(s) {missing}")
        if d["expected_verdict"] not in VERDICTS:
            raise FixtureError(
                f"{source}: fixture {d['id']!r} has expected_verdict "
                f"{d['expected_verdict']!r} not in {VERDICTS}")
        if d["expected_verdict"] != "allow" and not d.get("expected_evidence_span"):
            raise FixtureError(
                f"{source}: fixture {d['id']!r} expects a non-allow verdict but has "
                "no expected_evidence_span (the claim/span justifying it) —  requires one")
        return Fixture(
            id=d["id"],
            category=d["category"],
            expected_verdict=d["expected_verdict"],
            draft_response=d["draft_response"],
            recent_conversation=d.get("recent_conversation") or [],
            standing_summary=d.get("standing_summary") or "",
            ambient_cards=d.get("ambient_cards") or [],
            expected_evidence_span=d.get("expected_evidence_span") or "",
            expected_evidence_section=d.get("expected_evidence_section") or "",
            expected_evidence_quote=d.get("expected_evidence_quote") or "",
            is_control=bool(d.get("is_control", False)),
            author_model_family=d.get("author_model_family") or "unspecified",
            notes=d.get("notes") or "",
        )


@dataclass
class ParsedVerdict:
    verdict: str | None
    claim_span: str = ""
    evidence_section: str = "none"
    evidence_quote: str = ""
    malformed: bool = False
    malformed_reason: str | None = None
    raw: str = ""


@dataclass
class FixtureResult:
    fixture_id: str
    category: str
    is_control: bool
    author_model_family: str
    critic_model: str
    critic_model_family: str
    expected_verdict: str
    parsed: ParsedVerdict
    failure_mode: str          # "ok" | "timeout" | "missing_key" | "malformed"
    applied_verdict: str       # verdict actually used for scoring, after failure policy
    outcome: str                # "TP" | "FP" | "TN" | "FN"
    verdict_exact_match: bool   # applied_verdict == expected_verdict exactly
    input_tokens: int
    output_tokens: int
    latency_s: float
    cost_usd: float | None
    exceeded_latency_budget: bool
