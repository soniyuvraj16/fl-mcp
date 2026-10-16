"""
fl-mcp: the MCP server Claude talks to.

Every tool here is a thin wrapper: validate the arguments, forward one named
command to the controller inside FL Studio over MIDI SysEx, and return the
JSON the controller sent back.  All project logic (and the undo points) live
in the controller; the one exception is the organisation-template planner,
which runs here so a dry run never touches FL at all.

Stdio transport rule: stdout *is* the MCP channel, so nothing in this process
may print() to it.  Diagnostics go to stderr via logging.

Tool annotations: read-only tools carry readOnlyHint=True so the client can
treat them as safe; mutating tools carry destructiveHint=False because each
one creates an undo point first.
"""

from __future__ import annotations

import json
import logging
import sys
from typing import Any

from fastmcp import FastMCP

from .midi_bridge import FLBridge, FLCommandError, FLConnectionError, get_bridge
from .templates import normalise_hex, plan, strip_plan_metadata

logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="fl-mcp %(levelname)s: %(message)s")
log = logging.getLogger("fl_mcp")

READ_ONLY = {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False}
MUTATING = {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}

mcp = FastMCP(
    "fl-mcp",
    instructions=(
        "Controls FL Studio 2024 through a MIDI-scripting bridge. Call fl_ping first to confirm the "
        "link. Channel and mixer indices are 0-based; mixer track 0 is Master. Every change creates an "
        "undo point in FL (Ctrl+Z reverts it). For apply_organization_template always review the "
        "dry run before calling it again with dry_run=false."
    ),
)


def _call(cmd: str, **args: Any) -> Any:
    """Forward to FL and convert bridge exceptions into clean tool errors."""
    try:
        return get_bridge().request(cmd, args)
    except (FLConnectionError, FLCommandError) as e:
        # FastMCP turns a raised exception into an MCP tool error for the client.
        raise RuntimeError(str(e)) from None


# --------------------------------------------------------------------------
# Read-only tools
# --------------------------------------------------------------------------

@mcp.tool(annotations=READ_ONLY)
def fl_ping() -> dict:
    """Check the connection to FL Studio. Returns the MIDI-scripting API version
    (general.getVersion), the full FL Studio version string, whether global
    channel indexing is available, and the MIDI port number the script sees."""
    return _call("ping")


@mcp.tool(annotations=READ_ONLY)
def get_project_info() -> dict:
    """Project overview: tempo (BPM), project title (parsed from the window title,
    FL has no direct API for it), channel count, pattern count, mixer track count."""
    return _call("project_info")


@mcp.tool(annotations=READ_ONLY)
def list_channels() -> dict:
    """List every channel-rack channel: index, name, colour (#RRGGBB), type
    (sampler/plugin/layer/audio_clip/automation_clip), plugin name, and the
    mixer track it is routed to."""
    return _call("list_channels")


@mcp.tool(annotations=READ_ONLY)
def list_mixer_tracks(include_empty: bool = False) -> dict:
    """List mixer tracks: index, name, colour, volume (0-1 and dB), pan (-1..1),
    how many channels route to it, and the plugin in each effect slot.
    Tracks with no effects and no routed channels are skipped unless
    include_empty=true. Track 0 is Master and is always included."""
    return _call("list_mixer_tracks", include_empty=include_empty)


# --------------------------------------------------------------------------
# Mutating tools (each creates an undo point inside FL before changing anything)
# --------------------------------------------------------------------------

@mcp.tool(annotations=MUTATING)
def rename_channel(index: int, name: str) -> dict:
    """Rename a channel-rack channel. An empty name resets it to the plugin/sample default."""
    return _call("rename_channel", index=index, name=name)


@mcp.tool(annotations=MUTATING)
def set_channel_color(index: int, color: str) -> dict:
    """Set a channel's colour. color is #RRGGBB (e.g. #E74C3C)."""
    return _call("set_channel_color", index=index, color=normalise_hex(color))


@mcp.tool(annotations=MUTATING)
def route_channel_to_mixer(index: int, track: int) -> dict:
    """Route a channel to a mixer track (0 = Master, 1..N = inserts)."""
    return _call("route_channel_to_mixer", index=index, track=track)


