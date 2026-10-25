# name=FLMCP
# url=https://github.com/soniyuvraj16/fl-mcp
"""
FLMCP controller script -- runs INSIDE FL Studio's embedded Python.

How it fits together
--------------------
FL Studio loads this file as a "MIDI controller script" and calls the
OnInit / OnSysEx / OnDeInit hooks defined at the bottom.  The MCP server
(running in normal Python outside FL) talks to us by sending MIDI SysEx
messages into the loopMIDI port "FLMCP_In".  We reply with
device.midiOutSysex(), which FL routes to whichever output device shares our
Port number -- that is the loopMIDI port "FLMCP_Out".

Why SysEx?  FL's embedded Python has no sockets or threads, so MIDI is the
only channel the host gives us.  SysEx is the only MIDI message type that can
carry an arbitrary-length byte payload, with one catch: every payload byte
must be 7-bit (0x00-0x7F).  We solve that by only ever sending JSON produced
with ensure_ascii=True, which is pure printable ASCII (0x20-0x7E).

Framing (identical in fl_mcp/protocol.py on the server side)
------------------------------------------------------------
    F0 7D 46 4C  <fid hi> <fid lo>  <idx hi> <idx lo>  <tot hi> <tot lo>  payload...  F7
     |  |  |  |    frame id (14-bit)  chunk index        chunk count
     |  |  'F','L' sub-id so other 0x7D devices don't collide with us
     |  0x7D = manufacturer id reserved for non-commercial / educational use
     SysEx start                                                        SysEx end

A JSON message bigger than CHUNK_PAYLOAD bytes is split across several frames
that share a frame id; the receiver reassembles them in order.

Safety rules baked into this file
---------------------------------
* There is no exec/eval.  Only the names in COMMANDS can be called, and each
  one only touches the specific FL API functions you see below.
* Every mutating command goes through _mutating(), which creates an undo point
  (general.saveUndo) *before* changing anything, so Ctrl+Z reverts it.
* Read-only commands never call a set*/process* function.
* Nothing happens in OnIdle.  All work is done inside OnSysEx, which FL calls
  only when a message arrives.
"""

import json
import math

import arrangement
import channels
import device
import general
import midi
import mixer
import patterns
import playlist
import plugins
import transport
import ui

# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------

MANUFACTURER_ID = 0x7D          # "non-commercial" manufacturer id
SUB_ID = (0x46, 0x4C)           # 'F', 'L'
HEADER_LEN = 10                 # F0 7D 46 4C fid fid idx idx tot tot
CHUNK_PAYLOAD = 240             # payload bytes per SysEx frame we *send*

# Flags passed to general.saveUndo().  UF_None is the generic "project
# changed" entry.  If Ctrl+Z does not revert a rename during the test
# checklist, try midi.UF_Knob here and re-test.
UNDO_FLAGS = midi.UF_None

# FL stores colours as a Delphi TColor: 0x00BBGGRR (red in the LOW byte).
# The stubs' utils.ColorToRGB assumes the opposite order, so we do the
# conversion ourselves and make the order a single switch.  Step 6 of
# docs/TEST_CHECKLIST.md tells you how to confirm it on your machine.
COLOR_ORDER = "RGB"             # or "RGB"

# FL's own tempo limits (Project settings dialog).
TEMPO_MIN = 10.0
TEMPO_MAX = 522.0

# Mixer effect slots per track.
FX_SLOTS = 10

CHANNEL_TYPE_NAMES = {
    midi.CT_Sampler: "sampler",
    midi.CT_Hybrid: "hybrid",         # same value as CT_TS404
    midi.CT_GenPlug: "plugin",
    midi.CT_Layer: "layer",
    midi.CT_AudioClip: "audio_clip",
    midi.CT_AutoClip: "automation_clip",
}

# ---------------------------------------------------------------------------
# Colour helpers
# ---------------------------------------------------------------------------

def color_to_hex(value):
    """FL int colour -> '#RRGGBB'."""
    value &= 0xFFFFFF
    if COLOR_ORDER == "BGR":
        r, g, b = value & 0xFF, (value >> 8) & 0xFF, (value >> 16) & 0xFF
    else:
        r, g, b = (value >> 16) & 0xFF, (value >> 8) & 0xFF, value & 0xFF
    return "#%02X%02X%02X" % (r, g, b)


def hex_to_color(text):
    """'#RRGGBB' (or 'RRGGBB') -> FL int colour.  Raises ValueError."""
    s = str(text).strip().lstrip("#")
    if len(s) != 6:
        raise ValueError("color must be #RRGGBB, got %r" % (text,))
    r, g, b = int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16)
    if COLOR_ORDER == "BGR":
        return (b << 16) | (g << 8) | r
    return (r << 16) | (g << 8) | b

# ---------------------------------------------------------------------------
# Channel indexing compatibility
# ---------------------------------------------------------------------------
# The useGlobalIndex flag (API v33+) lets us address channels by their
# absolute position regardless of which channel-rack group is displayed.
# Older APIs raise TypeError on the extra argument, so we probe once and
# remember.  When unsupported we fall back to group-relative indexing, which
# is only correct while the "All" group is shown in the channel rack.

