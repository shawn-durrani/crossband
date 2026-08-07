from fastapi import APIRouter, HTTPException, Request

from .. import diagnostics, providers

router = APIRouter(prefix="/api/models", tags=["models"])


@router.get("")
def list_models(provider: str, api_key_env: str | None = None, base_url: str | None = None):
    """Live model listing from the provider's API. Runs in the threadpool (sync
    endpoint) so the network call never blocks the event loop."""
    try:
        return {"models": providers.list_models(provider, api_key_env or None, base_url or None)}
    except ValueError as e:
        raise HTTPException(400, str(e))
    except RuntimeError as e:  # missing key env
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(502, f"Could not list models: {e}")


@router.get("/status")
def models_status(request: Request):
    """Read-only answer to 'what model is each participant ACTUALLY running?'
    For every participant it reports three raw provider model IDs so drift is a
    glance, never a DB query:

      • configured - the live participants-row model (what the NEXT turn uses),
        which is authoritative over config.json's stale first-run seed.
      • last_used  - the model stamped on that participant's most recent
        completed message (usage_json.model). This is what actually produced
        their last reply; it lags `configured` until the next turn runs.
      • seed       - the config.json first-run seed for the two default seats
        (null for others). `seed_drift` flags when it disagrees with live.

    It also surfaces each seat's onboarding lifecycle and derived cost
    provenance so the Participants UI can SHOW trial vs onboarded, warn
    that a trial seat is manual-invoke-only (it sits out unaddressed rounds -
    never a silent disappearance), and offer a promotion path:

      • lifecycle    - 'trial' or 'onboarded' (defaults to trial for any row
        without an explicit value; never a silent upgrade).
      • cost_provenance / cost_provenance_label - how this model's cost is known.
      • onboardable  - whether a trial seat COULD be promoted right now, i.e. it
        has a known provenance record (the exact gate PATCH …/lifecycle enforces).
        A trial seat with unknown provenance shows why promotion is blocked
        instead of failing on save.
      • eligible_for_auto_selection - onboarded AND known provenance (the flag
        future price-aware selection filters on; building that selection is
        out of scope here).

    Raw IDs only; no aliasing. Purely observational - nothing here changes how
    a model is selected or edited.

    The actual work lives in diagnostics.participants_status (Request-free) -
    shared with the get_diagnostic MCP tool's "models" diagnostic, so there is
    one implementation, not two."""
    settings = request.app.state.settings
    seeds = {"claude": settings.anthropic_model, "gpt": settings.openai_model}
    out = diagnostics.participants_status(settings.pricing, seeds)
    return {"participants": out}
