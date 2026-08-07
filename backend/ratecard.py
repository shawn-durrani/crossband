"""Validate + normalize an operator-authored rate card.

This surface began as a pull request authored by the in-app Claude Code guest
and was ported here intact. A second implementation of the same feature shipped
first with weaker inline validation, having been built without checking for the
open pull request; rather than discard the original, its invariants became the
ones this surface enforces. The one thing NOT taken from it: it made `source`
and `as_of` optional, and they stay REQUIRED for an estimate here, because an
unsourced, undated price is precisely the hole this validation exists to close.

This is the one place that turns a UI form payload into a stored rate-card dict
of exactly the shape config.py's `_rate_card`/`_self_hosted` produce, or refuses
it with a plain-English reason. Kept import-light (only `provenance` + a couple
of config constants) and FastAPI-free so it is unit-testable directly and can be
reused by any future manifest-driven onboarding surface without dragging in a
request.

Safety invariants proven HERE, not in the prompt or the form:

  · Only two provenance types are operator-declarable: `rate_card_estimate`
    (a dated, sourced published list price - always an ESTIMATE) and
    `self_hosted_zero_marginal` (a declared local $0). `provider_reported` and
    `subscription_equivalent` are DERIVED from what a provider/subscription
    actually returned per turn - they can never be hand-asserted as a static
    card, or a user could relabel an estimate as "billed".
  · Numbers are finite, non-negative, and bounded (a fat-fingered $1e9 rate is
    a mistake, not a price). Dates are real ISO dates. Sources for an estimate
    must be an http(s) URL you can actually check.
  · Aliases are EXACT model ids only and can never be broad: glob/pattern
    characters and whitespace are rejected, so an alias attaches this card to
    one named model and nothing else. price_for already matches aliases
    exactly (never by prefix); this makes an unsafe one impossible to even save.
  · No field here is or carries a secret - a rate card is public list-price
    metadata. Nothing in this module reads credentials or the environment.
"""

import math
import re
from datetime import date

from . import provenance
from .config import ANTHROPIC_CACHE

# The only provenance a human may attest on a static card (see module docstring).
EDITABLE_PROVENANCE = (
    provenance.RATE_CARD_ESTIMATE,
    provenance.SELF_HOSTED_ZERO_MARGINAL,
)

# A model id / alias: starts alphanumeric, then a conservative id charset. No
# glob metacharacters, no leading dot/dash, bounded length. This is what makes a
# "broad" alias impossible to save.
_MODEL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/+\- ]{0,199}$")
_GLOB = re.compile(r"[*?\[\]{}]")
_URL = re.compile(r"^https?://\S+$", re.I)

# A per-1M rate above this, or a cache multiplier above this, is a typo not a
# price - refuse it rather than silently record an absurd cost basis.
_MAX_RATE = 100_000.0
_MAX_MULT = 1_000.0


class RateCardError(ValueError):
    """A rate card the operator submitted is invalid; message is user-facing."""


def _num(value, field, *, maximum):
    if value is None or (isinstance(value, str) and not value.strip()):
        raise RateCardError(f"{field} is required.")
    try:
        n = float(value)
    except (TypeError, ValueError):
        raise RateCardError(f"{field} must be a number.")
    if not math.isfinite(n):
        raise RateCardError(f"{field} must be a finite number.")
    if n < 0:
        raise RateCardError(f"{field} cannot be negative.")
    if n > maximum:
        raise RateCardError(f"{field} looks too large ({n:g}); check the units.")
    return n


def validate_model_id(raw) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise RateCardError("Model ID is required - the exact id the provider uses.")
    mid = raw.strip()
    if _GLOB.search(mid):
        raise RateCardError(
            "Model ID must be an exact id, not a pattern - remove any * ? [ ] { } "
            "characters."
        )
    if not _MODEL_ID.match(mid):
        raise RateCardError(
            "Model ID has characters that aren't allowed in a model id. Use the "
            "exact id string the provider returns."
        )
    return mid