_global_index_supported = None  # None = not probed yet


def use_global_index():
    global _global_index_supported
    if _global_index_supported is None:
        try:
            if channels.channelCount(True) > 0:
                channels.getChannelName(0, True)
            _global_index_supported = True
        except TypeError:
            _global_index_supported = False
    return _global_index_supported


def channel_count():
    try:
        return channels.channelCount(True)
    except TypeError:
        return channels.channelCount()


def _ch(fn, index, *extra):
    """Call a channels.*/plugins.* function with the global-index flag if we can."""
    if use_global_index():
        return fn(index, *(extra + (True,)))
    return fn(index, *extra)


def _check_channel(index):
    n = channel_count()
    if not isinstance(index, int) or index < 0 or index >= n:
        raise ValueError("channel index %r out of range 0..%d" % (index, n - 1))


def _check_track(index):
    n = mixer.trackCount()
    if not isinstance(index, int) or index < 0 or index >= n:
        raise ValueError("mixer track %r out of range 0..%d" % (index, n - 1))

# ---------------------------------------------------------------------------
# Undo + mutation guard
# ---------------------------------------------------------------------------

def _mutating(label):
    """Create an undo point.  Called at the top of every command that edits."""
    if hasattr(general, "safeToEdit"):
        try:
            if not general.safeToEdit():
                raise RuntimeError("FL Studio reports it is not safe to edit right now")
        except TypeError:
            pass
    general.saveUndo("FLMCP: " + label, UNDO_FLAGS, True)

# ---------------------------------------------------------------------------
# Read-only commands
# ---------------------------------------------------------------------------

def cmd_ping(args):
    info = {"api_version": general.getVersion()}
    try:
        info["fl_version"] = ui.getVersion(midi.VER_FullVersionAndEdition)
    except Exception as e:
        info["fl_version"] = "unavailable (%s)" % e
    info["global_index"] = use_global_index()
    info["port"] = device.getPortNumber()
    return info


def _tempo_bpm():
    """mixer.getCurrentTempo() -- normalise whichever unit FL hands us."""
    t = mixer.getCurrentTempo()
    try:
        t = float(t)
    except (TypeError, ValueError):
        return None
    # Some builds report BPM*1000; anything above FL's max is that case.
    if t > TEMPO_MAX:
        t = t / 1000.0
    return round(t, 3)


def _project_title():
    """There is no getProjectTitle(); parse the window title instead."""
    raw = ui.getProgTitle()
    title = raw
    for sep in (" - FL Studio", " - Fruity"):
        if sep in raw:
            title = raw.split(sep)[0]
            break
    return title.strip(), raw


def cmd_project_info(args):
    title, raw = _project_title()
    return {
        "tempo": _tempo_bpm(),
        "title": title,
        "window_title": raw,
        "channel_count": channel_count(),
        "pattern_count": patterns.patternCount(),
        "mixer_track_count": mixer.trackCount(),
    }


def _channel_plugin_name(i):
    try:
        if _ch(plugins.isValid, i, -1):
            return _ch(plugins.getPluginName, i, -1, False)
    except Exception:
        pass
    return None


def cmd_list_channels(args):
    out = []
    for i in range(channel_count()):
        ctype = _ch(channels.getChannelType, i)
        out.append({
            "index": i,
            "name": _ch(channels.getChannelName, i),
            "color": color_to_hex(_ch(channels.getChannelColor, i)),
            "type": CHANNEL_TYPE_NAMES.get(ctype, "unknown(%s)" % ctype),
            "plugin": _channel_plugin_name(i),
            "mixer_track": _ch(channels.getTargetFxTrack, i),
        })
    return {"indexing": "global" if use_global_index() else "group", "channels": out}


def cmd_list_mixer_tracks(args):
    include_empty = bool(args.get("include_empty", False))
    # Count channel routes per track in one pass so "empty" is meaningful.
    routed = {}
    for i in range(channel_count()):
        t = _ch(channels.getTargetFxTrack, i)
        routed[t] = routed.get(t, 0) + 1

    # Track-to-track sends, so a bus (fed only by other tracks) is not "empty".
    routes = {t: _track_routes(t) for t in range(mixer.trackCount())}
    fed_by = {}
    for src, dests in routes.items():
        for d in dests:
            if d["to"] != 0:                 # everything feeds Master by default
                fed_by.setdefault(d["to"], []).append(src)

    out = []
    for t in range(mixer.trackCount()):
        effects = []
        for slot in range(FX_SLOTS):
            try:
                if plugins.isValid(t, slot):
                    effects.append({"slot": slot, "plugin": plugins.getPluginName(t, slot)})
            except Exception:
                pass
        n_routed = routed.get(t, 0)
        sources = fed_by.get(t, [])
        if not include_empty and t != 0 and not effects and n_routed == 0 and not sources:
            continue
        out.append({
            "index": t,
            "name": mixer.getTrackName(t),
            "color": color_to_hex(mixer.getTrackColor(t)),
            "volume": round(mixer.getTrackVolume(t), 4),
            "volume_db": _round_db(mixer.getTrackVolume(t, 1)),
            "pan": round(mixer.getTrackPan(t), 4),
            "muted": bool(mixer.isTrackMuted(t)),
            "solo": bool(mixer.isTrackSolo(t)),
            "routed_channels": n_routed,
            "routes": routes[t],
            "fed_by": sources,
            "effects": effects,
        })
    return {"tracks": out, "total_tracks": mixer.trackCount()}

