"""
mock_mcp_server.py - Lightweight Mock MCP Server for SMRTk Testing
======================================================================
Runs a minimal FastMCP SSE server on port 8888.
Provides two test tools:
- get_host_time(): Returns host ISO timestamp string.
- multiply_numbers(a, b): Multiplies two integers.
"""

from fastmcp import FastMCP
import datetime

mcp = FastMCP("HostMock")


@mcp.tool()
def get_host_time() -> str:
    """Returns current system timestamp from the host."""
    return datetime.datetime.now().isoformat()


@mcp.tool()
def multiply_numbers(a: int, b: int) -> int:
    """Multiplies two numbers on the host."""
    return a * b


if __name__ == "__main__":
    print("Starting Mock MCP SSE Server on http://0.0.0.0:8888/sse ...")
    mcp.run(transport="sse", host="0.0.0.0", port=8888)
