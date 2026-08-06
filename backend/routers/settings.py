from fastapi import APIRouter
from pydantic import BaseModel

from .. import db

router = APIRouter(prefix="/api/settings", tags=["settings"])


class SettingsIn(BaseModel):
    shared_instructions: str | None = None


@router.get("")
def get_settings():
    con = db.connect()
    out = {"shared_instructions": db.get_setting(con, "shared_instructions")}
    con.close()
    return out


@router.patch("")
def update_settings(body: SettingsIn):
    con = db.connect()
    if body.shared_instructions is not None:
        db.set_setting(con, "shared_instructions", body.shared_instructions)
    con.commit()
    out = {"shared_instructions": db.get_setting(con, "shared_instructions")}
    con.close()
    return out