# ---------------------------------------------------------------------------
# Mutating commands.  Each one: validate -> undo point -> change -> report.
# The *_apply helpers do the change without an undo point so cmd_batch can
# wrap many of them in ONE undo step.
# ---------------------------------------------------------------------------

def _rename_channel_apply(index, name):
    _check_channel(index)
    old = _ch(channels.getChannelName, index)
    _ch(channels.setChannelName, index, str(name))
    return {"index": index, "from": old, "to": _ch(channels.getChannelName, index)}


def _set_channel_color_apply(index, color):
    _check_channel(index)
    old = color_to_hex(_ch(channels.getChannelColor, index))
    _ch(channels.setChannelColor, index, hex_to_color(color))
    return {"index": index, "from": old, "to": color_to_hex(_ch(channels.getChannelColor, index))}


def _route_channel_apply(index, track):
    _check_channel(index)
    _check_track(track)
    old = _ch(channels.getTargetFxTrack, index)
    _ch(channels.setTargetFxTrack, index, int(track))
    return {"index": index, "from": old, "to": _ch(channels.getTargetFxTrack, index)}


def _rename_track_apply(index, name):
    _check_track(index)
    old = mixer.getTrackName(index)
    mixer.setTrackName(index, str(name))
    return {"index": index, "from": old, "to": mixer.getTrackName(index)}


def _set_track_color_apply(index, color):
    _check_track(index)
    old = color_to_hex(mixer.getTrackColor(index))
    mixer.setTrackColor(index, hex_to_color(color))
    return {"index": index, "from": old, "to": color_to_hex(mixer.getTrackColor(index))}


def cmd_rename_channel(args):
    _check_channel(args.get("index"))
    _mutating("rename channel %s" % args["index"])
    return _rename_channel_apply(args["index"], args.get("name", ""))


def cmd_set_channel_color(args):
    _check_channel(args.get("index"))
    hex_to_color(args.get("color"))          # validate before the undo point
    _mutating("colour channel %s" % args["index"])
    return _set_channel_color_apply(args["index"], args["color"])


def cmd_route_channel_to_mixer(args):
    _check_channel(args.get("index"))
    _check_track(args.get("track"))
    _mutating("route channel %s -> track %s" % (args["index"], args["track"]))
    return _route_channel_apply(args["index"], args["track"])


def cmd_rename_mixer_track(args):
    _check_track(args.get("index"))
    _mutating("rename mixer track %s" % args["index"])
    return _rename_track_apply(args["index"], args.get("name", ""))


def cmd_set_mixer_track_color(args):
    _check_track(args.get("index"))
    hex_to_color(args.get("color"))
    _mutating("colour mixer track %s" % args["index"])
    return _set_track_color_apply(args["index"], args["color"])


def cmd_set_tempo(args):
    """
    There is no transport.setTempo() in the MIDI scripting API (verified
    against fl-studio-api-stubs 37).  The stubs themselves document the
    supported route: fire the REC event for the global tempo parameter.
    REC_Tempo stores BPM * 1000; REC_Control|REC_UpdateControl sets the value
    AND refreshes the on-screen control.
    """
    bpm = args.get("bpm")
    try:
        bpm = float(bpm)
    except (TypeError, ValueError):
        raise ValueError("bpm must be a number, got %r" % (bpm,))
    if not (TEMPO_MIN <= bpm <= TEMPO_MAX):
        raise ValueError("bpm %.3f outside FL's range %g..%g" % (bpm, TEMPO_MIN, TEMPO_MAX))
    before = _tempo_bpm()
    _mutating("set tempo %.3f" % bpm)
    general.processRECEvent(
        midi.REC_Tempo,
        int(round(bpm * 1000)),
        midi.REC_Control | midi.REC_UpdateControl,
    )
    return {"from": before, "to": _tempo_bpm(), "requested": bpm}


# FL's mixer fader law, fitted on FL Studio 24.2.2 against the dB readout
# (mixer.getTrackVolume(index, 1)); 13 points across the range all agree
# within 0.01 dB:  amplitude = (e^(3v) - 1) / (e^(2.4) - 1).  So v = 0.8 is
# unity (0 dB), v = 1.0 is +5.59 dB and v = 0 is silence.  We compute the
# fader value from this instead of searching against the readout, because
# FL applies setTrackVolume on its next tick on some tracks and a read in the
# same callback can still return the old value.
_FADER_K = 3.0
_FADER_DEN = math.exp(_FADER_K * 0.8) - 1.0


