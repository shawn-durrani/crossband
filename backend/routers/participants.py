import re

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, field_validator

from .. import db, provenance
from .. import providers
from ..config import load_settings, provenance_for
from ..engine import slugify

router = APIRouter(prefix="/api/participants", tags=["participants"])

_HEX_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")


def _reasoning_error(provider):
    """The 400 message for an unsupported/misspelled reasoning_effort —
    provider-aware so it correctly tells an OpenAI seat "adaptive" isn't a
    thing for it, rather than a generic complaint."""
    choices = [c or "Default" for c in providers.reasoning_choices(provider)]
    return (f"reasoning_effort must be one of: {', '.join(choices)} "
           f"for provider '{provider}'")


class ParticipantIn(BaseModel):
    name: str | None = None
    provider: str | None = None
    model: str | None = None
    base_url: str | None = None
    api_key_env: str | None = None
    system_prompt: str | None = None
    color: str | None = None
    enabled: bool | None = None
    voice_id: str | None = None
    voice_gain: float | None = None  # playback volume, clamped 0.2–1.0
    reasoning_effort: str | None = None
    lifecycle: str | None = None  # 'trial' | 'onboarded'

    @field_validator("voice_gain")
    @classmethod
    def _clamp_gain(cls, v):
        return v if v is None else min(1.0, max(0.2, v))

    @field_validator("lifecycle")
    @classmethod
    def _valid_lifecycle(cls, v):
        if v is not None and v not in provenance.LIFECYCLE_STATES:
            raise ValueError("lifecycle must be 'trial' or 'onboarded'")
        return v

    @field_validator("color")
    @classmethod
    def _valid_hex(cls, v):
        # color lands in a CSS value; only accept #rrggbb so nothing else can be
        # smuggled into the stylesheet (e.g. a url() exfil ping)
        if v is not None and not _HEX_COLOR.match(v):
            raise ValueError("color must be a #rrggbb hex string")
        return v


@router.post("")
def create_participant(body: ParticipantIn):
    if not body.name or not body.model or body.provider not in ("anthropic", "openai"):
        raise HTTPException(400, "name, model and provider ('anthropic' or 'openai') are required")
    # Reject an unsupported/misspelled reasoning_effort up front — provider-
    # aware, since "adaptive" only exists for Anthropic. A silently-dropped value
    # here used to mean the saved policy quietly never reached the provider call.
    if not providers.valid_reasoning_effort(body.provider, body.reasoning_effort):
        raise HTTPException(400, _reasoning_error(body.provider))
    con = db.connect()
    slug = base_slug = slugify(body.name)
    n = 2
    while con.execute("SELECT 1 FROM participants WHERE slug=?", (slug,)).fetchone():
        slug = f"{base_slug}-{n}"
        n += 1
    pos = con.execute("SELECT COALESCE(MAX(position),0)+1 FROM participants").fetchone()[0]
    cur = con.execute(
        "INSERT INTO participants(slug, name, provider, model, base_url, api_key_env, "
        "system_prompt, color, reasoning_effort, position, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (slug, body.name, body.provider, body.model, body.base_url or None,
         body.api_key_env or None, body.system_prompt or "", body.color or "#a78bfa",
         body.reasoning_effort or "", pos, db.now()),
    )
    # newcomers only join NEW chats by default; existing rosters are untouched
    con.commit()
    row = con.execute("SELECT * FROM participants WHERE id=?", (cur.lastrowid,)).fetchone()
    con.close()
    return dict(row)


@router.patch("/{pid}")
def update_participant(pid: int, body: ParticipantIn):
    con = db.connect()
    row = con.execute("SELECT * FROM participants WHERE id=?", (pid,)).fetchone()
    if not row:
        con.close()
        raise HTTPException(404)
    updates = {k: v for k, v in body.model_dump().items() if v is not None}
    if "provider" in updates and updates["provider"] not in ("anthropic", "openai"):
        con.close()
        raise HTTPException(400, "provider must be 'anthropic' or 'openai'")
    if "reasoning_effort" in updates:
        # Validate against the EFFECTIVE provider (a PATCH may change
        # provider and reasoning_effort together, or set effort alone against
        # the seat's existing provider) — "adaptive" only ever validates for
        # Anthropic, on either path.
        effective_provider = updates.get("provider", row["provider"])
        if not providers.valid_reasoning_effort(effective_provider, updates["reasoning_effort"]):
            con.close()
            raise HTTPException(400, _reasoning_error(effective_provider))
    if "enabled" in updates:
        updates["enabled"] = int(updates["enabled"])
    # Onboarding gate: a seat may become 'onboarded' only once an
    # explicit provenance record exists — never a silent upgrade of an unsourced
    # model. Trial stays freely settable (manual evaluation is always allowed).
    if updates.get("lifecycle") == provenance.ONBOARDED:
        model = updates.get("model") or row["model"]
        pricing = load_settings().pricing
        # Resolve provenance for the SEAT, not just the model id: a model
        # served from a keyless loopback endpoint is self-hosted, so it has a
        # known ($0 marginal) cost even though no rate card names it. Read the
        # incoming values first — a single PATCH may point the seat at a local
        # endpoint and promote it in one go.
        source = provenance_for(
            model, pricing,
            base_url=updates.get("base_url", row["base_url"]),
            api_key_env=updates.get("api_key_env", row["api_key_env"]),
        )["source"]
        if source == provenance.UNKNOWN:
            con.close()
            raise HTTPException(
                409,
                f"Cannot onboard '{model}': no cost-provenance record. Add a "
                "priced rate-card entry or an explicit self-hosted declaration "
                "first — until then this model stays a manual trial.")
    if updates:
        sets = ", ".join(f"{k}=?" for k in updates)
        con.execute(f"UPDATE participants SET {sets} WHERE id=?", (*updates.values(), pid))
        con.commit()
    row = con.execute("SELECT * FROM participants WHERE id=?", (pid,)).fetchone()
    con.close()
    return dict(row)


@router.delete("/{pid}")
def delete_participant(pid: int):
    con = db.connect()
    n = con.execute("SELECT COUNT(*) FROM participants").fetchone()[0]
    if n <= 1:
        con.close()
        raise HTTPException(400, "At least one participant must remain")
    con.execute("DELETE FROM participants WHERE id=?", (pid,))
    con.commit()
    con.close()
    return {"ok": True}
