from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .. import db

router = APIRouter(prefix="/api/projects", tags=["projects"])


class ProjectIn(BaseModel):
    name: str | None = None
    instructions: str | None = None
    memory: str | None = None


@router.post("")
def create_project(body: ProjectIn):
    con = db.connect()
    cur = con.execute(
        "INSERT INTO projects(name, instructions, created_at) VALUES(?,?,?)",
        (body.name or "New project", body.instructions or "", db.now()),
    )
    con.commit()
    row = con.execute("SELECT * FROM projects WHERE id=?", (cur.lastrowid,)).fetchone()
    con.close()
    return dict(row)


@router.patch("/{project_id}")
def update_project(project_id: int, body: ProjectIn):
    con = db.connect()
    row = con.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
    if not row:
        con.close()
        raise HTTPException(404)
    updates = {k: v for k, v in body.model_dump().items() if v is not None}
    if updates:
        sets = ", ".join(f"{k}=?" for k in updates)
        con.execute(f"UPDATE projects SET {sets} WHERE id=?", (*updates.values(), project_id))
        con.commit()
    row = con.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
    con.close()
    return dict(row)


@router.delete("/{project_id}")
def delete_project(project_id: int):
    con = db.connect()
    con.execute("DELETE FROM projects WHERE id=?", (project_id,))
    con.commit()
    con.close()
    return {"ok": True}