def _validate_as_of(raw, *, required):
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        if required:
            raise RateCardError("As-of date is required (YYYY-MM-DD).")
        return date.today().isoformat()
    s = str(raw).strip()
    try:
        date.fromisoformat(s)
    except ValueError:
        raise RateCardError("As-of date must be a real date in YYYY-MM-DD form.")
    return s


def validate_aliases(raw, model_id, existing_ids=()):
    """Exact, non-broad, deduped aliases. `existing_ids` is every OTHER known
    model id: an alias that collides with one is rejected, because two cards
    claiming the same id is ambiguous pricing."""
    if raw in (None, ""):
        return []
    if isinstance(raw, str):
        raise RateCardError("Aliases must be a list of exact model ids.")
    if not isinstance(raw, (list, tuple)):
        raise RateCardError("Aliases must be a list of exact model ids.")
    others = set(existing_ids or ())
    out, seen = [], set()
    for item in raw:
        alias = validate_model_id(item)  # same exactness rules as a model id
        if alias == model_id:
            raise RateCardError(
                f"Alias '{alias}' is the same as the model id - drop it."
            )
        if alias in seen:
            continue
        if alias in others:
            raise RateCardError(
                f"Alias '{alias}' is already a separate priced model; an alias "
                "must name a model that has no card of its own."
            )
        seen.add(alias)
        out.append(alias)
    return out


def validate_and_build(payload, existing_ids=()):
    """Return (model_id, card_dict) for a valid submission, else raise
    RateCardError. `card_dict` matches config._rate_card / _self_hosted exactly,
    so it drops straight into the pricing table. `existing_ids` should be every
    known model id EXCEPT the one being saved (for alias-collision checks)."""
    payload = payload or {}
    model_id = validate_model_id(payload.get("model_id"))

    prov = (payload.get("provenance") or "").strip()
    if prov not in EDITABLE_PROVENANCE:
        raise RateCardError(
            "Provenance must be either 'rate_card_estimate' (a dated, sourced "
            "published price) or 'self_hosted_zero_marginal' (a declared local "
            "$0). Provider-reported and subscription costs are recorded per turn "
            "from what the provider actually returned - they can't be entered as "
            "a fixed card."
        )

    aliases = validate_aliases(payload.get("aliases"), model_id, existing_ids)

    if prov == provenance.SELF_HOSTED_ZERO_MARGINAL:
        source = (payload.get("source") or "").strip()
        if not source:
            raise RateCardError(
                "Add a short source note for the self-hosted declaration "
                "(e.g. 'local (Ollama, self-hosted)') so it's auditable later."
            )
        return model_id, {
            "input": 0.0,
            "output": 0.0,
            "provenance": provenance.SELF_HOSTED_ZERO_MARGINAL,
            "as_of": _validate_as_of(payload.get("as_of"), required=False),
            "source": source,
            "cache": dict(ANTHROPIC_CACHE),
            "aliases": aliases,
        }

    # rate_card_estimate
    input_rate = _num(payload.get("input"), "Input rate (per 1M tokens)", maximum=_MAX_RATE)
    output_rate = _num(payload.get("output"), "Output rate (per 1M tokens)", maximum=_MAX_RATE)
    as_of = _validate_as_of(payload.get("as_of"), required=True)
    source = (payload.get("source") or "").strip()
    if not _URL.match(source):
        raise RateCardError(
            "Source must be an http(s) link to the provider's rate card, so the "
            "estimate can be checked and dated."
        )
    read_mult = _mult(payload.get("cache_read_mult"), "Cached-read multiplier",
                      ANTHROPIC_CACHE["read_mult"])
    write_mult = _mult(payload.get("cache_write_mult"), "Cached-write multiplier",
                       ANTHROPIC_CACHE["write_mult"])
    return model_id, {
        "input": input_rate,
        "output": output_rate,
        "provenance": provenance.RATE_CARD_ESTIMATE,
        "as_of": as_of,
        "source": source,
        "cache": {"read_mult": read_mult, "write_mult": write_mult},
        "aliases": aliases,
    }


def _mult(value, field, default):
    """A cache multiplier: optional (defaults to the provider default the form
    seeds), else a bounded non-negative number."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return float(default)
    return _num(value, field, maximum=_MAX_MULT)
