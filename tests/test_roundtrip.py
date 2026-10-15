"""
End-to-end tests with a fake FL Studio.

The server-side encoder produces SysEx frames -> we hand them to the
controller's OnSysEx exactly as FL would (with F0/F7 wrapped) -> the
controller's replies are captured from device.midiOutSysex -> the
server-side Reassembler decodes them.  If these pass, the only things left
to trust are loopMIDI, FL's real API behaviour, and the colour byte order.
"""

from __future__ import annotations

import json

import pytest

from fl_mcp.protocol import Reassembler, encode_message
from fl_mcp.templates import plan, strip_plan_metadata

from fake_fl import FakeProject, FakeSysExEvent, load_controller


@pytest.fixture
def fl():
    project = FakeProject()
    project.channels[0].update(name="Kick 909", plugin=None)
    project.channels[1].update(name="Snare", plugin=None)
    project.channels[2].update(name="Serum", plugin="Serum", type=3 - 1)  # CT_GenPlug
    project.channels[3].update(name="Reese", plugin="Sytrus", type=2)
    project.channels[4].update(name="Vox lead", plugin=None)
    project.channels[5].update(name="Riser", plugin=None)
    project.tracks[1]["fx"][0] = "Fruity Limiter"
    ctrl = load_controller(project)
    return project, ctrl


def roundtrip(project, ctrl, cmd, args=None, rid=1, chunk=240):
    """Send one request through the real framing, return the decoded reply."""
    project.sent.clear()
    for inner in encode_message({"id": rid, "cmd": cmd, "args": args or {}}, frame_id=rid, chunk_payload=chunk):
        ev = FakeSysExEvent(b"\xF0" + inner + b"\xF7")
        ctrl.OnSysEx(ev)
        assert ev.handled, "controller must claim our frames"
    reasm = Reassembler()
    reply = None
    for frame in project.sent:
        assert frame[0] == 0xF0 and frame[-1] == 0xF7
        assert all(b < 0x80 for b in frame[1:-1]), "SysEx payload must be 7-bit"
        got = reasm.feed(frame[1:-1])
        if got is not None:
            assert reply is None, "exactly one reply expected"
            reply = got
    assert reply is not None, "no complete reply"
    assert reply["id"] == rid
    return reply


def test_ping(fl):
    project, ctrl = fl
    r = roundtrip(project, ctrl, "ping")
    assert r["ok"] and r["result"]["api_version"] == 36
    assert "All Plugins" in r["result"]["fl_version"]
    assert r["result"]["global_index"] is True
    assert project.undo_log == []            # read-only


def test_project_info(fl):
    project, ctrl = fl
    r = roundtrip(project, ctrl, "project_info")["result"]
    assert r == {"tempo": 140.0, "title": "My Song.flp", "window_title": "My Song.flp - FL Studio 2024",
                 "channel_count": 6, "pattern_count": 4, "mixer_track_count": 127}


def test_list_channels_and_colour_order(fl):
    project, ctrl = fl
    project.channels[0]["color"] = 0x0000FF        # BGR int -> red in low byte
    r = roundtrip(project, ctrl, "list_channels")["result"]
    assert r["indexing"] == "global"
    assert r["channels"][0] == {"index": 0, "name": "Kick 909", "color": "#FF0000",
                                "type": "sampler", "plugin": None, "mixer_track": 0}
    assert r["channels"][2]["plugin"] == "Serum" and r["channels"][2]["type"] == "plugin"
    assert project.undo_log == []


def test_list_mixer_tracks_skips_empty(fl):
    project, ctrl = fl
    project.channels[1]["target"] = 5
    r = roundtrip(project, ctrl, "list_mixer_tracks")["result"]
    idx = [t["index"] for t in r["tracks"]]
    assert idx == [0, 1, 5]                        # master, fx on 1, route to 5
    assert r["tracks"][1]["effects"] == [{"slot": 0, "plugin": "Fruity Limiter"}]
    assert r["tracks"][2]["routed_channels"] == 1
    full = roundtrip(project, ctrl, "list_mixer_tracks", {"include_empty": True}, chunk=100)["result"]
    assert len(full["tracks"]) == 127              # multi-chunk reply reassembled


def test_chunking_large_request_and_reply(fl):
    project, ctrl = fl
    # 60-byte chunks force both the request and the reply to span many frames.
    r = roundtrip(project, ctrl, "rename_channel", {"index": 0, "name": "x" * 500}, chunk=60)
    assert r["ok"] and r["result"]["to"] == "x" * 500
    # The reply (~560 bytes) is split by the controller's own CHUNK_PAYLOAD (240).
    assert len(project.sent) == 3
    assert all(len(f) <= 240 + 11 for f in project.sent)


