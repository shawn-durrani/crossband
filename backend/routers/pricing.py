"""Operator-editable rate cards.

Pricing fails CLOSED and a seat's promotion depends on known cost provenance.
Both work (`gpt-5.6-terra` correctly read as unpriced for hours), but until
now the app could tell you a model was unpriced, block its seat on that basis,
and then offer no way out except hand-editing `config.local.json` on the
server. This router is the way out.

WHERE OVERRIDES LIVE. `config.local.json` - the file `config.load_settings()`
already layers (`defaults < config.json < config.local.json < env`), re-reading
on EVERY call, so a saved card takes effect on the next round with no restart.
Deliberately not a new store: `db` imports `config`, so `config` cannot import
`db` without a cycle, and a DB-backed table would need its own merge point and
precedence rules. Writing the file the layering already reads adds neither.

WHAT MAY BE SAVED. A card needs `input`, `output`, a non-empty `source` and an
`as_of` date. A rate with no stated source is REFUSED. That rule is the whole
point of the surface: the pricing failures behind this rule were both a number
nobody transcribed standing in for one nobody had, and a form is a much faster
way to invent one than a text editor was. The UI may make pricing easy; it may
not make guessing easy.
"""

import contextlib
import json
import os
import shutil
import tempfile

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from .. import provenance as prov
from .. import ratecard
from ..config import (DEFAULT_PRICING, LOCAL_CONFIG_PATH, _read_json,
                      load_settings, price_for)

router = APIRouter(prefix="/api/pricing", tags=["pricing"])


def _refresh_settings(request):
    """Re-read config and republish it as the app's shared snapshot.

    app.state.settings is assigned once, at app.py:359, and every request-time
    consumer reads it: the benchmark, the voice routes, the attachment cap. A
    saved card has to reach those too, not only the round (which picks it up
    through tools.refresh_repo_maps) and not only this router's own response.
    """
    fresh = load_settings()
    try:
        request.app.state.settings = fresh
    except AttributeError:      # a bare app object in a unit test
        pass
    return fresh

# The two provenances an operator may attest on a static card.
# `provider_reported` and `subscription_equivalent` are DERIVED per turn
# from what a provider or subscription actually returned - allowing either to be
# hand-entered would let an estimate be relabelled as billed spend.
_EDITABLE_PROVENANCE = ratecard.EDITABLE_PROVENANCE


class CacheTerms(BaseModel):
    read_mult: float | None = None
    write_mult: float | None = None


class RateCardIn(BaseModel):
    """The wire shape. Everything is Optional here on purpose - the real rules
    live in `ratecard.validate_and_build`, which refuses with a
    plain-English reason a form can display, rather than pydantic's 422. A
    self-hosted declaration carries no rates at all, so `input`/`output` cannot
    be required at this layer."""
    input: float | None = None
    output: float | None = None
    source: str | None = None
    as_of: str | None = None
    cache: CacheTerms | None = None
    aliases: list[str] = Field(default_factory=list)
    # Which of the two operator-declarable provenances this is. Defaults to a
    # published estimate - the overwhelmingly common case, and the one the
    # earlier pricing surface hardcoded.
    provenance: str = prov.RATE_CARD_ESTIMATE


def _overrides() -> dict:
    """The `pricing` block currently written in config.local.json (only)."""
    local = _read_json(LOCAL_CONFIG_PATH)
    block = local.get("pricing")
    return dict(block) if isinstance(block, dict) else {}


