"""Pictures from an outside tool reach the models (#610).

An MCP tool can answer with pictures as well as text. Pinned here:
1. The client keeps a result's pictures, and run_tool hands them on with
   the text, which stays the same string everywhere text goes. A result
   with no pictures is exactly as before.
2. An Anthropic seat sees them in its own tool_result. An OpenAI seat is
   told plainly that a picture came back that it isn't shown.
3. The engine stores each one as an ordinary attachment on the reply and
   the tool row, so every participant sees it from the next round.
4. Pictures are downscaled the way uploads are, and anything that isn't a
   picture, or is too big to send, is left out.

Keyless. The MCP server is the real fake in tests/fake_mcp.py, and the
provider and the round are faked as test_tool_concurrency and
test_view_screenshot fake them."""

import asyncio
import base64
import io
import json

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from backend import db, engine, providers
from backend import tools as tools_mod
from backend.app import create_app
from backend.config import Settings
from backend.mcp_client import MAX_RESULT_IMAGES, CallOutcome, images_of
from tests.fake_mcp import PNG_1PX_B64
from tests.test_mcp_client import run_with_manager
from tests.test_tool_concurrency import PARTICIPANT, ROSTER, TOOLS, _TwoRoundClient


# ---------- the client and run_tool ----------

def test_the_client_keeps_a_results_pictures_and_run_tool_hands_them_on():
    async def body(mgr):
        res = await mgr.call_result("mcp__fake__picture", {})
        assert isinstance(res, CallOutcome)
        assert res.text == "The bench, from the front."
        assert res.images == (("image/png", PNG_1PX_B64),)
        out = await tools_mod.run_tool("mcp__fake__picture", {},
                                       {"_mcp": mgr, "max_tool_output": 8000})
        # Everywhere text goes, it's the same text.
        assert out == "The bench, from the front."
        assert isinstance(out, str)
        assert out.images == (("image/png", PNG_1PX_B64),)
        # A result with no pictures is a plain string, as it always was.
        echo = await tools_mod.run_tool("mcp__fake__echo", {"text": "hi"},
                                        {"_mcp": mgr, "max_tool_output": 8000})
        assert type(echo) is str and echo == "echo:hi"
        return True
    assert run_with_manager(body)


def test_a_picture_with_no_words_still_says_it_is_one_and_an_error_brings_none():
    class _Pic:
        type = "image"
        data = PNG_1PX_B64
        mime_type = "image/png"

    class _Res:
        def __init__(self, content, is_error=False):
            self.content = content
            self.is_error = is_error

    assert images_of(_Res([_Pic()] * (MAX_RESULT_IMAGES + 2))) == (("image/png", PNG_1PX_B64),) * MAX_RESULT_IMAGES

    # SDK 1.x named the field mimeType.
    class _OldPic:
        type = "image"
        data = PNG_1PX_B64
        mimeType = "image/jpeg"
    assert images_of(_Res([_OldPic()])) == (("image/jpeg", PNG_1PX_B64),)


def _png(width, height):
    buf = io.BytesIO()
    Image.new("RGB", (width, height), (120, 90, 60)).save(buf, "PNG")
    return base64.b64encode(buf.getvalue()).decode()


def test_pictures_are_downscaled_like_uploads_and_non_pictures_are_left_out(monkeypatch):
    (mime, b64), = tools_mod.tool_images([("image/png", _png(3200, 400))])
    w, h = Image.open(io.BytesIO(base64.b64decode(b64))).size
    assert max(w, h) <= 1568
    assert mime in ("image/png", "image/jpeg")
    # A small picture goes as it came.
    assert tools_mod.tool_images([("image/png", PNG_1PX_B64)]) == (("image/png", PNG_1PX_B64),)
    # Not a picture, not base64, or too big: left out, never an error.
    assert tools_mod.tool_images([("application/pdf", PNG_1PX_B64)]) == ()
    assert tools_mod.tool_images([("image/png", "not base64!")]) == ()
    monkeypatch.setattr(tools_mod, "MAX_TOOL_IMAGE_BYTES", 10)
    assert tools_mod.tool_images([("image/png", PNG_1PX_B64)]) == ()


