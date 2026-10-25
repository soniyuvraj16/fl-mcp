# fl-mcp

An MCP server that lets Claude control **FL Studio 2024** (tested target:
v24.2.2, Windows 64-bit, All Plugins Edition). Phase 1 covers connection and
project organisation: reading channels and mixer tracks, renaming, colouring,
routing, tempo, and applying an organisation template.

```
┌──────────────┐  stdio (MCP)  ┌─────────────────┐  SysEx over loopMIDI  ┌───────────────────────────┐
│ Claude Code  │◄────────────►│ fl-mcp server   │──── "FLMCP_In" ─────►│ FL Studio                 │
│              │               │ (uv, FastMCP,   │                       │  device_FLMCP.py          │
│              │               │  mido, rtmidi)  │◄─── "FLMCP_Out" ─────│  (embedded Python script) │
└──────────────┘               └─────────────────┘                       └───────────────────────────┘
```

## Why this shape

FL Studio's embedded Python has no sockets, threads or pip. The only way in or
out of a MIDI controller script is MIDI itself. SysEx is the one MIDI message
that carries an arbitrary byte payload, so the server and the script exchange
JSON inside SysEx messages over two virtual MIDI cables created by loopMIDI.

Every FL API call in `fl_controller/device_FLMCP.py` was checked against
`fl-studio-api-stubs` 37.0.1 (the official Image-Line stubs). Two things you
might expect do **not** exist in the API, and here is what we do instead:

| Wanted                  | Reality (stubs v37)                                   | What fl-mcp does                                                                      |
| ----------------------- | ----------------------------------------------------- | ------------------------------------------------------------------------------------- |
| `transport.setTempo()`  | No such function. Only `mixer.getCurrentTempo()` reads it. | Fires `general.processRECEvent(midi.REC_Tempo, bpm*1000, REC_Control \| REC_UpdateControl)`, the route the stubs document for tempo. Validates 10–522 BPM first. |
| `general.getProjectTitle()` | No such function.                                  | Parses `ui.getProgTitle()` (the window title, e.g. `My Song.flp - FL Studio 2024`). Both parsed and raw values are returned. |

Two more facts worth knowing:

* `channels.*` functions take a `useGlobalIndex` flag since API v33. The script
  probes for it at load and falls back to group-relative indexing on older
  builds; `fl_ping` reports which mode is active.
* FL colour ints are documented as `0x--BBGGRR` (red in the low byte) while the
  stubs' `utils.ColorToRGB` assumes `RRGGBB`. The script has its own converter
  with a one-line switch (`COLOR_ORDER`), and the test checklist has a 30-second
  step to confirm the order on your machine.

## Repo layout

```
fl-mcp/
├── fl_controller/device_FLMCP.py   # runs INSIDE FL Studio (copy via install script)
├── src/fl_mcp/
│   ├── server.py                   # FastMCP tools
│   ├── midi_bridge.py              # request/response over loopMIDI (mido + python-rtmidi)
│   ├── protocol.py                 # SysEx framing + chunk reassembly (mirrors the controller)
│   └── templates.py                # organisation-template planner (pure Python, dry-run safe)
├── scripts/install_controller.ps1  # copies the controller into FL's Hardware folder
├── templates/example_template.json # Drums / Bass / Vocals / Synths / FX / Returns
├── tests/                          # fake FL Studio + end-to-end round-trip tests
├── docs/TEST_CHECKLIST.md          # manual verification against the real FL
└── pyproject.toml                  # uv project; `uv run fl-mcp` starts the server
```

## Setup (on the machine that runs FL Studio)

### 1. Install the tools