def fader_to_db(v):
    """0..1 fader value -> dB (None for silence)."""
    if v <= 0.0:
        return None
    return 20.0 * math.log10((math.exp(_FADER_K * v) - 1.0) / _FADER_DEN)


def db_to_fader(db):
    """dB -> 0..1 fader value (inverse of fader_to_db)."""
    return math.log(1.0 + (10.0 ** (db / 20.0)) * _FADER_DEN) / _FADER_K


FADER_MAX_DB = fader_to_db(1.0)


def _round_db(x):
    """FL reports -inf dB for a silent fader; JSON has no -inf, so use None."""
    x = float(x)
    return round(x, 2) if math.isfinite(x) else None


def _track_state(index):
    return {
        "index": index,
        "volume": round(mixer.getTrackVolume(index), 4),
        "volume_db": _round_db(mixer.getTrackVolume(index, 1)),
        "pan": round(mixer.getTrackPan(index), 4),
        "muted": bool(mixer.isTrackMuted(index)),
        "solo": bool(mixer.isTrackSolo(index)),
    }


def cmd_set_mixer_track_volume(args):
    index = args.get("index")
    _check_track(index)
    has_vol, has_db = args.get("volume") is not None, args.get("volume_db") is not None
    if has_vol == has_db:
        raise ValueError("give exactly one of volume (0..1) or volume_db")
    if has_vol:
        vol = float(args["volume"])
        if not (0.0 <= vol <= 1.0):
            raise ValueError("volume must be 0..1 (0.8 = 0 dB), got %r" % (vol,))
    else:
        db = float(args["volume_db"])
        if db > FADER_MAX_DB:
            raise ValueError("volume_db %.2f above FL's fader maximum %.2f" % (db, FADER_MAX_DB))
        vol = db_to_fader(db)
    before = _track_state(index)
    _mutating("set volume mixer track %s" % index)
    mixer.setTrackVolume(index, vol)
    after = _track_state(index)
    db_set = fader_to_db(vol)
    return {
        "from": before,
        "to": after,
        "set": {"volume": round(vol, 4), "volume_db": None if db_set is None else round(db_set, 2)},
        # FL sometimes applies the fader on its next tick; when the read-back
        # does not match what we just set, "to" is stale and "set" is the truth.
        "readback_stale": abs(after["volume"] - vol) > 1e-3,
    }


def cmd_set_mixer_track_pan(args):
    index = args.get("index")
    _check_track(index)
    pan = float(args.get("pan"))
    if not (-1.0 <= pan <= 1.0):
        raise ValueError("pan must be -1..1, got %r" % (pan,))
    before = _track_state(index)
    _mutating("set pan mixer track %s" % index)
    mixer.setTrackPan(index, pan)
    return {"from": before, "to": _deferred_state(index, pan=pan)}


def _deferred_state(index, **applied):
    """Track state after a pan/mute/solo change.

    Verified on FL 24.2.2: setTrackPan/muteTrack/soloTrack take effect, but
    reading the value back inside the same call still returns the OLD one
    (FL applies it on its next tick; a later read shows it).  Volume does
    read back immediately.  So report the requested value for the fields we
    just set and the live readback for everything else."""
    state = _track_state(index)
    state.update(applied)
    return state


# Mute and solo accept undo=false: analyze_live flips solo on and off once per
# track and puts every flag back itself, and one undo entry per flip would
# bury the user's real history.  Everything else keeps the undo point.

def cmd_set_mixer_track_mute(args):
    index = args.get("index")
    _check_track(index)
    want = bool(args.get("muted"))
    before = _track_state(index)
    if args.get("undo", True):
        _mutating("mute mixer track %s" % index)
    mixer.muteTrack(index, 1 if want else 0)      # explicit value, not toggle
    return {"from": before, "to": _deferred_state(index, muted=want)}


def cmd_set_mixer_track_solo(args):
    index = args.get("index")
    _check_track(index)
    want = bool(args.get("solo"))
    mode = args.get("mode")                       # FL solo mode 1..4, None = FL's default
    if mode is not None and mode not in (1, 2, 3, 4):
        raise ValueError("mode must be 1..4, got %r" % (mode,))
    before = _track_state(index)
    if args.get("undo", True):
        _mutating("solo mixer track %s" % index)
    if mode is None:
        mixer.soloTrack(index, 1 if want else 0)
    else:
        mixer.soloTrack(index, 1 if want else 0, mode)
    return {"from": before, "to": _deferred_state(index, solo=want)}

# ---------------------------------------------------------------------------
# Mixer routing (track -> track).  Every insert sends to Master by default;
# a "bus" is just an insert that other inserts send to instead of Master.
# ---------------------------------------------------------------------------

def _track_routes(index):
    """Destinations the track at `index` sends to, with send levels (0..1)."""
    out = []
    for dest in range(mixer.trackCount()):
        if dest == index:
            continue
        try:
            if not mixer.getRouteSendActive(index, dest):
                continue
        except Exception:
            continue
        level = None
        try:
            level = round(mixer.getRouteToLevel(index, dest), 4)
        except Exception:
            pass
        out.append({"to": dest, "level": level})
    return out


