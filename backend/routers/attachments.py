import logging
import os
import uuid

from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

from .. import attachments as att_mod
from .. import db, images
from .. import tools as tools_mod

log = logging.getLogger("crossband.attachments")
router = APIRouter(tags=["attachments"])


@router.post("/api/attachments")
async def upload_attachment(request: Request, file: UploadFile = File(...)):
    cfg = request.app.state.settings.as_cfg()
    cap = cfg["max_attachment_mb"] * 1024 * 1024
    # reject by declared length BEFORE buffering (a multi-GB body must not
    # reach RAM), then stream-read with the same cap as the backstop
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > cap + 8192:
        raise HTTPException(413, f"File exceeds {cfg['max_attachment_mb']} MB limit")
    chunks, total = [], 0
    while chunk := await file.read(1024 * 1024):
        total += len(chunk)
        if total > cap:
            raise HTTPException(413, f"File exceeds {cfg['max_attachment_mb']} MB limit")
        chunks.append(chunk)
    data = b"".join(chunks)
    filename = os.path.basename(file.filename or "file")
    mime = file.content_type or "application/octet-stream"
    # Downscale photos BEFORE the type gate: this is also what makes HEIC
    # (iPhone's default, and not a format any provider accepts) usable - it
    # comes out the other side as JPEG. Every turn re-sends every image,
    # so the size we store here is paid again on every future message.
    shrunk = images.downscale(data, mime, filename)
    if shrunk:
        log.info("attachment downscaled: %s %.1fMB -> %.1fMB (%dx%d)",
                 filename, len(data) / 1e6, len(shrunk["data"]) / 1e6,
                 shrunk["width"], shrunk["height"])
        data, mime, filename = shrunk["data"], shrunk["mime"], shrunk["filename"]
    if att_mod.kind_of(filename, mime) is None:
        raise HTTPException(
            415,
            "Unsupported file type. Supported: images (PNG/JPEG/GIF/WebP), PDFs, "
            "and plain-text/code files.",
        )
    stored = f"{uuid.uuid4().hex}_{filename}"
    os.makedirs(db.ATTACH_DIR, exist_ok=True)
    with open(os.path.join(db.ATTACH_DIR, stored), "wb") as f:
        f.write(data)
    con = db.connect()
    cur = con.execute(
        "INSERT INTO attachments(message_id, filename, stored_name, mime, size, created_at) "
        "VALUES(NULL,?,?,?,?,?)",
        (filename, stored, mime, len(data), db.now()),
    )
    con.commit()
    row = con.execute("SELECT * FROM attachments WHERE id=?", (cur.lastrowid,)).fetchone()
    con.close()
    return dict(row)


class YoutubeTranscriptIn(BaseModel):
    url: str


@router.post("/api/youtube-transcript")
def youtube_transcript_attachment(body: YoutubeTranscriptIn):
    """Fetch a FULL YouTube transcript and store it as a text attachment
    (document), so the whole thing reaches the models untouched by the
    tool-output trim - up to the attachment text limit. Returns the attachment
    row; the composer attaches it to your message."""
    try:
        video_id, text = tools_mod.youtube_transcript_text(body.url)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if not text:
        raise HTTPException(422, "No transcript text found for that video")
    filename = f"youtube-{video_id}-transcript.txt"
    stored = f"{uuid.uuid4().hex}_{filename}"
    data = f"YouTube transcript - https://www.youtube.com/watch?v={video_id}\n\n{text}".encode("utf-8")
    os.makedirs(db.ATTACH_DIR, exist_ok=True)
    with open(os.path.join(db.ATTACH_DIR, stored), "wb") as f:
        f.write(data)
    con = db.connect()
    cur = con.execute(
        "INSERT INTO attachments(message_id, filename, stored_name, mime, size, created_at) "
        "VALUES(NULL,?,?,?,?,?)",
        (filename, stored, "text/plain", len(data), db.now()),
    )
    con.commit()
    row = con.execute("SELECT * FROM attachments WHERE id=?", (cur.lastrowid,)).fetchone()
    con.close()
    return {"attachment": dict(row), "chars": len(text), "video_id": video_id}


@router.get("/api/attachments/{att_id}/file")
def get_attachment_file(att_id: int):
    con = db.connect()
    row = con.execute("SELECT * FROM attachments WHERE id=?", (att_id,)).fetchone()
    con.close()
    if not row:
        raise HTTPException(404)
    # explicit attachment disposition: stored files must never execute as a
    # page in the app's origin, whatever their claimed MIME (<img> still works)
    return FileResponse(att_mod.file_path(dict(row)), media_type=row["mime"],
                        filename=row["filename"],
                        content_disposition_type="attachment")
