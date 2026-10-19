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
    project.channels[0]["color"] = 0xFF0000        # RGB int (COLOR_ORDER = "RGB", confirmed on real FL 24.2.2)
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
    assert r["to"] == "#FF0000" and project.channels[1]["color"] == 0xFF0000
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


def test_mixer_volume_pan_mute_solo(fl):
    project, ctrl = fl
    r = roundtrip(project, ctrl, "set_mixer_track_volume", {"index": 3, "volume": 0.5})["result"]
    assert r["from"]["volume"] == 0.8 and r["to"]["volume"] == 0.5
    r = roundtrip(project, ctrl, "set_mixer_track_volume", {"index": 3, "volume_db": -6.0})["result"]
    assert abs(r["to"]["volume_db"] - -6.0) < 0.01 and r["set"] == {"volume": 0.5986, "volume_db": -6.0}   # FL's own bisection gave 0.5984; same dB
    assert r["readback_stale"] is False
    r = roundtrip(project, ctrl, "set_mixer_track_volume", {"index": 3, "volume": 0})["result"]
    assert r["to"]["volume_db"] is None and r["set"]["volume_db"] is None   # silence, not -inf
    bad = roundtrip(project, ctrl, "set_mixer_track_volume", {"index": 3, "volume_db": 20})
    assert not bad["ok"] and "maximum" in bad["error"]
    assert not roundtrip(project, ctrl, "set_mixer_track_volume", {"index": 3})["ok"]
    assert roundtrip(project, ctrl, "set_mixer_track_pan", {"index": 3, "pan": -0.5})["result"]["to"]["pan"] == -0.5
    assert not roundtrip(project, ctrl, "set_mixer_track_pan", {"index": 3, "pan": 2})["ok"]
    assert roundtrip(project, ctrl, "set_mixer_track_mute", {"index": 3, "muted": True})["result"]["to"]["muted"]
    # idempotent: muting an already-muted track must not toggle it back
    assert roundtrip(project, ctrl, "set_mixer_track_mute", {"index": 3, "muted": True})["result"]["to"]["muted"]
    assert project.tracks[3]["muted"] is True
    assert roundtrip(project, ctrl, "set_mixer_track_solo", {"index": 3, "solo": True})["result"]["to"]["solo"]
    assert not roundtrip(project, ctrl, "set_mixer_track_solo", {"index": 3, "solo": False})["result"]["to"]["solo"]
    listed = roundtrip(project, ctrl, "list_mixer_tracks", {"include_empty": True})["result"]["tracks"][3]
    assert listed["muted"] is True and listed["solo"] is False
    assert listed["routes"] == [{"to": 0, "level": 0.8}]


def test_fader_law_matches_fl_24_2_2(fl):
    """Fader values and dB readouts measured on real FL Studio 24.2.2."""
    _, ctrl = fl
    measured = {0.8: 0.0, 1.0: 5.59, 0.5984: -6.0, 0.5357: -8.0, 0.5054: -9.0, 0.4758: -10.0,
                0.4471: -11.0, 0.4191: -12.0, 0.366: -14.0, 0.6464: -4.52, 0.8328: 0.94}
    for v, db in measured.items():
        assert abs(ctrl.fader_to_db(v) - db) < 0.02, (v, db, ctrl.fader_to_db(v))
        assert abs(ctrl.db_to_fader(db) - v) < 0.0006, (v, db, ctrl.db_to_fader(db))
    assert ctrl.fader_to_db(0) is None
    assert abs(ctrl.FADER_MAX_DB - 5.59) < 0.01