def _check_route(source, dest):
    _check_track(source)
    _check_track(dest)
    if source == dest:
        raise ValueError("a track cannot route to itself (%s)" % source)
    if source == 0:
        raise ValueError("Master (track 0) cannot be routed anywhere")


def _set_route_apply(source, dest, enabled, level=None):
    mixer.setRouteTo(source, dest, 1 if enabled else 0)
    if enabled and level is not None:
        mixer.setRouteToLevel(source, dest, float(level))


def cmd_set_mixer_route(args):
    source, dest = args.get("source"), args.get("dest")
    _check_route(source, dest)
    enabled = bool(args.get("enabled", True))
    level = args.get("level")
    if level is not None:
        level = float(level)
        if not (0.0 <= level <= 1.0):
            raise ValueError("level must be 0..1 (0.8 = 0 dB), got %r" % (level,))
    before = _track_routes(source)
    _mutating("route mixer track %s -> %s" % (source, dest))
    _set_route_apply(source, dest, enabled, level)
    mixer.afterRoutingChanged()
    return {"source": source, "dest": dest, "enabled": enabled, "level": level,
            "routes_before": before, "routes_after": _track_routes(source)}


def cmd_create_bus(args):
    """Send several tracks into one bus track under a single undo point.

    Each source is routed to the bus and (unless keep_master) its default
    send to Master is removed, so the bus is the only path to Master."""
    bus = args.get("bus")
    sources = args.get("sources")
    if not isinstance(sources, list) or not sources:
        raise ValueError("sources must be a non-empty list of track indices")
    _check_track(bus)
    if bus == 0:
        raise ValueError("Master (track 0) cannot be a bus")
    for s in sources:
        _check_route(s, bus)
    if len(set(sources)) != len(sources):
        raise ValueError("sources contains duplicates")
    name = args.get("name")
    color = args.get("color")
    if color is not None:
        hex_to_color(color)                   # validate before the undo point
    keep_master = bool(args.get("keep_master", False))

    _mutating("create bus on mixer track %s" % bus)
    for s in sources:
        _set_route_apply(s, bus, True)
        if not keep_master:
            _set_route_apply(s, 0, False)
    mixer.afterRoutingChanged()
    result = {"bus": bus, "sources": sources, "keep_master": keep_master}
    if name:
        result["renamed"] = _rename_track_apply(bus, name)
    if color is not None:
        result["colored"] = _set_track_color_apply(bus, color)
    result["routes"] = {str(s): _track_routes(s) for s in sources}
    result["bus_routes"] = _track_routes(bus)
    return result


# ---------------------------------------------------------------------------
# Plugin parameters.  A target is a mixer slot (track + slot) or a channel-rack
# plugin (channel).  FL hands us every value normalised to 0..1; the plugin's
# own text for it ("200 Hz", "-3.0 dB") comes back as value_string so the
# caller can map between the two.
# ---------------------------------------------------------------------------

def _plugin_target(args):
    """-> (index, slot, is_channel), after checking a plugin is actually there."""
    channel, track, slot = args.get("channel"), args.get("track"), args.get("slot")
    if channel is not None:
        if track is not None or slot is not None:
            raise ValueError("give either channel, or track + slot, not both")
        _check_channel(channel)
        if not _ch(plugins.isValid, channel, -1):
            raise ValueError("channel %s has no plugin (samples are not plugins)" % channel)
        return channel, -1, True
    _check_track(track)
    if not isinstance(slot, int) or slot < 0 or slot >= FX_SLOTS:
        raise ValueError("slot must be 0..%d, got %r" % (FX_SLOTS - 1, slot))
    if not plugins.isValid(track, slot):
        raise ValueError("mixer track %s slot %s is empty" % (track, slot))
    return track, slot, False


def _gflag(is_channel):
    """Extra positional useGlobalIndex argument for plugins.* calls.
    Only channel-rack targets need it (mixer slots are never grouped)."""
    return (True,) if (is_channel and use_global_index()) else ()


def _param_text(p, idx, slot, is_channel):
    """The plugin's display string for a parameter, or None if it has none."""
    g = _gflag(is_channel)
    try:
        s = plugins.getParamValueString(p, idx, slot, midi.PIM_None, *g)
        if s:
            return s
    except Exception:
        pass
    try:
        s = plugins.getName(idx, slot, midi.FPN_ParamValue, p, *g)
        return s or None
    except Exception:
        return None


def _param_count(idx, slot, is_channel):
    return plugins.getParamCount(idx, slot, *_gflag(is_channel))


def _param_name(p, idx, slot, is_channel):
    return plugins.getParamName(p, idx, slot, *_gflag(is_channel))


def _param_value(p, idx, slot, is_channel):
    return plugins.getParamValue(p, idx, slot, *_gflag(is_channel))


