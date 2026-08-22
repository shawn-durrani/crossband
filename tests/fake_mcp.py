"""A real (tiny) MCP server for the client-layer tests - stdio, keyless."""
from mcp.server import MCPServer

mcp = MCPServer("fake")


@mcp.tool()
def echo(text: str) -> str:
    """Echo the text back."""
    return f"echo:{text}"


@mcp.tool()
def boom() -> str:
    """Always fails."""
    raise RuntimeError("kaboom")


if __name__ == "__main__":
    mcp.run()
