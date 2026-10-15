"""
Organisation-template planner.

This is pure Python with no FL or MIDI dependency, so it can be unit tested
and so "dry run" is genuinely side-effect free: it only reads the channel
list the server already fetched and returns the list of operations it
*would* send.  Applying is just handing that list to the controller's
"batch" command, which wraps everything in one undo point.

Template shape
--------------
{
  "groups": [
    {
      "name": "Drums",                     # label, also used for shared tracks
      "prefix": "DR ",                     # prepended to every matched channel
      "color": "#E74C3C",                  # #RRGGBB
      "mixer_range": [1, 10],              # inclusive mixer tracks for the group
      "match": ["kick", "snare", "hat"],   # case-insensitive substrings of the
                                           #   channel name or plugin name
      "channels": [4, 7]                   # optional explicit channel indices
    },
    ...
  ],
  "name_mixer_tracks": true,    # name each used track after its channel
  "color_mixer_tracks": true    # colour each used track with the group colour
}

Group order is priority: a channel goes to the first group that claims it.
Prefixes are stripped before re-applying, so running a template twice is a
no-op the second time.
"""

from __future__ import annotations

import re
from typing import Any

HEX_RE = re.compile(r"^#?[0-9a-fA-F]{6}$")


def normalise_hex(color: str) -> str:
    if not isinstance(color, str) or not HEX_RE.match(color.strip()):
        raise ValueError(f"colour must be #RRGGBB, got {color!r}")
    return "#" + color.strip().lstrip("#").upper()


def validate_template(template: dict[str, Any]) -> list[dict[str, Any]]:
    if not isinstance(template, dict):
        raise ValueError("template must be a JSON object")
    groups = template.get("groups")
    if not isinstance(groups, list) or not groups:
        raise ValueError('template needs a non-empty "groups" list')
    clean = []
    for n, g in enumerate(groups):
        if not isinstance(g, dict) or not g.get("name"):
            raise ValueError(f'group {n} needs a "name"')
        rng = g.get("mixer_range")
        if not (isinstance(rng, list) and len(rng) == 2 and all(isinstance(x, int) for x in rng)):
            raise ValueError(f'group {g["name"]!r}: "mixer_range" must be [first, last]')
        lo, hi = rng
        if lo < 1 or hi < lo:
            raise ValueError(f'group {g["name"]!r}: mixer_range {rng} invalid (first >= 1, last >= first)')
        clean.append({
            "name": str(g["name"]),
            "prefix": str(g.get("prefix", "")),
            "color": normalise_hex(g["color"]) if g.get("color") else None,
            "mixer_range": (lo, hi),
            "match": [str(m).lower() for m in g.get("match", []) if str(m).strip()],
            "channels": [int(c) for c in g.get("channels", [])],
        })
    return clean


def _base_name(name: str, prefixes: list[str]) -> str:
    """Strip any known group prefix so re-runs don't stack prefixes."""
    for p in sorted((p for p in prefixes if p), key=len, reverse=True):
        if name.startswith(p):
            return name[len(p):]
    return name


def _matches(group: dict[str, Any], ch: dict[str, Any]) -> bool:
    if ch["index"] in group["channels"]:
        return True
    hay = f'{ch.get("name") or ""} {ch.get("plugin") or ""}'.lower()
    return any(kw in hay for kw in group["match"])


def plan(
    channels: list[dict[str, Any]],
    template: dict[str, Any],
    mixer_track_count: int,
    mixer_tracks: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Compute the operations needed to make the project match the template.

    `mixer_tracks` is the output of list_mixer_tracks(include_empty=True); it
    lets us skip mixer renames/recolours that are already in place so a
    re-run plans nothing.  Without it every used track is (re)named/coloured.
    """
    groups = validate_template(template)
    name_tracks = bool(template.get("name_mixer_tracks", True))
    color_tracks = bool(template.get("color_mixer_tracks", True))
    prefixes = [g["prefix"] for g in groups]
    current_tracks = {t["index"]: t for t in (mixer_tracks or [])}

    ops: list[dict[str, Any]] = []
    warnings: list[str] = []
    assigned: dict[str, list[dict[str, Any]]] = {g["name"]: [] for g in groups}
    unmatched: list[dict[str, Any]] = []

    for ch in channels:
        for g in groups:
            if _matches(g, ch):
                assigned[g["name"]].append(ch)
                break
        else:
            unmatched.append({"index": ch["index"], "name": ch.get("name")})

    track_users: dict[int, list[tuple[str, str]]] = {}   # track -> [(group, channel new name)]

    for g in groups:
        lo, hi = g["mixer_range"]
        if hi >= mixer_track_count:
            warnings.append(f'group {g["name"]!r}: mixer_range ends at {hi} but project only has tracks 0..{mixer_track_count - 1}; clamping')
            hi = mixer_track_count - 1
        members = assigned[g["name"]]
        if len(members) > (hi - lo + 1):
            warnings.append(f'group {g["name"]!r}: {len(members)} channels but only {hi - lo + 1} mixer tracks; extra channels share track {hi}')
        for n, ch in enumerate(members):
            new_name = g["prefix"] + _base_name(ch["name"], prefixes)
            track = min(lo + n, hi)
            if new_name != ch["name"]:
                ops.append({"op": "rename_channel", "index": ch["index"], "name": new_name,
                            "from": ch["name"], "group": g["name"]})
            if g["color"] and normalise_hex(ch.get("color", "#000000")) != g["color"]:
                ops.append({"op": "set_channel_color", "index": ch["index"], "color": g["color"],
                            "from": ch.get("color"), "group": g["name"]})
            if ch.get("mixer_track") != track:
                ops.append({"op": "route_channel_to_mixer", "index": ch["index"], "track": track,
                            "from": ch.get("mixer_track"), "group": g["name"]})
            track_users.setdefault(track, []).append((g["name"], new_name))

    for track, users in sorted(track_users.items()):
        group_name = users[0][0]
        g = next(x for x in groups if x["name"] == group_name)
        cur = current_tracks.get(track, {})
        if name_tracks:
            # One channel on the track -> name it after the channel; shared -> group name.
            track_name = users[0][1] if len(users) == 1 else group_name
            if cur.get("name") != track_name:
                ops.append({"op": "rename_mixer_track", "index": track, "name": track_name,
                            "from": cur.get("name"), "group": group_name})
        if color_tracks and g["color"]:
            cur_color = normalise_hex(cur["color"]) if cur.get("color") else None
            if cur_color != g["color"]:
                ops.append({"op": "set_mixer_track_color", "index": track, "color": g["color"],
                            "from": cur.get("color"), "group": group_name})

    return {
        "ops": ops,
        "summary": {g["name"]: len(assigned[g["name"]]) for g in groups},
        "unmatched": unmatched,
        "warnings": warnings,
    }


def strip_plan_metadata(ops: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The controller's batch command only wants op/index/name/color/track."""
    keep = ("op", "index", "name", "color", "track")
    return [{k: v for k, v in op.items() if k in keep} for op in ops]
