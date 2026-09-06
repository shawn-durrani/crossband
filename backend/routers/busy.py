"""`GET /api/busy`: the deploy watcher's question before a restart (#343).

Answers `{"busy": <bool>, "reasons": [<fixed labels>]}` from in-process
state alone, so it is fast under any load. app.py's middleware lets a
loopback caller through without a session, the same posture as the
health probe on `/api/auth/session`: the watcher runs on this machine
and has no cookie jar. A trusted (tailnet) host still needs a session.
The labels come from backend/busy.py and say nothing about content.
"""

from fastapi import APIRouter

from .. import busy

router = APIRouter(tags=["busy"])


@router.get(busy.PATH)
def busy_state() -> dict:
    return busy.snapshot()
