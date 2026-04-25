"""mpv MCP server — controls a running mpv instance via its JSON IPC socket."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from mcp.server.fastmcp import FastMCP

SOCKET_PATH = os.environ.get("MPV_SOCKET", "/tmp/mpv.sock")

mcp = FastMCP("mpv")


async def _send(command: list) -> dict:
    if not Path(SOCKET_PATH).exists():
        return {"error": f"mpv socket not found at {SOCKET_PATH}. Start mpv with --idle --input-ipc-server={SOCKET_PATH}"}
    reader, writer = await asyncio.open_unix_connection(SOCKET_PATH)
    try:
        writer.write((json.dumps({"command": command}) + "\n").encode())
        await writer.drain()
        line = await asyncio.wait_for(reader.readline(), timeout=5.0)
        return json.loads(line.decode())
    finally:
        writer.close()
        await writer.wait_closed()


@mcp.tool()
async def play(url: str) -> dict:
    """Load and play a URL or file path, replacing current playback."""
    return await _send(["loadfile", url, "replace"])


@mcp.tool()
async def queue(url: str) -> dict:
    """Append a URL or file path to the playlist."""
    return await _send(["loadfile", url, "append-play"])


@mcp.tool()
async def pause() -> dict:
    return await _send(["set_property", "pause", True])


@mcp.tool()
async def resume() -> dict:
    return await _send(["set_property", "pause", False])


@mcp.tool()
async def stop() -> dict:
    return await _send(["stop"])


@mcp.tool()
async def skip() -> dict:
    """Skip to the next item in the playlist."""
    return await _send(["playlist-next"])


@mcp.tool()
async def previous() -> dict:
    return await _send(["playlist-prev"])


@mcp.tool()
async def seek(seconds: float, mode: str = "relative") -> dict:
    """Seek. mode: 'relative' (default), 'absolute', 'absolute-percent'."""
    return await _send(["seek", seconds, mode])


@mcp.tool()
async def volume(level: float) -> dict:
    """Set volume 0-100 (or higher for boost)."""
    return await _send(["set_property", "volume", level])


@mcp.tool()
async def now_playing() -> dict:
    """Return current title, position, duration, and pause state."""
    out = {}
    for prop in ("media-title", "path", "time-pos", "duration", "pause", "volume"):
        r = await _send(["get_property", prop])
        out[prop] = r.get("data")
    return out


@mcp.tool()
async def playlist() -> dict:
    return await _send(["get_property", "playlist"])


@mcp.tool()
async def raw(command: list) -> dict:
    """Send a raw mpv JSON IPC command. See mpv docs for command list."""
    return await _send(command)


if __name__ == "__main__":
    mcp.run()