def _target_desc(idx, slot, is_channel):
    d = {"channel": idx} if is_channel else {"track": idx, "slot": slot}
    d["plugin"] = plugins.getPluginName(idx, slot, False, *_gflag(is_channel))
    return d


def cmd_list_plugin_params(args):
    idx, slot, is_ch = _plugin_target(args)
    needle = (args.get("filter") or "").lower()
    limit = int(args.get("max_params") or 200)
    count = _param_count(idx, slot, is_ch)
    params = []
    truncated = False
    for p in range(count):
        name = _param_name(p, idx, slot, is_ch)
        if not name:                        # VSTs report 4240 slots, most unused
            continue
        if needle and needle not in name.lower():
            continue
        if len(params) >= limit:
            truncated = True
            break
        params.append({
            "index": p,
            "name": name,
            "value": round(_param_value(p, idx, slot, is_ch), 4),
            "value_string": _param_text(p, idx, slot, is_ch),
        })
    out = _target_desc(idx, slot, is_ch)
    out.update({"param_count": count, "params": params, "truncated": truncated})
    return out


def _resolve_param(param, idx, slot, is_ch):
    """param may be an index or a name (exact, else unique substring, case-insensitive)."""
    count = _param_count(idx, slot, is_ch)
    if isinstance(param, int):
        if param < 0 or param >= count:
            raise ValueError("param index %r out of range 0..%d" % (param, count - 1))
        return param
    needle = str(param).strip().lower()
    if not needle:
        raise ValueError("param must be an index or a name")
    names = [(p, _param_name(p, idx, slot, is_ch)) for p in range(count)]
    exact = [p for p, n in names if n and n.lower() == needle]
    if len(exact) == 1:
        return exact[0]
    partial = [p for p, n in names if n and needle in n.lower()]
    if len(partial) == 1:
        return partial[0]
    if not partial:
        raise ValueError("no parameter named %r" % (param,))
    raise ValueError("parameter name %r is ambiguous: %s" % (
        param, ", ".join("%d=%s" % (p, n) for p, n in names if p in partial[:8])))


def cmd_set_plugin_param(args):
    idx, slot, is_ch = _plugin_target(args)
    p = _resolve_param(args.get("param"), idx, slot, is_ch)
    try:
        value = float(args.get("value"))
    except (TypeError, ValueError):
        raise ValueError("value must be a number 0..1, got %r" % (args.get("value"),))
    if not (0.0 <= value <= 1.0):
        raise ValueError("value must be 0..1, got %r" % (value,))
    name = _param_name(p, idx, slot, is_ch)
    before = {"value": round(_param_value(p, idx, slot, is_ch), 4),
              "value_string": _param_text(p, idx, slot, is_ch)}
    _mutating("set %s on %s" % (name, _target_desc(idx, slot, is_ch)["plugin"]))
    plugins.setParamValue(value, p, idx, slot, midi.PIM_None, *_gflag(is_ch))
    after = {"value": round(_param_value(p, idx, slot, is_ch), 4),
             "value_string": _param_text(p, idx, slot, is_ch)}
    out = _target_desc(idx, slot, is_ch)
    # Verified on FL 24.2.2: the plugin's text updates at once but
    # getParamValue can still return the old number inside the same call.
    out.update({"param": p, "name": name, "requested": value, "from": before, "to": after,
                "readback_stale": abs(after["value"] - value) > 1e-3})
    return out

# ---------------------------------------------------------------------------
# Metering.  One snapshot per call; the server polls this while FL plays.
# ---------------------------------------------------------------------------

def _track_peak(t):
    try:
        return float(mixer.getTrackPeaks(t, 2))          # max of L/R
    except Exception:
        return max(float(mixer.getTrackPeaks(t, 0)), float(mixer.getTrackPeaks(t, 1)))


def cmd_track_peaks(args):
    tracks = args.get("tracks")
    if tracks is None:
        tracks = list(range(mixer.trackCount()))
    for t in tracks:
        _check_track(t)
    return {
        "playing": bool(transport.isPlaying()),
        "peaks": [{"index": t, "name": mixer.getTrackName(t), "peak": round(_track_peak(t), 4)}
                  for t in tracks],
    }


# ---------------------------------------------------------------------------
# Playlist tracks (1-indexed in FL's API).  Only the current arrangement is
# reachable; the API cannot switch arrangements.
# ---------------------------------------------------------------------------

def _check_playlist_track(index):
    n = playlist.trackCount()
    if not isinstance(index, int) or index < 1 or index > n:
        raise ValueError("playlist track %r out of range 1..%d" % (index, n))


def _playlist_track_state(i):
    return {"index": i, "name": playlist.getTrackName(i), "color": color_to_hex(playlist.getTrackColor(i)),
            "muted": bool(playlist.isTrackMuted(i)), "solo": bool(playlist.isTrackSolo(i))}


