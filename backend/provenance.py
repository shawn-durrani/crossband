"""Cost provenance vocabulary + model lifecycle.

A *calculated* dollar is not a *billed* one. This module is the single
definition of "how do we know this cost" and "is this model onboarded", shared
by the pricing table (config.py), the cost accounting (accounting.py), the
per-turn recorder (engine.py/guest.py) and the integrations view
(integrations.py). It has no imports of its own so anything may depend on it.

Two orthogonal axes:

  PROVENANCE - how a cost figure is known, per cost record:
    provider_reported         the provider returned an attributable monetary
                              cost (e.g. a Claude Code guest turn's
                              total_cost_usd on the metered API key). The only
                              provenance we call "billed".
    rate_card_estimate        computed from a dated, sourced local price table.
                              Always an ESTIMATE, never billed spend - a token
                              count times a published per-1M rate is not an
                              invoice.
    self_hosted_zero_marginal an explicit local/self-hosted declaration
                              (Ollama, LM Studio, …). No metered marginal cost -
                              which is NOT "free" in an absolute sense (hardware
                              and power are real, just out of scope) and, more
                              importantly, is NOT the same as "no data": a $0.00
                              here is a fact, not a gap.
    subscription_equivalent   API-equivalent usage covered by a subscription
                              (a Claude Code turn on a subscription or OAuth login). Informational,
                              never incremental cash.
    unknown                   no provenance record yet - the conservative
                              default. Shown apart, never folded into spend.

  LIFECYCLE - per model seat, how far along onboarding it is:
    trial       may be added and invoked MANUALLY for evaluation, but is not
                normal/selectable-by-default and is never eligible for future
                price-aware auto-selection. The conservative default for every
                seat, new or pre-existing.
    onboarded   has an explicit provenance record; treated as a normal seat and
                (once selection exists) eligible to be auto-picked.
"""

# ---- provenance states ----
PROVIDER_REPORTED = "provider_reported"
RATE_CARD_ESTIMATE = "rate_card_estimate"
SELF_HOSTED_ZERO_MARGINAL = "self_hosted_zero_marginal"
SUBSCRIPTION_EQUIVALENT = "subscription_equivalent"
UNKNOWN = "unknown"

PROVENANCE_STATES = (
    PROVIDER_REPORTED, RATE_CARD_ESTIMATE, SELF_HOSTED_ZERO_MARGINAL,
    SUBSCRIPTION_EQUIVALENT, UNKNOWN,
)

# The only provenance that counts as verifiable, incremental billed cash.
BILLED = frozenset({PROVIDER_REPORTED})

# Provenances that carry a REAL (possibly exactly-zero) tracked cost. Distinct
# from "not tracked": a self_hosted_zero_marginal $0.00 is a declared fact, an
# untracked source is a gap. Consumers must render the two differently.
TRACKED = frozenset({
    PROVIDER_REPORTED, RATE_CARD_ESTIMATE, SELF_HOSTED_ZERO_MARGINAL,
    SUBSCRIPTION_EQUIVALENT,
})

# Provenances whose dollar figure is an estimate rather than a billed amount.
_ESTIMATED = frozenset({RATE_CARD_ESTIMATE, SUBSCRIPTION_EQUIVALENT})

PROVENANCE_LABELS = {
    PROVIDER_REPORTED: "Provider-reported (billed)",
    RATE_CARD_ESTIMATE: "Rate-card estimate",
    SELF_HOSTED_ZERO_MARGINAL: "Self-hosted - no metered marginal cost",
    SUBSCRIPTION_EQUIVALENT: "Subscription-equivalent (informational)",
    UNKNOWN: "Unknown - no provenance recorded",
}

# ---- lifecycle states ----
TRIAL = "trial"
ONBOARDED = "onboarded"
LIFECYCLE_STATES = (TRIAL, ONBOARDED)


def is_billed(source) -> bool:
    return source in BILLED


def is_tracked(source) -> bool:
    return source in TRACKED


def estimated_flag(source):
    """True/False when we can say, else None (unknown)."""
    if source == UNKNOWN:
        return None
    return source in _ESTIMATED


def record(source=UNKNOWN, *, as_of=None, source_ref=None):
    """A JSON-able provenance snapshot. Attached to a per-turn cost record so a
    later rate-card edit can never rewrite history: the row keeps the provenance
    that was true when it was recorded."""
    source = source if source in PROVENANCE_STATES else UNKNOWN
    return {
        "source": source,
        "as_of": as_of,
        "source_ref": source_ref,
        "estimated": estimated_flag(source),
    }


def auto_participates(lifecycle) -> bool:
    """Whether a seat SPEAKS in a normal, unaddressed round without being
    explicitly invoked. Onboarded seats do - exactly as before.
    A `trial` seat does NOT: it is manual-invoke-only, reached by @mention or by
    being addressed by name. This is the real behavioral gate the lifecycle flag
    enforces on the round loop, and is deliberately distinct from
    `eligible_for_auto_selection` below (which concerns FUTURE price-aware
    picking among onboarded seats, not whether a trial seat runs at all)."""
    return lifecycle != TRIAL


def eligible_for_auto_selection(lifecycle, provenance_source) -> bool:
    """Whether a seat may be picked by FUTURE price-aware auto-selection. Only a
    fully onboarded seat with a known provenance qualifies; trial/unknown never
    do. This is only the derivation future work filters on; building the
    selection itself is out of scope for the provenance and lifecycle work
    here."""
    return lifecycle == ONBOARDED and provenance_source != UNKNOWN
