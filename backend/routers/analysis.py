"""`/api/analysis`: the Analysis page (#407). The work lives in
backend/analysis.py; this file is the HTTP surface and its gate.

Every route here needs a signed-in owner session, before and after a
password is enrolled. The app's middleware already holds everything past
the login surface to a session once a password exists; before then,
loopback is open, and a Claude Code guest runs on loopback. A measurement
can spend money and one reads your chat history, so "open because nobody
enrolled yet" isn't good enough here. The machine side-channel's bearer is
not a session either, so a producer can't start one.

A run is named by its measurement id and a practice flag, and nothing else
is accepted: a request that brings any other field, such as a flag or a
path, is refused before the table is even read.
"""

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse

from .. import analysis, auth
from . import voice as voice_router

RUN_FIELDS = {"measurement", "practice"}


def owner_only(request: Request) -> None:
    if auth.request_session_ok(request):
        return
    if request.app.state.auth_enrolled:
        raise HTTPException(401, "not authenticated")
    raise HTTPException(401, "Set an owner password first. Measurements can "
                             "spend money and read your chats, so they only "
                             "run for a signed-in owner.")


router = APIRouter(prefix="/api/analysis", tags=["analysis"],
                   dependencies=[Depends(owner_only)])


def _voice_live() -> bool:
    return bool(voice_router.capture_sessions())


@router.get("")
def overview() -> dict:
    """The measurements with what each costs and touches, anything running,
    and the stored runs newest first."""
    return {"measurements": analysis.catalogue(_voice_live()),
            "runs": analysis.list_runs()}


@router.post("/runs", status_code=202)
async def start(request: Request) -> dict:
    # async on purpose: the run is a task on this loop.
    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(400, "send {\"measurement\": <id>}")
    if not isinstance(body, dict):
        raise HTTPException(400, "send {\"measurement\": <id>}")
    extra = sorted(set(body) - RUN_FIELDS)
    if extra:
        raise HTTPException(400, "A run from the Analysis page takes no "
                                 f"options ({', '.join(extra)}). Run the "
                                 "harness from a terminal for anything else.")
    practice = body.get("practice", False)
    if not isinstance(practice, bool):
        raise HTTPException(400, "practice is true or false")
    try:
        record = analysis.start(
            str(body.get("measurement") or ""), practice=practice,
            voice_live=_voice_live(),
            memory_url=request.app.state.settings.memory_url)
    except LookupError:
        raise HTTPException(404, "no such measurement")
    except analysis.Busy as e:
        raise HTTPException(409, str(e))
    except analysis.Refused as e:
        raise HTTPException(400, str(e))
    return record


@router.get("/runs/{run_id}")
def run(run_id: str) -> dict:
    found = analysis.report_text(run_id)
    if found is None:
        raise HTTPException(404, "no such run")
    record, report = found
    return {"run": record, "report": report}


@router.get("/runs/{run_id}/report.json")
def report_json(run_id: str):
    path = analysis.report_json_path(run_id)
    if path is None:
        raise HTTPException(404, "no report for that run")
    return FileResponse(path, media_type="application/json",
                        filename=f"{run_id}.json")


@router.post("/runs/{run_id}/stop")
async def stop(run_id: str) -> dict:
    refusal = analysis.stop(run_id)
    if refusal:
        raise HTTPException(409, refusal)
    return {"ok": True}


@router.delete("/runs/{run_id}")
def remove(run_id: str) -> dict:
    refusal = analysis.delete_run(run_id)
    if refusal:
        raise HTTPException(409 if "still going" in refusal else 404, refusal)
    return {"ok": True}
