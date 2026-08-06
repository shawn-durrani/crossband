"""Provider-export import: upload a Claude.ai / ChatGPT data export, stream
progress as SSE while chats land locally and Membro is seeded through the
contract. The response format matches the round loop's event stream so the
frontend reuses streamSSE."""

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import StreamingResponse

from .. import engine, importer

router = APIRouter(tags=["import"])


@router.post("/api/import")
async def import_export(request: Request, file: UploadFile = File(...),
                        mine: bool = Form(True)):
    data = await file.read()
    memory = request.app.state.memory

    async def gen():
        async for ev in importer.import_stream(data, file.filename, memory, mine=mine):
            yield engine.sse(ev)

    return StreamingResponse(gen(), media_type="text/event-stream")