def _write_overrides(block: dict) -> None:
    """Replace config.local.json's `pricing` block, preserving every other key.

    Atomic (temp file + os.replace) because this file is read on every
    load_settings() call - a torn write would not brick startup (_read_json
    swallows a malformed file) but WOULD silently drop every local setting the
    operator has, which is worse than a crash. A single `.bak` of the previous
    contents is kept alongside: atomicity guarantees the file
    is never half-written, it does not guarantee the NEW contents are what the
    operator wanted, and this is their personal config.
    """
    local = _read_json(LOCAL_CONFIG_PATH)
    if block:
        local["pricing"] = block
    else:
        local.pop("pricing", None)
    LOCAL_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(LOCAL_CONFIG_PATH.parent),
                               prefix=".config.local.", suffix=".json")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(local, f, indent=2, sort_keys=True)
            f.write("\n")
        if LOCAL_CONFIG_PATH.exists():
            with contextlib.suppress(OSError):
                shutil.copy2(LOCAL_CONFIG_PATH,
                             LOCAL_CONFIG_PATH.with_suffix(".json.bak"))
        os.replace(tmp, LOCAL_CONFIG_PATH)
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _card_row(model: str, effective: dict, overrides: dict) -> dict:
    """One model's effective card plus WHERE it came from, so the UI can tell a
    shipped default from an operator's own figure - never conflate the two."""
    card = price_for(model, effective)
    if card is None:
        return {"model": model, "origin": "unpriced", "card": None,
                "priced": False}
    return {
        "model": model,
        # `override` only when THIS model id is the one written locally; a card
        # reached via an alias still reports the id that actually carries it.
        "origin": "override" if model in overrides else "builtin",
        "priced": True,
        "card": {
            "input": card["input"], "output": card["output"],
            "as_of": card.get("as_of"), "source": card.get("source"),
            "provenance": card.get("provenance"),
            "cache": card.get("cache"),
            "aliases": list(card.get("aliases") or ()),
        },
    }


def _known_models(effective: dict) -> list[str]:
    """Every model worth showing a row for: the price table's own keys plus the
    models seats are actually configured to run - the second half is the point,
    since an UNPRICED configured model is exactly the row the operator came
    here to fix and it appears in no table."""
    from .. import db
    models = set(effective) | set(DEFAULT_PRICING)
    try:
        con = db.connect()
        try:
            rows = con.execute(
                "SELECT DISTINCT model FROM participants WHERE model != ''"
            ).fetchall()
            models |= {r["model"] for r in rows if r["model"]}
        finally:
            con.close()
    except Exception:
        # A pricing page must still render if the participants table is
        # unavailable - the built-in rows remain useful on their own.
        pass
    s = load_settings()
    for attr in ("anthropic_model", "openai_model", "utility_model"):
        m = getattr(s, attr, "") or ""
        if m:
            models.add(m)
    return sorted(models)


@router.get("")
def list_rate_cards():
    """Effective card for every known + configured model, tagged by origin."""
    effective = load_settings().pricing
    overrides = _overrides()
    rows = [_card_row(m, effective, overrides) for m in _known_models(effective)]
    return {
        "cards": rows,
        "unpriced": [r["model"] for r in rows if not r["priced"]],
        "editable_provenance": _EDITABLE_PROVENANCE,
    }


@router.put("/{model:path}")
def upsert_rate_card(model: str, body: RateCardIn, request: Request):
    """Validated by `ratecard.validate_and_build`, which is where every
    safety invariant is proven: only the two operator-declarable provenances,
    finite bounded rates, real ISO dates, an http(s) source for an estimate,
    and aliases that are exact ids colliding with no other priced model."""
    effective = load_settings().pricing
    # Every OTHER known id, for the alias-collision check - an alias must name a
    # model that has no card of its own, or two cards claim the same id.
    existing = {m for m in effective if m != model.strip()}
    payload = {
        "model_id": model,
        "provenance": body.provenance,
        "input": body.input,
        "output": body.output,
        "source": body.source,
        "as_of": body.as_of,
        "aliases": body.aliases,
        "cache_read_mult": body.cache.read_mult if body.cache else None,
        "cache_write_mult": body.cache.write_mult if body.cache else None,
    }
    try:
        model_id, card = ratecard.validate_and_build(payload, existing_ids=existing)
    except ratecard.RateCardError as e:
        # 400 with the module's own plain-English message - the form shows it
        # verbatim, so the reason a save was refused is never a bare 422.
        raise HTTPException(status_code=400, detail=str(e))
    block = _overrides()
    block[model_id] = card
    _write_overrides(block)
    return _card_row(model_id, _refresh_settings(request).pricing, _overrides())


@router.delete("/{model:path}")
def delete_rate_card(model: str, request: Request):
    """Drop an override. Falls back to the built-in card, or - honestly - to
    unpriced when there is no built-in. Never leaves a stale figure behind."""
    model = model.strip()
    block = _overrides()
    if model not in block:
        raise HTTPException(status_code=404,
                            detail=f"no local rate card for {model!r}")
    block.pop(model)
    _write_overrides(block)
    effective = _refresh_settings(request).pricing
    return _card_row(model, effective, _overrides())