@mcp.tool(annotations=MUTATING)
def rename_mixer_track(index: int, name: str) -> dict:
    """Rename a mixer track. An empty name resets it to the default ("Insert N")."""
    return _call("rename_mixer_track", index=index, name=name)


@mcp.tool(annotations=MUTATING)
def set_mixer_track_color(index: int, color: str) -> dict:
    """Set a mixer track's colour. color is #RRGGBB."""
    return _call("set_mixer_track_color", index=index, color=normalise_hex(color))


@mcp.tool(annotations=MUTATING)
def set_tempo(bpm: float) -> dict:
    """Set the project tempo in BPM (10-522). The scripting API has no setTempo
    function, so this fires FL's REC_Tempo automation event, the route the
    API stubs document. Returns the tempo before and after."""
    return _call("set_tempo", bpm=bpm)


@mcp.tool(annotations=MUTATING)
def set_mixer_track_volume(index: int, volume: float | None = None, volume_db: float | None = None) -> dict:
    """Set a mixer track's fader. Give exactly one of volume (0-1, where 0.8 = 0 dB)
    or volume_db (e.g. -6). Returns the track's volume, dB, pan, mute and solo
    before and after."""
    return _call("set_mixer_track_volume", index=index, volume=volume, volume_db=volume_db)


@mcp.tool(annotations=MUTATING)
def set_mixer_track_pan(index: int, pan: float) -> dict:
    """Set a mixer track's pan, -1 (full left) to 1 (full right), 0 = centre."""
    return _call("set_mixer_track_pan", index=index, pan=pan)


@mcp.tool(annotations=MUTATING)
def set_mixer_track_mute(index: int, muted: bool) -> dict:
    """Mute or unmute a mixer track."""
    return _call("set_mixer_track_mute", index=index, muted=muted)


@mcp.tool(annotations=MUTATING)
def set_mixer_track_solo(index: int, solo: bool) -> dict:
    """Solo or unsolo a mixer track."""
    return _call("set_mixer_track_solo", index=index, solo=solo)


@mcp.tool(annotations=MUTATING)
def apply_organization_template(template: dict | str, dry_run: bool = True) -> dict:
    """Rename, colour and route channels according to a JSON template of groups.

    template: {"groups": [{"name": "Drums", "prefix": "DR ", "color": "#E74C3C",
    "mixer_range": [1, 10], "match": ["kick", "snare", "hat"], "channels": [4]}, ...],
    "name_mixer_tracks": true, "color_mixer_tracks": true}. Group order is priority.
    Matching is a case-insensitive substring test against the channel name or
    plugin name; "channels" lists explicit indices.

    dry_run=true (default) returns the planned operations WITHOUT touching FL.
    dry_run=false applies them as one batch under a single undo point.
    """
    if isinstance(template, str):
        try:
            template = json.loads(template)
        except json.JSONDecodeError as e:
            raise RuntimeError(f"template is not valid JSON: {e}") from None

    # Three reads, no writes: the plan is computed entirely on this side.
    snapshot = _call("list_channels")
    info = _call("project_info")
    tracks = _call("list_mixer_tracks", include_empty=True)
    try:
        planned = plan(snapshot["channels"], template, info["mixer_track_count"], tracks["tracks"])
    except ValueError as e:
        raise RuntimeError(f"template error: {e}") from None

    result = {
        "dry_run": dry_run,
        "indexing": snapshot.get("indexing"),
        "planned_changes": planned["ops"],
        "channels_per_group": planned["summary"],
        "unmatched_channels": planned["unmatched"],
        "warnings": planned["warnings"],
    }
    if dry_run or not planned["ops"]:
        result["applied"] = 0
        return result

    outcome = _call("batch", ops=strip_plan_metadata(planned["ops"]),
                    label="apply organisation template")
    result["applied"] = outcome.get("applied")
    result["results"] = outcome.get("results")
    return result


def main() -> None:
    """Entry point for `uv run fl-mcp`: stdio transport."""
    mcp.run()


if __name__ == "__main__":
    main()