def cmd_list_playlist_tracks(args):
    n = playlist.trackCount()
    limit = int(args.get("limit") or 50)
    include_default = bool(args.get("include_default", False))
    out = []
    for i in range(1, n + 1):
        st = _playlist_track_state(i)
        if not include_default and st["name"] == "Track %d" % i and i > limit:
            continue
        out.append(st)
        if len(out) >= limit and not include_default:
            break
    return {"tracks": out, "total_tracks": n}


def _rename_playlist_track_apply(index, name):
    before = playlist.getTrackName(index)
    playlist.setTrackName(index, str(name))
    return {"index": index, "from": before, "to": playlist.getTrackName(index)}


def _set_playlist_track_color_apply(index, color):
    before = playlist.getTrackColor(index)
    playlist.setTrackColor(index, hex_to_color(color))
    return {"index": index, "from": color_to_hex(before), "to": color_to_hex(playlist.getTrackColor(index))}


def cmd_set_playlist_track(args):
    index = args.get("index")
    _check_playlist_track(index)
    name, color = args.get("name"), args.get("color")
    if name is None and color is None:
        raise ValueError("give a name and/or a color")
    if color is not None:
        hex_to_color(color)
    _mutating("set playlist track %s" % index)
    out = {"index": index}
    if name is not None:
        out["renamed"] = _rename_playlist_track_apply(index, name)
    if color is not None:
        out["colored"] = _set_playlist_track_color_apply(index, color)
    return out


# ---------------------------------------------------------------------------
# Arrangement time markers.  FL has no API to read a marker's position or to
# delete one, so this is append-only: it adds markers at bar positions.
# ---------------------------------------------------------------------------

def _bar_to_ticks(bar, beats_per_bar=4):
    return int(round((float(bar) - 1.0) * beats_per_bar * general.getRecPPQ()))


def cmd_add_markers(args):
    markers = args.get("markers")
    if not isinstance(markers, list) or not markers:
        raise ValueError("markers must be a non-empty list of {bar, name}")
    beats = int(args.get("beats_per_bar") or 4)
    clean = []
    for n, m in enumerate(markers):
        try:
            bar = float(m["bar"]); name = str(m["name"]).strip()
        except (KeyError, TypeError, ValueError):
            raise ValueError("marker %d: need bar (number) and name" % n)
        if bar < 1:
            raise ValueError("marker %d: bar must be >= 1, got %r" % (n, bar))
        if not name:
            raise ValueError("marker %d: empty name" % n)
        clean.append((bar, name))
    _mutating("add %d time markers" % len(clean))
    out = []
    for bar, name in sorted(clean):
        t = _bar_to_ticks(bar, beats)
        arrangement.addAutoTimeMarker(t, name)
        out.append({"bar": bar, "ticks": t, "name": name})
    return {"added": out, "ppq": general.getRecPPQ(), "beats_per_bar": beats}


# Operations cmd_batch may perform.  Still a whitelist: the op name selects
# one of the helpers above; nothing else is reachable.
BATCH_OPS = {
    "rename_playlist_track":    lambda op: _rename_playlist_track_apply(op["index"], op["name"]),
    "set_playlist_track_color": lambda op: _set_playlist_track_color_apply(op["index"], op["color"]),
    "rename_channel":         lambda op: _rename_channel_apply(op["index"], op["name"]),
    "set_channel_color":      lambda op: _set_channel_color_apply(op["index"], op["color"]),
    "route_channel_to_mixer": lambda op: _route_channel_apply(op["index"], op["track"]),
    "rename_mixer_track":     lambda op: _rename_track_apply(op["index"], op["name"]),
    "set_mixer_track_color":  lambda op: _set_track_color_apply(op["index"], op["color"]),
}
MIXER_OPS = ("rename_mixer_track", "set_mixer_track_color")
PLAYLIST_OPS = ("rename_playlist_track", "set_playlist_track_color")


def cmd_batch(args):
    """Apply a list of whitelisted ops under ONE undo point.

    Used by apply_organization_template so a whole template is a single
    Ctrl+Z.  Every op is validated first; if any is malformed nothing runs.
    """
    ops = args.get("ops")
    if not isinstance(ops, list) or not ops:
        raise ValueError("ops must be a non-empty list")
    for n, op in enumerate(ops):
        kind = op.get("op") if isinstance(op, dict) else None
        if kind not in BATCH_OPS:
            raise ValueError("op %d: unknown or missing op %r" % (n, kind))
        if kind in MIXER_OPS:
            _check_track(op.get("index"))
        elif kind in PLAYLIST_OPS:
            _check_playlist_track(op.get("index"))
        else:
            _check_channel(op.get("index"))
        if "track" in op:
            _check_track(op["track"])
        if "color" in op:
            hex_to_color(op["color"])

    _mutating(args.get("label") or "batch of %d changes" % len(ops))
    results = []
    for op in ops:
        try:
            r = BATCH_OPS[op["op"]](op)
            r["op"] = op["op"]
            r["ok"] = True
        except Exception as e:
            r = {"op": op.get("op"), "ok": False, "error": str(e), "index": op.get("index")}
        results.append(r)
    return {"applied": sum(1 for r in results if r["ok"]), "results": results}