def test_mutations_create_undo_points(fl):
    project, ctrl = fl
    assert roundtrip(project, ctrl, "rename_channel", {"index": 1, "name": "DR Snare"})["ok"]
    assert project.channels[1]["name"] == "DR Snare"
    r = roundtrip(project, ctrl, "set_channel_color", {"index": 1, "color": "#FF0000"})["result"]
    assert r["to"] == "#FF0000" and project.channels[1]["color"] == 0x0000FF
    assert roundtrip(project, ctrl, "route_channel_to_mixer", {"index": 1, "track": 9})["result"]["to"] == 9
    assert roundtrip(project, ctrl, "rename_mixer_track", {"index": 9, "name": "Snare"})["result"]["to"] == "Snare"
    assert roundtrip(project, ctrl, "set_mixer_track_color", {"index": 9, "color": "#00FF00"})["ok"]
    assert project.tracks[9]["color"] == 0x00FF00
    assert len(project.undo_log) == 5 and all(u.startswith("FLMCP: ") for u in project.undo_log)


def test_set_tempo_via_rec_event(fl):
    project, ctrl = fl
    r = roundtrip(project, ctrl, "set_tempo", {"bpm": 128.5})["result"]
    assert r == {"from": 140.0, "to": 128.5, "requested": 128.5}
    bad = roundtrip(project, ctrl, "set_tempo", {"bpm": 9000})
    assert not bad["ok"] and "range" in bad["error"]
    assert len(project.undo_log) == 1            # the failed call made no undo point


def test_errors_are_clean(fl):
    project, ctrl = fl
    r = roundtrip(project, ctrl, "rename_channel", {"index": 99, "name": "nope"})
    assert not r["ok"] and "out of range" in r["error"]
    r = roundtrip(project, ctrl, "exec", {"code": "import os"})
    assert not r["ok"] and "unknown command" in r["error"]
    r = roundtrip(project, ctrl, "set_channel_color", {"index": 0, "color": "red"})
    assert not r["ok"] and "#RRGGBB" in r["error"]
    assert project.undo_log == []                # validation happens before saveUndo


def test_foreign_sysex_is_ignored(fl):
    project, ctrl = fl
    ev = FakeSysExEvent(bytes([0xF0, 0x7E, 0x7F, 0x06, 0x01, 0xF7]))   # universal device inquiry
    ctrl.OnSysEx(ev)
    assert ev.handled is False and project.sent == []


def test_template_plan_and_batch(fl):
    project, ctrl = fl
    template = json.load(open("templates/example_template.json"))
    def snapshot():
        chans = roundtrip(project, ctrl, "list_channels")["result"]["channels"]
        tracks = roundtrip(project, ctrl, "list_mixer_tracks", {"include_empty": True})["result"]["tracks"]
        return chans, tracks

    chans, tracks = snapshot()
    planned = plan(chans, template, 127, tracks)
    assert planned["summary"] == {"Drums": 2, "Bass": 1, "Synths": 1, "Vocals": 1, "FX": 1, "Returns": 0}
    assert planned["unmatched"] == []
    by = {(o["op"], o["index"]): o for o in planned["ops"]}
    assert by[("rename_channel", 0)]["name"] == "DR Kick 909"
    assert by[("route_channel_to_mixer", 1)]["track"] == 2       # second drum -> track 2
    assert by[("route_channel_to_mixer", 3)]["track"] == 11      # Reese -> Bass range start
    assert by[("rename_mixer_track", 11)]["name"] == "BS Reese"

    r = roundtrip(project, ctrl, "batch", {"ops": strip_plan_metadata(planned["ops"]), "label": "template"})
    assert r["ok"] and r["result"]["applied"] == len(planned["ops"])
    assert project.undo_log == ["FLMCP: template"]              # ONE undo point for the lot
    assert project.channels[4]["name"] == "VX Vox lead" and project.channels[4]["target"] == 25

    # Re-planning after apply is a no-op: prefixes are not stacked.
    chans2, tracks2 = snapshot()
    assert plan(chans2, template, 127, tracks2)["ops"] == []


def test_batch_rejects_everything_if_one_op_is_bad(fl):
    project, ctrl = fl
    ops = [{"op": "rename_channel", "index": 0, "name": "ok"},
           {"op": "rename_channel", "index": 42, "name": "bad"}]
    r = roundtrip(project, ctrl, "batch", {"ops": ops})
    assert not r["ok"] and project.channels[0]["name"] == "Kick 909" and project.undo_log == []


def test_server_registers_expected_tools():
    import asyncio
    from fl_mcp.server import mcp
    tools = {t.name: t for t in asyncio.run(mcp.list_tools())}
    assert set(tools) == {"fl_ping", "get_project_info", "list_channels", "list_mixer_tracks",
                          "rename_channel", "set_channel_color", "route_channel_to_mixer",
                          "rename_mixer_track", "set_mixer_track_color", "set_tempo",
                          "apply_organization_template"}
    ro = {"fl_ping", "get_project_info", "list_channels", "list_mixer_tracks"}
    for name, t in tools.items():
        ann = t.annotations
        if isinstance(ann, dict):
            read_only = ann.get("readOnlyHint", ann.get("read_only_hint"))
        else:
            read_only = getattr(ann, "read_only_hint", None)
        assert read_only is (name in ro), name