* [loopMIDI](https://www.tobias-erichsen.de/software/loopmidi.html) (free virtual MIDI cable)
* [uv](https://docs.astral.sh/uv/) (`winget install astral-sh.uv`)
* Claude Code CLI (`npm install -g @anthropic-ai/claude-code` or the desktop app)

### 2. Create the MIDI ports in loopMIDI

Open loopMIDI. In the name box at the bottom type `FLMCP_In`, press **+**.
Then type `FLMCP_Out`, press **+**. Leave loopMIDI running (it minimises to the
tray; tick "Start minimized" and "Autostart" in its settings so the ports exist
before FL starts).

### 3. Install the controller script into FL Studio

```powershell
git clone https://github.com/soniyuvraj16/fl-mcp.git FL_studio
cd FL_studio
uv sync
powershell -ExecutionPolicy Bypass -File scripts\install_controller.ps1
```

This copies `fl_controller/device_FLMCP.py` to
`Documents\Image-Line\FL Studio\Settings\Hardware\FLMCP\device_FLMCP.py`
(backing up any previous copy). If your Documents folder lives somewhere
unusual, pass `-FLSettings "<path to>\FL Studio\Settings"`.

### 4. FL Studio MIDI settings

1. Start (or restart) FL Studio.
2. **Options > MIDI Settings**.
3. **Input** list: click `FLMCP_In`, tick **Enable**, set **Controller type**
   to `FLMCP`, set **Port** to a free number, e.g. `7`.
4. **Output** list: click `FLMCP_Out`, tick **Enable**, set **Port** to the
   **same** number (`7`). FL routes `device.midiOutSysex()` from a script to
   the output device with the matching port number, which is how replies reach
   the server.
5. Do **not** enable `FLMCP_Out` as an input or `FLMCP_In` as an output.
6. **View > Script output**, pick the `FLMCP` tab. You should see
   `FLMCP controller loaded. API vNN, input port 7, global index: True`.

### 5. Register the server with Claude Code

```powershell
claude mcp add fl-mcp --scope user -- uv --directory "C:\path\to\fl-mcp" run fl-mcp
```

Optional environment overrides (`-e KEY=VALUE` before the `--`):

| Variable          | Default     | Meaning                                              |
| ----------------- | ----------- | ---------------------------------------------------- |
| `FL_MCP_IN_PORT`  | `FLMCP_In`  | loopMIDI port the server **sends** to (FL's input)   |
| `FL_MCP_OUT_PORT` | `FLMCP_Out` | loopMIDI port the server **listens** on (FL's output)|
| `FL_MCP_TIMEOUT`  | `5`         | seconds to wait for FL's reply                       |
| `FL_MCP_CHUNK`    | `240`       | payload bytes per SysEx frame sent to FL             |

Then in Claude Code: `/mcp` should list `fl-mcp` as connected, and "call fl_ping"
should return the FL version.

## Tools

| Tool                           | Kind       | What it does                                                                 |
| ------------------------------ | ---------- | ---------------------------------------------------------------------------- |
| `fl_ping`                      | read-only  | API version (`general.getVersion`), full FL version string, indexing mode    |
| `get_project_info`             | read-only  | tempo, title, channel / pattern / mixer-track counts                         |
| `list_channels`                | read-only  | index, name, `#RRGGBB`, type, plugin, target mixer track                     |
| `list_mixer_tracks`            | read-only  | index, name, colour, volume, pan, mute/solo, routes, `fed_by` (bus sources), effect slots; `include_empty` to show all |
| `rename_channel`               | mutating   | `index`, `name`                                                              |
| `set_channel_color`            | mutating   | `index`, `color` (`#RRGGBB`)                                                 |
| `route_channel_to_mixer`       | mutating   | `index`, `track`                                                             |
| `rename_mixer_track`           | mutating   | `index`, `name`                                                              |
| `set_mixer_track_color`        | mutating   | `index`, `color`                                                             |
| `set_tempo`                    | mutating   | `bpm` 10–522, via `REC_Tempo`                                                |
| `set_mixer_track_volume`       | mutating   | `index`, exactly one of `volume` (0–1, 0.8 = 0 dB) or `volume_db` (max +5.59) |
| `set_mixer_track_pan`          | mutating   | `index`, `pan` -1..1                                                         |
| `set_mixer_track_mute`         | mutating   | `index`, `muted`                                                             |
| `set_mixer_track_solo`         | mutating   | `index`, `solo` (FL clears the track's mute when it is soloed)               |
| `set_mixer_route`              | mutating   | `source`, `dest`, `enabled` (default `true`), optional send `level` 0–1      |
| `create_bus`                   | mutating   | `bus`, `sources` list, optional `name`/`color`, `keep_master`; one undo step |
| `list_plugin_params`           | read-only  | `track`+`slot` or `channel`; name, value 0–1 and the plugin's text per param; `filter`, `max_params` |
| `set_plugin_param`             | mutating   | `param` (index or name), `value` 0–1, `track`+`slot` or `channel`           |
| `get_track_levels`             | read-only  | polls `getTrackPeaks` for `duration_s` while FL plays; peak/avg in dBFS (FL's meter value is dB-linear, not amplitude; calibrated on 24.2.2) |
| `analyze_stems`                | read-only  | FFT of exported stem WAVs in `folder`: band energy, peaks, dynamics, clashes (server-side, no FL call) |
| `list_audio_devices`           | read-only  | WASAPI loopback devices `analyze_live` can record from                      |
| `separate_stems`               | read-only  | vocal / instrumental stems of a song file via the UVR models (`audio-separator` CLI, installed separately) |
| `analyze_live`                 | mutating*  | solo each track, record FL's output via loopback, same analysis as stems; captures and subtracts the bleed of channels routed straight to Master (`master_direct`, `above_bleed_db`); `tracks`, `seconds`, `device`. *flips solo/mute and restores them, no undo entries |
| `list_playlist_tracks`         | read-only  | playlist tracks of the current arrangement (1-based): name, colour, mute/solo |
| `set_playlist_track`           | mutating   | `index` (1-based), `name` and/or `color`                                     |
| `setup_playlist_tracks`        | mutating   | `tracks` list of `{index, name, color}`; one undo step                        |
| `add_markers`                  | mutating   | `markers` list of `{bar, name}` time markers in the current arrangement (append-only) |
| `apply_organization_template`  | mutating   | `template` (JSON), `dry_run` (default `true`)                                |

`volume_db` is converted with FL's fader law, fitted on 24.2.2:
`amplitude = (e^(3v) - 1) / (e^(2.4) - 1)`, so `v = 0.8` is 0 dB and `v = 1`
is +5.59 dB. The reply carries `set` (what was written) next to `to` (what FL
read back). On some tracks FL applies the fader on its next tick, so if
`readback_stale` is true trust `set` and re-read with `list_mixer_tracks`.

Every mutating tool creates an undo point named `FLMCP: ...` inside FL
**before** changing anything, so Ctrl+Z reverts it. A template application is
one undo point for the whole batch. Read-only tools never call a setter.

### Stem separation (remixing)

`separate_stems(file=...)` runs the same models as Ultimate Vocal Remover
through the `audio-separator` command line and writes WAV stems next to the
song. It is a heavy install (PyTorch), so it lives outside this project:

```powershell
uv tool install "audio-separator[cpu]"
# NVIDIA GPU, much faster:
uv tool install --force --index https://download.pytorch.org/whl/cu124 --index-strategy unsafe-best-match "audio-separator[gpu]"
```

The server finds the executable on `PATH`, in `~/.local/bin`, or via the
`FL_MCP_SEPARATOR` environment variable. The default model is the BS-Roformer
vocal model, which downloads on first use (about 400 MB).

### Mixing workflow

The scripting API exposes no audio, so "what frequencies are on this track" is
answered by listening from outside FL, two ways:

**Live (no export).** Press Play on a loop and call `analyze_live()`. For each
non-empty insert the bridge solos it (solo mode 3, sources and sends; modes 1
and 4 mute Master and capture silence), records a couple of seconds of FL's
output through WASAPI loopback (`pyaudiowpatch`), unsolos, and finally puts
every solo/mute flag back. Works when FL plays through *FL Studio ASIO* or a
WASAPI/DirectSound device; a hardware ASIO driver or ASIO4ALL bypasses Windows
and loopback hears silence (`list_audio_devices` shows what is capturable).

**Stems (any driver).**
1. In FL: File > Export > WAV, tick **Split mixer tracks**, export a short loop
   where everything plays. One WAV per mixer track lands in the folder.
2. `analyze_stems(folder=...)` reports each stem's energy per band, its top
   spectral peaks, and *clashes* (two stems both leaning on the same band).

Both report, per track: peak/RMS dBFS, energy share per band, spectral
centroid and peaks, and *dynamics* (crest factor, loudness range over time,
transients per second, percussive/sustained/mixed), plus clashes and a
loudness order across tracks.
Then act on it through FL: `get_track_levels` while playing for balance,
   `set_mixer_track_volume` / `set_mixer_track_pan`, `create_bus`, and
   `list_plugin_params` + `set_plugin_param` to move EQ bands on a Fruity
   Parametric EQ 2 you have loaded (plugins cannot be loaded by script).

Plugin values are FL's normalised 0–1; the reply's `value_string` shows the
plugin's own readout, so iterate towards a target like "200 Hz".

Fruity Parametric EQ 2 maps (measured on 24.2.2, so you can set a target
directly instead of iterating): `Band N freq` is `20 Hz * 2^(10 v)`, i.e.
`v = log2(Hz / 20) / 10`; `Band N level` is 36 dB across the range with `0.5`
= 0 dB, i.e. `v = 0.5 + dB / 36`; `Band N type` steps through Disabled, Low
pass, Band pass, High pass, Notch, Low shelf, Peaking, High shelf as
`v = k / 7`; `Band N order` 0.5 is slope "2", 0 is "Steep 8".

Some plugins update their readout lazily: Fruity Delay 3's `value_string`
in a `set_plugin_param` reply can still show the previous value. Re-read it
with `list_plugin_params` a moment later. Its `Time` (tempo-synced) is
`steps / 16`, `Feedback cutoff` is exponential (0.68 is about 5 kHz) and
`Delay time R/L offset` is `-100..100 %` across `0..1`.

### Template format

```json
{
  "name_mixer_tracks": true,
  "color_mixer_tracks": true,
  "groups": [
    {"name": "Drums", "prefix": "DR ", "color": "#E74C3C", "mixer_range": [1, 10],
     "match": ["kick", "snare", "hat"], "channels": [4]}
  ]
}
```

* Group order is priority: a channel belongs to the first group that claims it
  (explicit `channels` index or a case-insensitive substring match on channel
  name or plugin name).
* Channels in a group get `prefix + name`, the group colour, and sequential
  mixer tracks starting at `mixer_range[0]`. Extra channels share the last track
  and a warning is reported.
* Used mixer tracks are named after their channel (or the group, if shared) and
  coloured with the group colour.
* Known prefixes are stripped before re-applying, so a second run plans nothing.
* The default `dry_run=true` reads FL and returns `planned_changes` without
  touching anything. Review, then call again with `dry_run=false`.

## How the wire protocol works

Each message is JSON (`{"id": 3, "cmd": "rename_channel", "args": {...}}`)
encoded with `ensure_ascii=True`, which guarantees every byte is printable
ASCII (0x20–0x7E), i.e. 7-bit and therefore legal inside SysEx. The bytes are
split into chunks and each chunk becomes one SysEx frame:

```
F0 7D 46 4C  <fid hi> <fid lo>  <idx hi> <idx lo>  <tot hi> <tot lo>  <payload…>  F7
   │  └─'F','L' sub-id            14-bit frame id, chunk index, chunk count
   └─ 0x7D: manufacturer id reserved for non-commercial use
```

Both ends keep an inbox keyed by frame id and dispatch when all chunks have
arrived. Replies echo the request `id` (`{"id": 3, "ok": true, "result": …}` or
`{"id": 3, "ok": false, "error": "…"}`). The server waits up to
`FL_MCP_TIMEOUT` seconds per request and raises a descriptive error otherwise.
On the FL side everything happens in `OnSysEx`; `OnIdle` is not used.

## Running the automated tests

```powershell
uv run pytest
```

`tests/fake_fl.py` fakes the FL modules with an in-memory project and loads the
real controller file, so the tests exercise the actual framing, chunking,
undo-point placement, error handling and template planner. Only loopMIDI and
FL's real behaviour remain for the manual checklist in `docs/TEST_CHECKLIST.md`.

## Troubleshooting

* **`fl_ping` says a port was not found**: loopMIDI isn't running or the port
  names differ. The error lists what the server can see; set
  `FL_MCP_IN_PORT`/`FL_MCP_OUT_PORT` if you named them differently.
* **`fl_ping` times out**: controller type not set to `FLMCP`, the input and
  output Port numbers differ, or the script errored (check View > Script
  output). Toggling **Enable** off and on reloads the script.
* **Colours come out swapped**: flip `COLOR_ORDER` in `device_FLMCP.py` (see
  checklist step 6).
* **Ctrl+Z doesn't revert**: change `UNDO_FLAGS` in `device_FLMCP.py` to
  `midi.UF_Knob` (checklist step 5).
* **Channel indices look wrong**: `fl_ping` shows `global_index: false`, so the
  API is older than v33; keep the channel rack's group filter on "All".

## Developing on one machine, using on another

The server must run on the FL Studio machine because loopMIDI is local. You do
not need VS Code there: copy or clone the repo, run `uv sync`, install the
controller, register with `claude mcp add`, and drive it from the plain
`claude` CLI in a terminal (a few hundred MB of RAM, far lighter than VS Code).