COMMANDS = {
    "ping": cmd_ping,
    "project_info": cmd_project_info,
    "list_channels": cmd_list_channels,
    "list_mixer_tracks": cmd_list_mixer_tracks,
    "rename_channel": cmd_rename_channel,
    "set_channel_color": cmd_set_channel_color,
    "route_channel_to_mixer": cmd_route_channel_to_mixer,
    "rename_mixer_track": cmd_rename_mixer_track,
    "set_mixer_track_color": cmd_set_mixer_track_color,
    "set_tempo": cmd_set_tempo,
    "set_mixer_track_volume": cmd_set_mixer_track_volume,
    "set_mixer_track_pan": cmd_set_mixer_track_pan,
    "set_mixer_track_mute": cmd_set_mixer_track_mute,
    "set_mixer_track_solo": cmd_set_mixer_track_solo,
    "set_mixer_route": cmd_set_mixer_route,
    "create_bus": cmd_create_bus,
    "list_plugin_params": cmd_list_plugin_params,
    "set_plugin_param": cmd_set_plugin_param,
    "track_peaks": cmd_track_peaks,
    "list_playlist_tracks": cmd_list_playlist_tracks,
    "add_markers": cmd_add_markers,
    "set_playlist_track": cmd_set_playlist_track,
    "batch": cmd_batch,
}

# ---------------------------------------------------------------------------
# Transport: framing, reassembly, sending
# ---------------------------------------------------------------------------

_inbox = {}        # frame id -> {"total": int, "parts": {idx: bytes}}
_next_frame_id = 0


def _u14(hi, lo):
    return ((hi & 0x7F) << 7) | (lo & 0x7F)


def _split14(v):
    return ((v >> 7) & 0x7F, v & 0x7F)


def send_json(obj):
    """Encode obj as ASCII JSON, chunk it, and emit one SysEx per chunk."""
    global _next_frame_id
    payload = json.dumps(obj, ensure_ascii=True, separators=(",", ":")).encode("ascii")
    fid = _next_frame_id
    _next_frame_id = (_next_frame_id + 1) & 0x3FFF
    chunks = [payload[i:i + CHUNK_PAYLOAD] for i in range(0, len(payload), CHUNK_PAYLOAD)] or [b""]
    total = len(chunks)
    for idx, chunk in enumerate(chunks):
        head = bytes((0xF0, MANUFACTURER_ID) + SUB_ID + _split14(fid) + _split14(idx) + _split14(total))
        device.midiOutSysex(head + chunk + b"\xF7")


def _handle_message(payload):
    try:
        msg = json.loads(payload.decode("ascii"))
    except Exception as e:
        send_json({"id": None, "ok": False, "error": "bad JSON: %s" % e})
        return
    rid = msg.get("id")
    cmd = msg.get("cmd")
    args = msg.get("args") or {}
    fn = COMMANDS.get(cmd)
    if fn is None:
        send_json({"id": rid, "ok": False, "error": "unknown command %r" % (cmd,)})
        return
    try:
        result = fn(args)
        send_json({"id": rid, "ok": True, "result": result})
    except Exception as e:
        send_json({"id": rid, "ok": False, "error": "%s: %s" % (type(e).__name__, e)})


def _ingest_frame(data):
    """Validate one SysEx frame, stash its chunk, dispatch when complete.

    Returns True if the frame was ours (so the caller marks it handled)."""
    if len(data) < HEADER_LEN + 1 or data[0] != 0xF0 or data[-1] != 0xF7:
        return False
    if data[1] != MANUFACTURER_ID or (data[2], data[3]) != SUB_ID:
        return False                              # not ours; let FL have it
    fid = _u14(data[4], data[5])
    idx = _u14(data[6], data[7])
    total = _u14(data[8], data[9])
    chunk = bytes(data[HEADER_LEN:-1])
    entry = _inbox.setdefault(fid, {"total": total, "parts": {}})
    entry["parts"][idx] = chunk
    if len(entry["parts"]) == entry["total"]:
        del _inbox[fid]
        payload = b"".join(entry["parts"][i] for i in range(entry["total"]))
        _handle_message(payload)
    return True

# ---------------------------------------------------------------------------
# FL Studio hooks
# ---------------------------------------------------------------------------

def OnInit():
    print("FLMCP controller loaded. API v%s, input port %s, global index: %s"
          % (general.getVersion(), device.getPortNumber(), use_global_index()))
    if not device.isAssigned():
        print("FLMCP: WARNING - no output device assigned. Set the same Port "
              "number on FLMCP_In and FLMCP_Out in MIDI settings.")


def OnDeInit():
    print("FLMCP controller unloaded.")


def OnSysEx(event):
    try:
        if _ingest_frame(event.sysex):
            event.handled = True
    except Exception as e:
        # Never let an exception escape into FL; report it instead.
        print("FLMCP: error in OnSysEx: %r" % (e,))
        try:
            send_json({"id": None, "ok": False, "error": "controller crash: %r" % (e,)})
        except Exception:
            pass
        event.handled = True
