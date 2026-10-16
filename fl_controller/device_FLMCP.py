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

import channels
import device
import general
import midi
import mixer
import patterns
import plugins
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
        if not include_empty and t != 0 and not effects and n_routed == 0:
            continue
        out.append({
            "index": t,
            "name": mixer.getTrackName(t),
            "color": color_to_hex(mixer.getTrackColor(t)),
            "volume": round(mixer.getTrackVolume(t), 4),
            "volume_db": round(mixer.getTrackVolume(t, 1), 2),
            "pan": round(mixer.getTrackPan(t), 4),
            "routed_channels": n_routed,
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


def _track_state(index):
    return {
        "index": index,
        "volume": round(mixer.getTrackVolume(index), 4),
        "volume_db": round(mixer.getTrackVolume(index, 1), 2),
        "pan": round(mixer.getTrackPan(index), 4),
        "muted": bool(mixer.isTrackMuted(index)),
        "solo": bool(mixer.isTrackSolo(index)),
    }


def _volume_for_db(index, db):
    """Find the 0..1 fader value whose dB readout is closest to db.

    FL's fader law is not a plain formula we can trust, so we bisect against
    mixer.getTrackVolume(index, 1) (the dB readout).  The fader is monotonic,
    so 24 steps is far more precision than FL stores.
    """
    lo, hi = 0.0, 1.0
    for _ in range(24):
        mid = (lo + hi) / 2.0
        mixer.setTrackVolume(index, mid)
        if mixer.getTrackVolume(index, 1) < db:
            lo = mid
        else:
            hi = mid
    return hi


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
    before = _track_state(index)
    _mutating("set volume mixer track %s" % index)
    if has_vol:
        mixer.setTrackVolume(index, vol)
    else:
        mixer.setTrackVolume(index, 1.0)
        top = mixer.getTrackVolume(index, 1)
        if db > top:
            mixer.setTrackVolume(index, before["volume"])
            raise ValueError("volume_db %.2f above FL's fader maximum %.2f" % (db, top))
        mixer.setTrackVolume(index, _volume_for_db(index, db))
    return {"from": before, "to": _track_state(index)}


def cmd_set_mixer_track_pan(args):
    index = args.get("index")
    _check_track(index)
    pan = float(args.get("pan"))
    if not (-1.0 <= pan <= 1.0):
        raise ValueError("pan must be -1..1, got %r" % (pan,))
    before = _track_state(index)
    _mutating("set pan mixer track %s" % index)
    mixer.setTrackPan(index, pan)
    return {"from": before, "to": _track_state(index)}


def cmd_set_mixer_track_mute(args):
    index = args.get("index")
    _check_track(index)
    before = _track_state(index)
    _mutating("mute mixer track %s" % index)
    want = bool(args.get("muted"))
    if before["muted"] != want:          # muteTrack toggles when no value is given
        mixer.muteTrack(index)
    return {"from": before, "to": _track_state(index)}


def cmd_set_mixer_track_solo(args):
    index = args.get("index")
    _check_track(index)
    before = _track_state(index)
    _mutating("solo mixer track %s" % index)
    want = bool(args.get("solo"))
    if before["solo"] != want:
        mixer.soloTrack(index)
    return {"from": before, "to": _track_state(index)}


# Operations cmd_batch may perform.  Still a whitelist: the op name selects
# one of the helpers above; nothing else is reachable.
BATCH_OPS = {
    "rename_channel":         lambda op: _rename_channel_apply(op["index"], op["name"]),
    "set_channel_color":      lambda op: _set_channel_color_apply(op["index"], op["color"]),
    "route_channel_to_mixer": lambda op: _route_channel_apply(op["index"], op["track"]),
    "rename_mixer_track":     lambda op: _rename_track_apply(op["index"], op["name"]),
    "set_mixer_track_color":  lambda op: _set_track_color_apply(op["index"], op["color"]),
}
MIXER_OPS = ("rename_mixer_track", "set_mixer_track_color")


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