def test_mixer_routing_and_bus(fl):
    project, ctrl = fl
    r = roundtrip(project, ctrl, "set_mixer_route", {"source": 2, "dest": 20, "level": 0.5})["result"]
    assert r["routes_before"] == [{"to": 0, "level": 0.8}]
    assert r["routes_after"] == [{"to": 0, "level": 0.8}, {"to": 20, "level": 0.5}]
    assert project.routing_notified == 1
    r = roundtrip(project, ctrl, "set_mixer_route", {"source": 2, "dest": 0, "enabled": False})["result"]
    assert r["routes_after"] == [{"to": 20, "level": 0.5}]
    for bad in ({"source": 0, "dest": 3}, {"source": 3, "dest": 3}, {"source": 3, "dest": 4, "level": 2}):
        assert not roundtrip(project, ctrl, "set_mixer_route", bad)["ok"]

    r = roundtrip(project, ctrl, "create_bus",
                  {"bus": 30, "sources": [1, 3], "name": "Drums", "color": "#E74C3C"})["result"]
    assert r["routes"] == {"1": [{"to": 30, "level": 0.8}], "3": [{"to": 30, "level": 0.8}]}
    assert r["bus_routes"] == [{"to": 0, "level": 0.8}]          # the bus still feeds Master
    assert project.tracks[30]["name"] == "Drums" and project.tracks[30]["color"] == 0xE74C3C
    assert project.undo_log[-1] == "FLMCP: create bus on mixer track 30"   # one undo point
    # the bus has no effects and no channels, but it is fed by tracks, so it is listed
    listed = {t["index"]: t for t in roundtrip(project, ctrl, "list_mixer_tracks")["result"]["tracks"]}
    assert listed[30]["fed_by"] == [1, 3] and listed[1]["fed_by"] == []
    assert 20 in listed and listed[20]["fed_by"] == [2]
    r = roundtrip(project, ctrl, "create_bus", {"bus": 31, "sources": [4], "keep_master": True})["result"]
    assert r["routes"]["4"] == [{"to": 0, "level": 0.8}, {"to": 31, "level": 0.8}]
    for bad in ({"bus": 0, "sources": [1]}, {"bus": 5, "sources": [5]}, {"bus": 5, "sources": []},
                {"bus": 5, "sources": [1, 1]}, {"bus": 5, "sources": [1], "color": "red"}):
        assert not roundtrip(project, ctrl, "create_bus", bad)["ok"]


def test_plugin_params(fl):
    project, ctrl = fl
    r = roundtrip(project, ctrl, "list_plugin_params", {"track": 1, "slot": 0})["result"]
    assert r["plugin"] == "Fruity Limiter" and r["param_count"] == 3 and not r["truncated"]
    assert [p["index"] for p in r["params"]] == [0, 2]            # unnamed slot skipped
    assert r["params"][0] == {"index": 0, "name": "Gain", "value": 0.5, "value_string": "50%"}
    r = roundtrip(project, ctrl, "list_plugin_params", {"track": 1, "slot": 0, "filter": "FREQ"})["result"]
    assert [p["name"] for p in r["params"]] == ["Freq"]
    r = roundtrip(project, ctrl, "list_plugin_params", {"track": 1, "slot": 0, "max_params": 1})["result"]
    assert len(r["params"]) == 1 and r["truncated"]
    r = roundtrip(project, ctrl, "list_plugin_params", {"channel": 2})["result"]
    assert r["plugin"] == "Serum" and r["channel"] == 2
    for bad in ({"track": 1, "slot": 3}, {"channel": 0}, {"channel": 2, "track": 1, "slot": 0},
                {"track": 1, "slot": 10}):
        assert not roundtrip(project, ctrl, "list_plugin_params", bad)["ok"]

    r = roundtrip(project, ctrl, "set_plugin_param", {"track": 1, "slot": 0, "param": "gain", "value": 0.75})["result"]
    assert r["param"] == 0 and r["from"]["value"] == 0.5 and r["to"] == {"value": 0.75, "value_string": "75%"}
    assert r["readback_stale"] is False
    assert project.params[(1, 0)][0]["value"] == 0.75
    r = roundtrip(project, ctrl, "set_plugin_param", {"track": 1, "slot": 0, "param": 2, "value": 0})["result"]
    assert r["name"] == "Freq" and r["to"]["value"] == 0
    assert project.undo_log[-1] == "FLMCP: set Freq on Fruity Limiter"
    for bad in ({"param": "nope", "value": 0.5}, {"param": 0, "value": 2}, {"param": 7, "value": 0.5},
                {"param": "", "value": 0.5}):
        bad.update(track=1, slot=0)
        assert not roundtrip(project, ctrl, "set_plugin_param", bad)["ok"]
    assert len(project.undo_log) == 2


