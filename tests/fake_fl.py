"""
A fake FL Studio: minimal stand-ins for the modules the controller imports
(channels, device, general, midi, mixer, patterns, plugins, ui) backed by a
tiny in-memory project.  Lets us load device_FLMCP.py in normal Python and
drive it end-to-end through the real framing code.

The real `midi` module is taken from fl-studio-api-stubs (installed as a dev
dependency) so the constants are the genuine ones.
"""

from __future__ import annotations

import importlib.util
import sys
import math
import types
from pathlib import Path

import midi as real_midi  # from fl-studio-api-stubs

ROOT = Path(__file__).resolve().parents[1]
CONTROLLER = ROOT / "fl_controller" / "device_FLMCP.py"


class FakeProject:
    def __init__(self, n_channels: int = 6, n_tracks: int = 127) -> None:
        self.tempo = 140.0
        self.undo_log: list[str] = []
        self.channels = [
            {"name": f"Channel {i}", "color": 0x404040, "type": real_midi.CT_Sampler,
             "plugin": None, "target": 0}
            for i in range(n_channels)
        ]
        self.tracks = [
            {"name": "Master" if t == 0 else f"Insert {t}", "color": 0x808080,
             "volume": 0.8, "pan": 0.0, "fx": [None] * 10,
             # routes: dest -> send level; every insert sends to Master by default
             "routes": {} if t == 0 else {0: 0.8}}
            for t in range(n_tracks)
        ]
        self.routing_notified = 0
        self.playlist_tracks = [{"name": "Track %d" % (i + 1), "color": 0x808080} for i in range(500)]
        self.markers: list[tuple[int, str]] = []
        self.params: dict[tuple[int, int], list[dict]] = {}
        self.playing = False
        self.sent: list[bytes] = []        # SysEx the controller emitted


