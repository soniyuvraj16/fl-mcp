# fl-mcp manual test checklist (Phase 1)

Run this on the machine that has FL Studio, loopMIDI, uv and Claude Code
installed. Do it in order; each step depends on the previous one. Use a
throwaway project with a handful of named channels (e.g. "Kick", "Snare",
"Serum", "Reese", "Vox") so the template step has something to match.

Automated tests (`uv run pytest`) already cover framing, chunking, undo-point
placement, error paths and the template planner against a fake FL. This list
covers only what needs the real FL Studio.

## 0. Setup sanity

- [ ] loopMIDI is running and shows two ports: `FLMCP_In` and `FLMCP_Out`.
- [ ] `scripts\install_controller.ps1` reported the install path, and the file
      `Documents\Image-Line\FL Studio\Settings\Hardware\FLMCP\device_FLMCP.py`
      exists with `# name=FLMCP` on line 1.
- [ ] `uv run pytest` passes in the repo.

## 1. Enable the script in FL Studio

- [ ] Start FL Studio (restart it if it was already open when you installed).
- [ ] Options > MIDI Settings. In the **Input** list select `FLMCP_In`, tick
      **Enable**, set **Controller type** to `FLMCP`, and set **Port** to a
      number nothing else uses (e.g. 7).
- [ ] In the **Output** list select `FLMCP_Out`, enable it, and set the same
      **Port** number (7). This pairing is what makes `device.midiOutSysex`
      reach `FLMCP_Out`.
- [ ] Make sure `FLMCP_Out` is **not** also enabled as an input and `FLMCP_In`
      is **not** enabled as an output (that would create a feedback loop).

## 2. Script output

- [ ] View > Script output. Pick the `FLMCP` tab.
- [ ] Expect: `FLMCP controller loaded. API vNN, input port 7, global index: True`.
      Note the API number; if `global index: False` the API is older than v33
      and indices are group-relative (keep the channel rack on the "All" group).
- [ ] If you see a traceback, read it. The most common cause is a typo from
      editing the file; reinstall with the script and press **Reload script**
      in the Script output window.
- [ ] There must be **no** `WARNING - no output device assigned` line. If there
      is, the Port numbers don't match.

## 3. Connection: `fl_ping`

- [ ] In Claude Code: "Call fl_ping".
- [ ] Expect `api_version`, `fl_version` containing `24.2.2` and `All Plugins`,
      `global_index`, `port: 7`.
- [ ] If it times out: the error text lists the exact things to check. The
      usual culprits are controller type not set, port numbers not paired, or
      FL not focused yet after enabling (toggle Enable off/on once).

## 4. Read-only tools (project must NOT change)

- [ ] `get_project_info`: tempo matches the transport display, title matches
      the window title, counts look right. Pattern count includes empty
      patterns FL has created.
- [ ] `list_channels`: every channel listed, names correct, `mixer_track`
      matches the FX number shown on each channel button.
- [ ] `list_mixer_tracks`: only Master plus tracks with effects or routed
      channels. Then with `include_empty=true`: all tracks. Volume 0.8 for
      untouched tracks, pan 0.
- [ ] Press Ctrl+Z once. Nothing should happen (no undo entries were created).
      Also check Edit > Undo history is unchanged.

## 5. Single mutations and undo

For each: run the tool, confirm the change on screen, check Edit > Undo history
shows an entry named `FLMCP: ...`, press Ctrl+Z, confirm the change reverted.

- [ ] `rename_channel(index=0, name="TEST kick")`
- [ ] `route_channel_to_mixer(index=0, track=12)` (channel button shows 12)
- [ ] `rename_mixer_track(index=12, name="TEST track")`
- [ ] `set_tempo(bpm=128.5)` (transport shows 128.500)
- [ ] `set_mixer_track_volume(index=12, volume_db=-6)` moves the fader to
      -6 dB (`set.volume` 0.5986). If `readback_stale` is true in the reply,
      `list_mixer_tracks` a moment later must still show -6 dB.
- [ ] `set_mixer_track_pan(index=12, pan=-0.5)` pans halfway left.
- [ ] `set_mixer_track_mute(index=12, muted=true)` then `muted=false`; calling
      `muted=true` twice leaves it muted.
