"""
Meta-tools for inspecting and managing the tool-guard's broken-tool registry.

These tools are intentionally NOT wrapped by the guard. They form the
control plane: if the guard itself ever malfunctions and starts marking
benign tools broken, we still need a way to inspect and reset state from
the agent without needing shell access. Wrapping these would mean a bug
in their bodies could disable the only escape hatch.

To register these without going through the guarded mcp.tool, the guard
exposes the original (unpatched) decorator on mcp via mcp._unguarded_tool
(set by tool_guard.install). If install() hasn't been called, we fall back
to mcp.tool — which is the right behavior for a non-guarded server.
"""

import logging
from typing import Any

from reaper_mcp import tool_guard

logger = logging.getLogger("reaper_mcp.guard_tools")


def register_tools(mcp: Any) -> None:
    raw_tool = getattr(mcp, "_unguarded_tool", mcp.tool)

    @raw_tool()
    def list_broken_tools() -> dict:
        """List all tools that the guard has marked broken (auto-disabled).
        Each entry includes the failure reason, hit count, and timestamps.
        Broken tools are hidden from the tool surface on server startup."""
        return {"success": True, "broken": tool_guard.list_broken()}

    @raw_tool()
    def clear_broken_tool(name: str) -> dict:
        """Re-enable a tool that the guard previously disabled.
        After clearing, the tool will be registered normally on the NEXT
        server start. Use this after fixing the underlying API drift."""
        cleared = tool_guard.clear_broken(name)
        return {
            "success": True,
            "cleared": cleared,
            "name": name,
            "note": (
                "Tool will be registered on next server start."
                if cleared else
                "No entry found — tool was not in the broken set."
            ),
        }

    @raw_tool()
    def clear_all_broken_tools() -> dict:
        """Wipe the entire broken-tool registry.
        Use this when you've done a bulk fix or want a clean slate.
        All previously-broken tools will be registered on the next start."""
        n = tool_guard.clear_all_broken()
        return {"success": True, "cleared_count": n}