# ---------- the providers ----------

def test_an_anthropic_seat_sees_the_pictures_in_its_own_tool_result(cfg, monkeypatch):
    fake = _TwoRoundClient()
    monkeypatch.setattr(providers, "_anthropic_client", lambda p: fake)

    async def fake_run_tool(name, tool_input, cfg_, origin_agent=None, memory=None):
        if name == "alpha":
            return tools_mod.ToolText("The bench, from the front.", [("image/png", PNG_1PX_B64)])
        return "out-beta"

    monkeypatch.setattr(providers, "run_tool", fake_run_tool)

    async def go():
        async for _ in providers.stream_reply(
                PARTICIPANT, ROSTER, [], {}, dict(cfg), None, "", False, tools=TOOLS):
            pass

    asyncio.run(go())
    results = fake.captured[1]["messages"][-1]["content"]
    assert results[0]["content"] == [
        {"type": "text", "text": "The bench, from the front."},
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": PNG_1PX_B64}},
    ]
    # A tool with no pictures answers exactly as before.
    assert results[1]["content"] == "out-beta"


def test_an_openai_seat_is_told_a_picture_came_back_that_it_isnt_shown():
    one = providers._tool_output_text(tools_mod.ToolText("The bench.", [("image/png", PNG_1PX_B64)]))
    assert one.startswith("The bench.\n\n[1 picture came with this result. You aren't shown pictures from a tool here")
    assert "say so rather than guess what it shows" in one
    two = providers._tool_output_text(tools_mod.ToolText("x", [("image/png", "a"), ("image/png", "b")]))
    assert "[2 pictures came with this result." in two and "what they show" in two
    assert providers._tool_output_text("plain") == "plain"


# ---------- the round ----------

@pytest.fixture
def app(tmp_path):
    settings = Settings(data_dir=str(tmp_path / "data"),
                        memory_url="http://127.0.0.1:1")
    return create_app(settings)


def test_the_round_stores_each_picture_on_the_reply_and_the_tool_row(app, monkeypatch):
    async def stream_reply(participant, roster, transcript, names, cfg, project,
                           chat_summary, voice_mode, tools=None, memory=None):
        yield ("tool", {"tool": "mcp__fake__picture", "input": {},
                        "output": tools_mod.ToolText(
                            "The bench, from the front.",
                            [("image/png", PNG_1PX_B64), ("image/png", PNG_1PX_B64)])})
        yield ("text", "seen it")

    monkeypatch.setattr(engine.providers, "stream_reply", stream_reply)
    with TestClient(app, base_url="http://127.0.0.1") as c:
        chat = c.post("/api/chats", json={}).json()
        with c.stream("POST", f"/api/chats/{chat['id']}/send",
                      json={"text": "@claude show me the bench"}) as r:
            body = "".join(r.iter_text())
        events = [json.loads(l[6:]) for l in body.splitlines()
                  if l.startswith("data: ")]
        (act,) = [e for e in events if e["type"] == "tool_activity"]
        assert act["output_text"] == "The bench, from the front."
        assert act["attachment_id"]

        got = c.get(f"/api/chats/{chat['id']}").json()
        reply = next(m for m in got["messages"] if m["speaker"] == "claude")
        (ev,) = reply["tool_events"]
        assert ev["attachment_id"] == act["attachment_id"]
        assert ev["output_text"] == "The bench, from the front."
        # Both pictures are on the reply, for every participant from the next round.
        files = reply["attachments"]
        assert len(files) == 2 and files[0]["id"] == act["attachment_id"]
        assert all(f["mime"] == "image/png" and f["filename"].startswith("fake-picture-") for f in files)
        con = db.connect()
        try:
            sizes = [r[0] for r in con.execute("SELECT size FROM attachments ORDER BY id")]
        finally:
            con.close()
        assert sizes == [len(base64.b64decode(PNG_1PX_B64))] * 2