def test_playlist_tracks(fl):
    project, ctrl = fl
    r = roundtrip(project, ctrl, "list_playlist_tracks", {"limit": 3})["result"]
    assert r["total_tracks"] == 500 and [t["index"] for t in r["tracks"]] == [1, 2, 3]
    assert r["tracks"][0] == {"index": 1, "name": "Track 1", "color": "#808080", "muted": False, "solo": False}
    r = roundtrip(project, ctrl, "set_playlist_track", {"index": 2, "name": "Kick", "color": "#E74C3C"})["result"]
    assert r["renamed"] == {"index": 2, "from": "Track 2", "to": "Kick"} and r["colored"]["to"] == "#E74C3C"
    assert project.undo_log[-1] == "FLMCP: set playlist track 2"
    for bad in ({"index": 0, "name": "x"}, {"index": 501, "name": "x"}, {"index": 1}, {"index": 1, "color": "red"}):
        assert not roundtrip(project, ctrl, "set_playlist_track", bad)["ok"]
    ops = [{"op": "rename_playlist_track", "index": 5, "name": "Snare"},
           {"op": "set_playlist_track_color", "index": 5, "color": "#00FF00"}]
    r = roundtrip(project, ctrl, "batch", {"ops": ops, "label": "pl"})["result"]
    assert r["applied"] == 2 and project.playlist_tracks[4] == {"name": "Snare", "color": 0x00FF00}
    assert not roundtrip(project, ctrl, "batch", {"ops": [{"op": "rename_playlist_track", "index": 0, "name": "x"}]})["ok"]
    # named tracks past the limit are still listed; default-named ones are not
    listed = roundtrip(project, ctrl, "list_playlist_tracks", {"limit": 3})["result"]["tracks"]
    assert [t["index"] for t in listed] == [1, 2, 3]


def test_track_peaks(fl):
    project, ctrl = fl
    project.tracks[1]["peak"] = 0.9
    project.playing = True
    r = roundtrip(project, ctrl, "track_peaks", {"tracks": [0, 1]})["result"]
    assert r["playing"] is True
    assert r["peaks"] == [{"index": 0, "name": "Master", "peak": 0.0}, {"index": 1, "name": "Insert 1", "peak": 0.9}]
    assert len(roundtrip(project, ctrl, "track_peaks")["result"]["peaks"]) == 127
    assert not roundtrip(project, ctrl, "track_peaks", {"tracks": [200]})["ok"]
    assert project.undo_log == []                 # metering is read-only


def test_solo_mute_without_undo(fl):
    project, ctrl = fl
    assert roundtrip(project, ctrl, "set_mixer_track_solo", {"index": 2, "solo": True, "mode": 1, "undo": False})["ok"]
    assert project.tracks[2]["solo"] is True
    assert roundtrip(project, ctrl, "set_mixer_track_mute", {"index": 2, "muted": True, "undo": False})["ok"]
    assert project.undo_log == []
    assert not roundtrip(project, ctrl, "set_mixer_track_solo", {"index": 2, "solo": True, "mode": 9})["ok"]
    assert roundtrip(project, ctrl, "set_mixer_track_solo", {"index": 2, "solo": False})["ok"]
    assert project.undo_log == ["FLMCP: solo mixer track 2"]


def test_analyze_live_solos_records_and_restores(monkeypatch):
    import numpy as np
    from fl_mcp import server

    calls: list[tuple] = []
    tracks = [
        {"index": 0, "name": "Master", "routed_channels": 5, "solo": False, "muted": False},
        {"index": 1, "name": "kick", "routed_channels": 1, "solo": False, "muted": False},
        {"index": 2, "name": "pad", "routed_channels": 1, "solo": False, "muted": True},
        {"index": 3, "name": "Insert 3", "routed_channels": 0, "solo": False, "muted": False},
        {"index": 4, "name": "bus", "routed_channels": 0, "fed_by": [1], "solo": False, "muted": False},
    ]

    def fake_call(cmd, **a):
        calls.append((cmd, a))
        if cmd == "list_mixer_tracks":
            return {"tracks": tracks}
        if cmd == "track_peaks":
            return {"playing": True, "peaks": []}
        if cmd == "list_channels":
            return {"channels": [{"index": 0, "name": "808 Kick", "type": "sampler", "mixer_track": 0},
                                 {"index": 1, "name": "auto", "type": "automation_clip", "mixer_track": 0}]}
        return {}

    rate = 48000
    t = np.arange(rate) / rate
    bleed = 0.1 * np.sin(2 * np.pi * 60 * t)                                     # 808 on Master
    recordings = iter([
        0.5 * np.sin(2 * np.pi * 45 * t) + 0.2 * np.sin(2 * np.pi * 2000 * t),   # mix
        bleed,                                                                    # spare insert 3 soloed
        0.5 * np.sin(2 * np.pi * 45 * t) + bleed,                                # kick
        0.3 * np.sin(2 * np.pi * 2000 * t) + bleed,                              # pad
        bleed,                                                                    # bus: nothing of its own
    ])
    monkeypatch.setattr(server, "_call", fake_call)
    monkeypatch.setattr(server.live, "record", lambda s, d=None: (next(recordings).astype(np.float32), rate))
    monkeypatch.setattr(server.time, "sleep", lambda s: None)

    fn = getattr(server.analyze_live, "fn", server.analyze_live)
    r = fn(seconds=1.0)
    assert [x["name"] for x in r["tracks"]] == ["kick", "pad", "bus"]      # empty Insert 3 skipped
    assert "sub" in r["mix"]["dominant"] and r["tracks"][0]["dominant"] == ["sub"]
    assert r["tracks"][1]["dominant"] == ["high_mid"]                       # bleed subtracted
    assert r["tracks"][2]["silent"] and r["tracks"][2]["bleed_only"]
    assert r["master_direct"]["channels"] == [{"index": 0, "name": "808 Kick"}]   # automation clip excluded
    assert r["tracks"][0]["above_bleed_db"] > 10 and len(r["warnings"]) == 1 and "808 Kick" in r["warnings"][0]
    assert r["clashes"] == [] and r["restored"] == {"solo": [], "muted": [2]}

    solo_calls = [(a["index"], a["solo"]) for c, a in calls if c == "set_mixer_track_solo"]
    assert solo_calls[:8] == [(3, True), (3, False), (1, True), (1, False), (2, True), (2, False), (4, True), (4, False)]
    assert all(a["undo"] is False and a["mode"] == 3 for c, a in calls if c == "set_mixer_track_solo")
    assert calls[-1] == ("set_mixer_track_mute", {"index": 2, "muted": True, "undo": False})   # mute restored

    tracks[0]["solo"] = False
    monkeypatch.setattr(server, "_call", lambda cmd, **a: {"playing": False} if cmd == "track_peaks" else {"tracks": tracks})
    with pytest.raises(RuntimeError, match="not playing"):
        fn()


