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
             "volume": 0.8, "pan": 0.0, "fx": [None] * 10}
            for t in range(n_tracks)
        ]
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
    # dB readout: monotonic stand-in for FL's fader law (0.8 -> 0 dB, 1.0 -> +5.6 dB).
    mx.getTrackVolume = lambda t, mode=0: project.tracks[t]["volume"] if mode == 0 else (project.tracks[t]["volume"] - 0.8) * 28.0
    mx.setTrackVolume = lambda t, v: project.tracks[t].__setitem__("volume", v)
    mx.getTrackPan = lambda t: project.tracks[t]["pan"]
    mx.setTrackPan = lambda t, p: project.tracks[t].__setitem__("pan", p)
    mx.isTrackMuted = lambda t: project.tracks[t].get("muted", False)
    mx.muteTrack = lambda t: project.tracks[t].__setitem__("muted", not project.tracks[t].get("muted", False))
    mx.isTrackSolo = lambda t: project.tracks[t].get("solo", False)
    mx.soloTrack = lambda t: project.tracks[t].__setitem__("solo", not project.tracks[t].get("solo", False))
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
    pl.isValid = isValid
    pl.getPluginName = getPluginName
    m["plugins"] = pl

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
