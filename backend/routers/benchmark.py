"""The synthetic voice benchmark's surface (#94). Owner-only like every other
/api route (the browser gate middleware sits in front of all of them); the
work itself lives in backend/benchmark.py."""

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

from .. import benchmark, db, voice

router = APIRouter(prefix="/api/benchmark", tags=["benchmark"])


class RunIn(BaseModel):
    models: list[str] = []
    cases: list[str] = []
    dimensions: list[str] = []


def _enabled_seats() -> list:
    con = db.connect()
    rows = [dict(r) for r in con.execute(
        "SELECT * FROM participants WHERE enabled=1 ORDER BY position")]
    con.close()
    return rows


@router.get("")
def catalogue():
    """Everything the panel needs to build a run: the enabled seats, the
    scripted cases, the dimensions, and whether the voice legs can work."""
    fixture = benchmark.fixtures_root() / benchmark.FIXTURE_FILE
    return {
        "seats": [benchmark.seat_public(r) for r in _enabled_seats()],
        "cases": [{"id": c, "label": v["label"], "prompt": v["prompt"]}
                  for c, v in benchmark.CASES.items()],
        "dimensions": [{"id": d, "label": benchmark.DIMENSION_LABELS[d]}
                       for d in benchmark.DIMENSIONS],
        "eleven": voice.enabled(),
        "fixture_present": fixture.is_file(),
        "active_run_id": next(iter(benchmark._active), None),
    }


@router.post("/runs", status_code=202)
async def start(body: RunIn, request: Request):
    # async on purpose: engine.spawn needs the running loop, and one running
    # benchmark at a time is the point (overlap would time contention).
    if benchmark._active:
        raise HTTPException(409, "a benchmark is already running - one at a "
                                 "time keeps the timings honest")
    plan, error = benchmark.build_plan(
        body.model_dump(), _enabled_seats(), voice.enabled())
    if error:
        raise HTTPException(400, error)
    cfg = request.app.state.settings.as_cfg()
    return {"run_id": benchmark.start_run(plan, cfg)}


@router.get("/runs")
def runs():
    return {"runs": benchmark.list_runs()}


@router.get("/runs/{run_id}")
def run(run_id: str):
    results = benchmark.get_run(run_id)
    if results is None:
        raise HTTPException(404, "no such benchmark run")
    return results


@router.delete("/runs/{run_id}")
def remove(run_id: str):
    refusal = benchmark.delete_run(run_id)
    if refusal:
        raise HTTPException(409 if "still going" in refusal else 404, refusal)
    return {"ok": True}


@router.get("/runs/{run_id}/artefacts/{name}")
def artefact(run_id: str, name: str):
    """Retained audio, handed over for human inspection on request - the
    runner itself never plays anything."""
    path = benchmark.artefact_path(run_id, name)
    if path is None:
        raise HTTPException(404, "no such artefact")
    return FileResponse(path, media_type="audio/mpeg", filename=name)
