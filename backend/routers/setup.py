"""First-run setup: point-and-click key configuration for non-technical users.

GET  /api/setup/status  - per-service {configured, valid, detail}; no key material ever.
POST /api/setup/key     - validate a pasted key LIVE with a minimal cheap call, then
                          persist it to the repo's .env (chmod 600) AND os.environ so
                          it works without a restart.

Security invariants: keys are never logged, never echoed back, and never appear in
GET responses (booleans only). The app binds to 127.0.0.1, so this endpoint is
reachable only from the user's own machine.
"""

import os

import httpx
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from .. import diagnostics
from ..capabilities import CAPABILITIES
from ..config import ROOT

router = APIRouter(prefix="/api/setup", tags=["setup"])

ENV_PATH = ROOT / ".env"
VALIDATE_TIMEOUT = 15.0

# SERVICES + the live-validation cache now live in backend/diagnostics.py -
# the single source of truth shared with GET /api/setup/status, GET
# /api/models/status's sibling routes, and the get_diagnostic MCP tool
# (backend/diag_mcp.py). Aliased here so the rest of this module
# (POST /api/setup/key, below) reads unchanged.
SERVICES = diagnostics.SERVICES
_session_valid = diagnostics.session_valid


class KeyIn(BaseModel):
    service: str
    key: str
    secret: str | None = None  # reddit only (client id + secret)


# ---------- live validation (one minimal, cheap call per provider) ----------

def _friendly_http_error(name: str, status: int) -> str:
    if status in (401, 403):
        return (f"That key wasn't accepted by {name} - check you copied all of it "
                "(keys are long and easy to cut short).")
    return (f"{name} replied with an unexpected error (HTTP {status}). "
            "The key may still be fine - try again in a moment.")


async def _validate(service: str, key: str, secret: str | None) -> tuple[bool, str | None]:
    """Run a capability's declarative probe. Cheapest possible authenticated
    call; a 429 counts as valid (the key authenticated, we just got
    rate-limited). No probe means the credential is accepted without a live
    call (Reddit)."""
    cap = CAPABILITIES.get(service)
    if cap is None:
        return False, "Unknown service."
    probe = cap.get("probe")
    if not probe:
        return True, None

    def fill(v):
        if isinstance(v, str):
            return v.replace("{key}", key).replace("{secret}", secret or "")
        if isinstance(v, dict):
            return {k: fill(x) for k, x in v.items()}
        return v

    name = cap["name"].split(" (")[0]
    try:
        async with httpx.AsyncClient(timeout=VALIDATE_TIMEOUT) as client:
            r = await client.request(
                probe["method"], probe["url"],
                headers=fill(probe.get("headers")) or None,
                json=fill(probe.get("body")) or None,
                params=fill(probe.get("params")) or None)
    except httpx.HTTPError:
        return False, (f"Couldn't reach {name} - check your internet connection "
                       "and try again.")
    if r.status_code < 400 or r.status_code == 429:
        return True, None
    return False, _friendly_http_error(name, r.status_code)


# ---------- .env persistence (preserve every other line + comments) ----------

def write_env_var(path, name: str, value: str) -> None:
    """Create/update NAME=value in the .env file, preserving all other lines,
    comments and ordering. File is chmod 600 (owner-only) afterwards.

    A value carrying a newline would write extra assignments of the caller's
    choosing, so control characters are refused here rather than at each call
    site: this is the only function that writes the file."""
    if any(ch in value for ch in "\r\n\x00") or any(ch in name for ch in "\r\n\x00"):
        raise HTTPException(status_code=400, detail=(
            "value contains a line break; keys and secrets are single-line"))
    lines = path.read_text().splitlines() if path.exists() else []
    replaced = False
    for i, line in enumerate(lines):
        stripped = line.lstrip()
        for prefix in (f"{name}=", f"export {name}="):
            if stripped.startswith(prefix):
                lines[i] = f"{name}={value}"
                replaced = True
                break
        if replaced:
            break
    if not replaced:
        lines.append(f"{name}={value}")
    path.write_text("\n".join(lines) + "\n")
    os.chmod(path, 0o600)


# ---------- endpoints ----------

@router.get("/status")
async def setup_status(request: Request):
    return await diagnostics.health_status(request.app.state.memory)


@router.post("/key")
async def setup_key(body: KeyIn):
    svc = body.service.strip().lower()
    if svc not in SERVICES:
        raise HTTPException(400, f"Unknown service '{svc}'")
    key = body.key.strip()
    if not key:
        raise HTTPException(400, "Paste the key first.")
    secret = (body.secret or "").strip() or None
    if svc == "reddit" and not secret:
        return {"valid": False,
                "error": "Reddit needs both parts - the client id and the secret "
                         "from your reddit.com/prefs/apps 'script' app."}

    ok, error = await _validate(svc, key, secret)
    if not ok:
        _session_valid.pop(svc, None)
        return {"valid": False, "error": error}

    # persist + take effect immediately (no restart needed)
    values = dict(zip(SERVICES[svc]["env"], [key, secret]))
    for var, value in values.items():
        write_env_var(ENV_PATH, var, value)
        os.environ[var] = value
    _session_valid[svc] = True
    return {"valid": True, "unlocked": SERVICES[svc]["unlocked"]}
