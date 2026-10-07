"""A real (tiny) MCP server for the client-layer tests - stdio, keyless."""
from mcp.server import MCPServer
from mcp.types import CallToolResult, ImageContent, TextContent

mcp = MCPServer("fake")

# The background-work contract (#604): a long job answers at once with a
# `background` block in its structured content, and a progress tool reports
# where it's got to. Made-up work, counted per server process.
_progress = {"steps": 0}


def _block(state, steps, stage):
    return {"background": {
        "job": "job-1", "state": state, "title": "Fake bench",
        "progress_tool": "build_progress", "stage": stage, "steps": steps,
        "elapsed_s": steps * 10, "waiting_for": None, "ask": "", "reply": ""}}


@mcp.tool()
def echo(text: str) -> str:
    """Echo the text back."""
    return f"echo:{text}"


@mcp.tool()
def boom() -> str:
    """Always fails."""
    raise RuntimeError("kaboom")


@mcp.tool()
def build(text: str) -> CallToolResult:
    """Start a made-up build that runs in the background."""
    return CallToolResult(
        content=[TextContent(type="text", text=f"Started building: {text}")],
        structured_content=_block("running", 0, "cutting the legs"))


@mcp.tool()
def build_progress() -> CallToolResult:
    """Where the made-up build has got to."""
    _progress["steps"] += 1
    return CallToolResult(
        content=[TextContent(type="text", text="still going")],
        structured_content=_block("running", _progress["steps"],
                                  "fitting the top"))


# A real 1x1 PNG, for a tool that answers with a picture (#610).
PNG_1PX_B64 = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGNgYGBg"
               "AAAABQABh6FO1AAAAABJRU5ErkJggg==")


@mcp.tool()
def picture() -> CallToolResult:
    """A made-up picture of the bench, with a line about it."""
    return CallToolResult(content=[
        TextContent(type="text", text="The bench, from the front."),
        ImageContent(type="image", data=PNG_1PX_B64, mime_type="image/png")])


if __name__ == "__main__":
    mcp.run()