def test_get_track_levels_aggregates(monkeypatch):
    from fl_mcp import server
    snaps = iter([
        {"playing": False, "peaks": [{"index": 0, "name": "Master", "peak": 0.5}, {"index": 1, "name": "kick", "peak": 0.0}]},
        {"playing": True, "peaks": [{"index": 0, "name": "Master", "peak": 1.2}, {"index": 1, "name": "kick", "peak": 0.0}]},
    ])
    monkeypatch.setattr(server, "_call", lambda cmd, **a: next(snaps))
    monkeypatch.setattr(server.time, "sleep", lambda s: None)
    fn = getattr(server.get_track_levels, "fn", server.get_track_levels)   # FunctionTool or plain fn
    r = fn(duration_s=0.2, interval_s=0.1)
    assert r["playing"] is True and r["samples"] == 2
    assert r["tracks"] == [{"index": 0, "name": "Master", "peak": 1.2, "peak_db": server.meter_to_dbfs(1.2),
                            "avg": 0.85, "avg_db": server.meter_to_dbfs(0.85), "clipping": True}]   # silent kick dropped


def test_meter_scale_calibration():
    from fl_mcp.server import meter_to_dbfs
    # anchors measured on FL 24.2.2 (kick clip, sample peak -0.7 dBFS, fader stepped)
    assert meter_to_dbfs(0.9798) == -0.7 and meter_to_dbfs(0.8027) == -6.7 and meter_to_dbfs(0.3272) == -24.7
    assert abs(meter_to_dbfs(0.847) - (-5.2)) < 0.2          # between anchors
    assert meter_to_dbfs(1.2) > 4.3 and meter_to_dbfs(0.2) < -24.7   # extrapolates both ways
    assert meter_to_dbfs(0) is None
    # six dB of fader is about 0.17 of meter, NOT a factor of two in the raw value
    assert abs((meter_to_dbfs(0.8027) - meter_to_dbfs(0.6301)) - 6.0) < 0.01


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
                          "set_mixer_track_volume", "set_mixer_track_pan",
                          "set_mixer_track_mute", "set_mixer_track_solo",
                          "set_mixer_route", "create_bus",
                          "list_plugin_params", "set_plugin_param", "get_track_levels", "analyze_stems",
                          "list_audio_devices", "analyze_live",
                          "list_playlist_tracks", "set_playlist_track", "setup_playlist_tracks",
                          "apply_organization_template"}
    ro = {"fl_ping", "get_project_info", "list_channels", "list_mixer_tracks",
          "list_plugin_params", "get_track_levels", "analyze_stems", "list_audio_devices",
          "list_playlist_tracks"}
    for name, t in tools.items():
        ann = t.annotations
        if isinstance(ann, dict):
            read_only = ann.get("readOnlyHint", ann.get("read_only_hint"))
        else:
            read_only = getattr(ann, "read_only_hint", None)
        assert read_only is (name in ro), name
