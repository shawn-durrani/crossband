"""`GET /api/app-links`: the owner's other apps, for the header's link row.

Behind the browser gate like every /api route outside the login surface.
The answer names the apps that answered their health probe on loopback,
each with the address a browser elsewhere can open it at and its loopback
address. The page chooses between them (frontend/src/appLinks.js).
"""

from fastapi import APIRouter, Request

router = APIRouter(tags=["app-links"])


@router.get("/api/app-links")
async def app_links(request: Request) -> dict:
    return {"app": "crossband",
            "apps": await request.app.state.sibling_probe.found()}