- [ ] `set_mixer_track_solo(index=12, solo=true)` then `solo=false`. Note FL
      un-mutes a track when it is soloed.
- [ ] `set_mixer_track_volume(index=12, volume_db=20)`: "above FL's fader
      maximum" error and the fader is left where it was.
- [ ] `set_mixer_route(source=12, dest=13, level=0.5)`: in the mixer, select
      track 12 and the send arrow under track 13 lights up at about -6 dB;
      `routes_after` lists both 0 and 13.
- [ ] `set_mixer_route(source=12, dest=0, enabled=false)`: track 12 no longer
      feeds Master (its send arrow under Master goes off).
- [ ] `create_bus(bus=14, sources=[12, 13], name="TEST bus", color="#00FF00")`:
      tracks 12 and 13 now send only to 14, 14 still sends to Master, 14 is
      renamed and green, and the undo history has ONE new entry.
      `create_bus(..., keep_master=true)` leaves the Master sends in place.
- [ ] Load Fruity Parametric EQ 2 into slot 0 of track 12, then
      `list_plugin_params(track=12, slot=0)`: band names/values match the
      plugin window; `value_string` shows Hz / dB text (note which, if any,
      come back as null; `getParamValueString` is not supported by every plugin).
- [ ] `set_plugin_param(track=12, slot=0, param="<a band Freq name>", value=0.5)`:
      the knob moves on screen, the reply's `to.value_string` matches, Ctrl+Z
      reverts it.
- [ ] `list_plugin_params(channel=<index of a FLEX/Sytrus channel>)` works with
      the channel form.
- [ ] Press Play, then `get_track_levels(duration_s=2)`: `playing=true`, the
      tracks with sound appear loudest-first with sensible dBFS; with transport
      stopped every track is omitted (silent).
- [ ] Export stems (File > Export > WAV, Split mixer tracks) to a folder and run
      `analyze_stems(folder=...)`: one entry per WAV, kick/bass stems dominant in
      sub/bass, hats in presence/air; `errors` is empty.

If Ctrl+Z does **not** revert a change but the history entry exists, open
`device_FLMCP.py`, change `UNDO_FLAGS = midi.UF_None` to
`UNDO_FLAGS = midi.UF_Knob`, reload the script, and repeat this section.

## 6. Colour byte order (do this once)

- [ ] `set_channel_color(index=0, color="#FF0000")`. The channel must turn
      **pure red**. If it turns **blue**, FL on your build uses the other byte
      order: set `COLOR_ORDER = "RGB"` in `device_FLMCP.py`, reload, repeat.
- [ ] `list_channels` now reports `#FF0000` for channel 0.
- [ ] `set_mixer_track_color(index=12, color="#00FF00")` turns the track green.
- [ ] Ctrl+Z twice reverts both.

## 7. Organisation template

- [ ] `apply_organization_template(template=<contents of templates/example_template.json>)`
      with the default `dry_run=true`. Read `planned_changes`,
      `channels_per_group`, `unmatched_channels` and `warnings`. Nothing in FL
      should have changed. Ctrl+Z does nothing.
- [ ] Same call with `dry_run=false`. Channels are renamed with prefixes,
      coloured, and routed into the group ranges; used mixer tracks are named
      and coloured.
- [ ] Edit > Undo history shows **one** entry `FLMCP: apply organisation
      template`. A single Ctrl+Z reverts the whole template.
- [ ] Redo (Ctrl+Shift+Z or Ctrl+Y), then run the dry run again: `planned_changes`
      must be empty (prefixes are not stacked on a second run).

## 8. Failure modes

- [ ] Close loopMIDI, call `fl_ping`: a clear "port not found" error listing
      available ports. Reopen loopMIDI, call again: works without restarting
      the MCP server.
- [ ] Disable the FLMCP device in MIDI Settings, call `fl_ping`: timeout error
      with the checklist text. Re-enable: works again.
- [ ] `rename_channel(index=999, name="x")`: "out of range" error, no undo entry.
- [ ] `set_tempo(bpm=1000)`: "outside FL's range" error, no undo entry.
