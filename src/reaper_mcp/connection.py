import logging
import reapy

logger = logging.getLogger("reaper_mcp.connection")

_connected = False


def ensure_connected() -> None:
    global _connected
    if _connected:
        return
    try:
        # Use reconnect() rather than connect(): if reapy was imported before
        # REAPER was up (common when this MCP server is launched as a long-lived
        # daemon by an editor or agent harness), the import-time probe failed,
        # CLIENTS["localhost"] is missing, and reapy.reascript_api loaded with
        # an empty __all__. connect() with no host is a no-op in that state.
        # reconnect() retries the probe and reloads reascript_api on success,
        # healing the late-start case. When REAPER was already up at import,
        # reconnect() is a cheap re-probe.
        reapy.reconnect()
        _connected = True
        logger.info("Connected to REAPER")
    except Exception as e:
        raise RuntimeError(
            f"Cannot connect to REAPER: {e}. "
            "Make sure REAPER is running and the distant API is enabled. "
            "To enable it: run the setup script (scripts/enable_reapy.py) or "
            "in REAPER go to Actions > Run ReaScript, then run: "
            "import reapy; reapy.config.enable_dist_api()"
        ) from e


def get_project() -> reapy.Project:
    ensure_connected()
    return reapy.Project()
