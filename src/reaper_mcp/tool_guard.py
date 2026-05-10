"""
Tool-guard shim for the REAPER MCP server.

This module exists because the upstream tool surface (~58 tools) was written
against an older reapy and parts of it have drifted out of sync with reapy
0.10's object API (e.g. Track.volume was removed, several reascript_api
functions get renamed across versions). Hitting one of those drifted tools
returns AttributeError, which is the structural-failure signature.

What the guard does:

1. **Runtime detection.** Every guarded tool runs inside a try/except that
   catches AttributeError. If one fires, we record the tool name + reason
   in a state file at ~/.config/reaper-mcp/broken_tools.json and return a
   payload that tells the caller the tool is broken and points at the reapy
   bridge as the fallback.

2. **Hide on next start.** When the server boots, register_tools() consults
   the state file before letting the tool register with FastMCP. Any tool
   marked broken simply doesn't get registered, so the agent never sees it
   in the tool list. This forces the agent toward the bridge instead of
   re-trying a known-broken endpoint.

3. **Manual control.** Three meta-tools (list/clear/clear_all) live in
   guard_tools.py and let the agent inspect/reset the broken set after a
   fix lands — those tools are intentionally NOT guarded so a bug in them
   can't lock the operator out of the control plane.

The shim is installed in server.py via tool_guard.install(mcp) BEFORE the
per-module register_tools() calls fire. install() monkey-patches mcp.tool
so every subsequent @mcp.tool() decoration goes through the guard. This
avoids touching all nine *_tools.py modules.

Why AttributeError (only) marks a tool broken:
    - It's the signature of API drift (the failure modes we've actually
      seen: 'Track' has no attribute 'volume', module 'reapy.reascript_api'
      has no attribute 'EnumProjects').
    - Other exception types (ValueError from bad args, RuntimeError from
      transient connection blips, etc.) are NOT structural failures and
      should pass through unchanged so the operator can retry.
"""

from __future__ import annotations

import functools
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any, Callable

logger = logging.getLogger("reaper_mcp.tool_guard")

_STATE_DIR = Path(
    os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")
) / "reaper-mcp"
_STATE_FILE = _STATE_DIR / "broken_tools.json"

_lock = Lock()
_state_cache: dict[str, dict[str, Any]] | None = None


def _load_state() -> dict[str, dict[str, Any]]:
    if not _STATE_FILE.exists():
        return {}
    try:
        data = json.loads(_STATE_FILE.read_text())
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("broken_tools.json unreadable, starting fresh: %s", e)
        return {}


def _save_state(state: dict[str, dict[str, Any]]) -> None:
    _STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True))
    tmp.replace(_STATE_FILE)


def _state() -> dict[str, dict[str, Any]]:
    global _state_cache
    if _state_cache is None:
        with _lock:
            if _state_cache is None:
                _state_cache = _load_state()
    return _state_cache


def is_broken(name: str) -> bool:
    return name in _state()


def mark_broken(name: str, reason: str) -> None:
    """Record that `name` failed structurally. Persists to disk."""
    with _lock:
        state = _state()
        now = datetime.now(timezone.utc).isoformat()
        if name in state:
            entry = state[name]
            entry["hits"] = int(entry.get("hits", 0)) + 1
            entry["last_seen"] = now
            entry["last_reason"] = reason
        else:
            state[name] = {
                "reason": reason,
                "first_seen": now,
                "last_seen": now,
                "hits": 1,
            }
        _save_state(state)
    logger.warning("tool %r marked broken: %s", name, reason)


def list_broken() -> dict[str, dict[str, Any]]:
    return dict(_state())


def clear_broken(name: str) -> bool:
    """Remove `name` from the broken set. Returns True if it was there."""
    with _lock:
        state = _state()
        if name not in state:
            return False
        state.pop(name)
        _save_state(state)
    logger.info("tool %r un-marked", name)
    return True


def clear_all_broken() -> int:
    """Remove all entries. Returns count cleared."""
    with _lock:
        state = _state()
        n = len(state)
        state.clear()
        _save_state(state)
    logger.info("cleared %d broken-tool entries", n)
    return n


def _disabled_payload(name: str, reason: str) -> dict[str, Any]:
    return {
        "success": False,
        "tool_disabled": True,
        "tool": name,
        "reason": reason,
        "note": (
            "This tool hit a structural failure (likely reapy API drift) "
            "and has been recorded as broken. It will be hidden from the "
            "tool list on the next server start. Use the reapy bridge "
            "(reapy.inside_reaper()) for this operation instead."
        ),
    }


def install(mcp: Any) -> None:
    """Monkey-patch mcp.tool so every subsequent @mcp.tool() registration
    is guarded. Call this once, before any register_tools() runs.

    The patched decorator:
      - Skips registration entirely for tools already in the broken set
        (hides them from the surface).
      - For tools not yet broken, wraps the function so AttributeError at
        call time persists the failure and returns a disabled payload.
    """
    original_tool = mcp.tool
    # Expose the original decorator for the meta-tools in guard_tools.py to
    # use. They MUST bypass the guard so a bug in them can't disable the
    # control plane.
    mcp._unguarded_tool = original_tool  # type: ignore[attr-defined]

    def guarded_tool(*tool_args: Any, **tool_kwargs: Any) -> Callable:
        # mcp.tool() is called with parens; it returns the real decorator.
        real_decorator = original_tool(*tool_args, **tool_kwargs)

        def wrapper(fn: Callable) -> Callable:
            name = fn.__name__
            if is_broken(name):
                logger.info(
                    "skipping registration of broken tool %r (reason: %s)",
                    name, _state().get(name, {}).get("reason", "?"),
                )
                # Return the original function so any non-MCP callers in
                # the same module still work, but never hand it to FastMCP.
                return fn

            @functools.wraps(fn)
            def guarded_fn(*args: Any, **kwargs: Any) -> Any:
                try:
                    return fn(*args, **kwargs)
                except AttributeError as e:
                    mark_broken(name, str(e))
                    return _disabled_payload(name, str(e))

            return real_decorator(guarded_fn)

        return wrapper

    mcp.tool = guarded_tool  # type: ignore[assignment]
    logger.info("tool-guard installed on FastMCP instance")