def build_modules(project: FakeProject) -> dict[str, types.ModuleType]:
    """Return {module_name: module} to inject into sys.modules."""
    m: dict[str, types.ModuleType] = {}

    # --- channels ---------------------------------------------------------
    ch = types.ModuleType("channels")

    def _gi(index, use_global):
        # The fake has no groups, so group index == global index.
        if not isinstance(use_global, bool):
            raise TypeError("useGlobalIndex must be bool")
        return index

    ch.channelCount = lambda globalCount=False: len(project.channels)
    ch.getChannelName = lambda i, g=False: project.channels[_gi(i, g)]["name"]
    ch.setChannelName = lambda i, name, g=False: project.channels[_gi(i, g)].__setitem__("name", name)
    ch.getChannelColor = lambda i, g=False: project.channels[_gi(i, g)]["color"]
    ch.setChannelColor = lambda i, c, g=False: project.channels[_gi(i, g)].__setitem__("color", c)
    ch.getChannelType = lambda i, g=False: project.channels[_gi(i, g)]["type"]
    ch.getTargetFxTrack = lambda i, g=False: project.channels[_gi(i, g)]["target"]
    ch.setTargetFxTrack = lambda i, t, g=False: project.channels[_gi(i, g)].__setitem__("target", t)
    m["channels"] = ch

    # --- device -----------------------------------------------------------
    dv = types.ModuleType("device")
    dv.midiOutSysex = lambda data: project.sent.append(bytes(data))
    dv.getPortNumber = lambda: 7
    dv.isAssigned = lambda: True
    m["device"] = dv

    # --- general ----------------------------------------------------------
    gn = types.ModuleType("general")
    gn.getVersion = lambda: 36
    gn.saveUndo = lambda name, flags, update=True: project.undo_log.append(name)
    gn.safeToEdit = lambda: True

    def processRECEvent(event_id, value, flags):
        assert event_id == real_midi.REC_Tempo, "only tempo is wired in the fake"
        project.tempo = value / 1000.0
        return 0
    gn.processRECEvent = processRECEvent
    gn.getRecPPQ = lambda: 96
    m["general"] = gn

    # --- midi (real constants) -------------------------------------------
    m["midi"] = real_midi

    # --- mixer ------------------------------------------------------------
    mx = types.ModuleType("mixer")
    mx.trackCount = lambda: len(project.tracks)
    mx.getCurrentTempo = lambda asInt=False: project.tempo
    mx.getTrackName = lambda t: project.tracks[t]["name"]
    mx.setTrackName = lambda t, n: project.tracks[t].__setitem__("name", n)
    mx.getTrackColor = lambda t: project.tracks[t]["color"]
    mx.setTrackColor = lambda t, c: project.tracks[t].__setitem__("color", c)
    # dB readout uses FL's real fader law (fitted on 24.2.2, see the controller):
    # amplitude = (e^(3v) - 1) / (e^(2.4) - 1); 0.8 -> 0 dB, 1.0 -> +5.59 dB, 0 -> -inf.
    def _db(v):
        return -math.inf if v <= 0 else 20.0 * math.log10((math.exp(3.0 * v) - 1.0) / (math.exp(2.4) - 1.0))
    mx.getTrackVolume = lambda t, mode=0: project.tracks[t]["volume"] if mode == 0 else _db(project.tracks[t]["volume"])
    mx.setTrackVolume = lambda t, v: project.tracks[t].__setitem__("volume", v)
    mx.getTrackPan = lambda t: project.tracks[t]["pan"]
    mx.setTrackPan = lambda t, p: project.tracks[t].__setitem__("pan", p)
    def _toggle_or_set(key):
        def fn(t, value=-1, mode=-1):
            cur = project.tracks[t].get(key, False)
            project.tracks[t][key] = (not cur) if value == -1 else bool(value)
        return fn
    mx.isTrackMuted = lambda t: project.tracks[t].get("muted", False)
    mx.muteTrack = _toggle_or_set("muted")
    mx.isTrackSolo = lambda t: project.tracks[t].get("solo", False)
    mx.soloTrack = _toggle_or_set("solo")

    def setRouteTo(i, d, value, updateUI=False):
        routes = project.tracks[i]["routes"]
        if value:
            routes.setdefault(d, 0.8)
        else:
            routes.pop(d, None)
    mx.setRouteTo = setRouteTo
    mx.getRouteSendActive = lambda i, d: d in project.tracks[i]["routes"]
    mx.setRouteToLevel = lambda i, d, l: project.tracks[i]["routes"].__setitem__(d, l)
    mx.getRouteToLevel = lambda i, d: project.tracks[i]["routes"].get(d, 0.0)
    def afterRoutingChanged():
        project.routing_notified += 1
    mx.afterRoutingChanged = afterRoutingChanged
    mx.getTrackPeaks = lambda t, mode: project.tracks[t].get("peak", 0.0)
    m["mixer"] = mx

    # --- patterns ---------------------------------------------------------
    pt = types.ModuleType("patterns")
    pt.patternCount = lambda: 4
    m["patterns"] = pt

    # --- plugins ----------------------------------------------------------
    pl = types.ModuleType("plugins")

    def isValid(index, slot=-1, use_global=False):
        if slot == -1:
            return project.channels[index]["plugin"] is not None
        return project.tracks[index]["fx"][slot] is not None

    def getPluginName(index, slot=-1, userName=False, use_global=False):
        if slot == -1:
            return project.channels[index]["plugin"]
        return project.tracks[index]["fx"][slot]

    # Parameters live in project.params[(index, slot)] as [{"name", "value"}].
    # A valid plugin with no entry gets three generic ones; name "" mimics the
    # unused slots VSTs report.
    def _params(index, slot):
        key = (index, slot)
        if key not in project.params:
            project.params[key] = [{"name": "Gain", "value": 0.5}, {"name": "", "value": 0.0},
                                   {"name": "Freq", "value": 0.25}]
        return project.params[key]

    pl.isValid = isValid
    pl.getPluginName = getPluginName
    pl.getParamCount = lambda index, slot=-1, g=False: len(_params(index, slot))
    pl.getParamName = lambda p, index, slot=-1, g=False: _params(index, slot)[p]["name"]
    pl.getParamValue = lambda p, index, slot=-1, g=False: _params(index, slot)[p]["value"]
    pl.setParamValue = lambda v, p, index, slot=-1, pickup=0, g=False: _params(index, slot)[p].__setitem__("value", v)
    pl.getParamValueString = lambda p, index, slot=-1, pickup=0, g=False: "%d%%" % round(100 * _params(index, slot)[p]["value"])
    m["plugins"] = pl

    # --- arrangement markers ------------------------------------------------
    ar = types.ModuleType("arrangement")
    ar.addAutoTimeMarker = lambda t, name: project.markers.append((t, name))
    m["arrangement"] = ar

    # --- playlist (1-indexed tracks) ---------------------------------------
    pls = types.ModuleType("playlist")
    pls.trackCount = lambda: len(project.playlist_tracks)
    pls.getTrackName = lambda i: project.playlist_tracks[i - 1]["name"]
    pls.setTrackName = lambda i, n: project.playlist_tracks[i - 1].__setitem__("name", n)
    pls.getTrackColor = lambda i: project.playlist_tracks[i - 1]["color"]
    pls.setTrackColor = lambda i, c: project.playlist_tracks[i - 1].__setitem__("color", c)
    pls.isTrackMuted = lambda i: project.playlist_tracks[i - 1].get("muted", False)
    pls.isTrackSolo = lambda i: project.playlist_tracks[i - 1].get("solo", False)
    m["playlist"] = pls

    # --- transport -------------------------------------------------------
    tr = types.ModuleType("transport")
    tr.isPlaying = lambda: project.playing
    m["transport"] = tr

    # --- ui ---------------------------------------------------------------
    ui = types.ModuleType("ui")
    ui.getProgTitle = lambda: "My Song.flp - FL Studio 2024"
    ui.getVersion = lambda mode=4: "Producer Edition v24.2.2 [build 4597] - All Plugins Edition - 64Bit"
    m["ui"] = ui
    return m


def load_controller(project: FakeProject):
    """Import device_FLMCP.py against the fake modules; returns the module."""
    for name, mod in build_modules(project).items():
        sys.modules[name] = mod
    spec = importlib.util.spec_from_file_location("device_FLMCP", CONTROLLER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeSysExEvent:
    def __init__(self, data: bytes) -> None:
        self.sysex = data
        self.handled = False
