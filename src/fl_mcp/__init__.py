"""fl-mcp: an MCP server that controls FL Studio over MIDI SysEx."""

from .server import main, mcp

__all__ = ["main", "mcp"]
