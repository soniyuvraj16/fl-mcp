# Prompt: set up fl-mcp on the FL Studio laptop

Paste everything below the line into Claude Code on the laptop (open it in any
folder; it will create the right one). It is written so Claude does every
command itself and only stops for the two GUI-only steps (loopMIDI ports and
FL Studio MIDI settings) plus anything destructive.

---

You are setting up **fl-mcp** on this Windows laptop. fl-mcp is an MCP server that lets you control FL Studio 2024 over MIDI SysEx through loopMIDI. The code is finished and tested; your job is installation, registration, and running the manual verification checklist with me. Do each step yourself with commands; do not just tell me what to run. Stop and ask only where a step says "ASK ME", or before anything destructive.

Facts you need:
- GitHub repo: https://github.com/soniyuvraj16/fl-mcp (private; I am logged in to GitHub as soniyuvraj16).
- Target folder: `%USERPROFILE%\Desktop\MCP\FL_studio`. The `MCP` folder may already exist and may hold other MCP projects later. Never touch sibling folders.
- FL Studio 2024 v24.2.2 (All Plugins Edition, 64-bit) is installed on this laptop.
- The server must run on this laptop because loopMIDI is a local virtual MIDI cable.
- Read `README.md` and `docs/TEST_CHECKLIST.md` in the repo once cloned; they are the source of truth for every step below.

## Step 1: prerequisites

Check each tool and install what is missing. Use `winget` with `--accept-source-agreements --accept-package-agreements`. Report versions when done.

1. `git --version` → if missing: `winget install Git.Git`.
2. `uv --version` → if missing: `winget install astral-sh.uv`. uv brings its own Python; do not install Python separately.
3. `claude --version` → should already exist since you are running in it.
4. loopMIDI: check whether `"%ProgramFiles(x86)%\Tobias Erichsen\loopMIDI\loopMIDI.exe"` or `"%ProgramFiles%\Tobias Erichsen\loopMIDI\loopMIDI.exe"` exists. If not, try `winget search loopMIDI`; if winget has it, install it, otherwise tell me to download it from https://www.tobias-erichsen.de/software/loopmidi.html and ASK ME to confirm when installed.

After installing anything via winget, open a fresh shell or refresh `PATH` from the registry before continuing, because the current shell won't see new tools.

## Step 2: get the repo

- Create `%USERPROFILE%\Desktop\MCP` if it does not exist.
- If `Desktop\MCP\FL_studio` does not exist or is empty: `git clone https://github.com/soniyuvraj16/fl-mcp.git "%USERPROFILE%\Desktop\MCP\FL_studio"`. If git prompts for GitHub credentials, tell me and wait.
- If it already contains the repo: `git pull` instead.
- `cd` into it. From here on run everything from inside this folder.

## Step 3: build and self-test

- `uv sync` (creates `.venv` and installs fastmcp, mido, python-rtmidi, plus dev deps).
- `uv run pytest`. All tests must pass (they use a fake FL, no FL needed). If anything fails, show me the output and stop.

## Step 4: install the controller script into FL Studio

- Run `powershell -ExecutionPolicy Bypass -File scripts\install_controller.ps1`.
- Verify the file exists at `<Documents>\Image-Line\FL Studio\Settings\Hardware\FLMCP\device_FLMCP.py` (use `[Environment]::GetFolderPath('MyDocuments')` to find Documents; it may be under OneDrive) and that its first line is exactly `# name=FLMCP`.
- If the `Image-Line\FL Studio\Settings` folder does not exist at all, FL has never been launched on this machine: tell me to open and close FL Studio once, then rerun the script.

## Step 5: loopMIDI ports (GUI step)

loopMIDI ports can only be created in its window. Tell me to:
1. Open loopMIDI.
2. Type `FLMCP_In` in the name box at the bottom and press **+**.
3. Type `FLMCP_Out` and press **+**.
4. In loopMIDI's settings tick "Start minimized" and "Autostart".

ASK ME to confirm, then verify from the repo folder:
```
uv run python -c "import mido; print('IN:', mido.get_input_names()); print('OUT:', mido.get_output_names())"
```
Both lists must contain the ports (Windows may append a number, e.g. `FLMCP_In 1`; that is fine). If not, do not continue; tell me what is listed.

## Step 6: register the MCP server with Claude Code

- Get the absolute repo path (`(Get-Location).Path`).
- Run: `claude mcp add fl-mcp --scope user -- uv --directory "<absolute path>" run fl-mcp`
- Verify with `claude mcp list`; `fl-mcp` must appear. Note: the server will only be available in a **new** Claude Code session, so after this step tell me: "Restart Claude Code in this folder and tell me: continue fl-mcp setup from Step 7." Then stop.

## Step 7: FL Studio MIDI settings (GUI step, new session)

Tell me to do this in FL Studio and ASK ME to confirm each sub-step:
1. Start FL Studio (restart it if it was open before Step 4).
2. Options > MIDI Settings.
3. **Input** list: click `FLMCP_In`, tick **Enable**, set **Controller type** to `FLMCP`, set **Port** to 7.
4. **Output** list: click `FLMCP_Out`, tick **Enable**, set **Port** to 7 (same number; this is how the script's replies reach the server).
5. Make sure `FLMCP_Out` is NOT enabled as an input and `FLMCP_In` is NOT enabled as an output.
6. View > Script output, FLMCP tab. It must say `FLMCP controller loaded. API vNN, input port 7, global index: ...`. Ask me to paste that line to you and record the API version number.

## Step 8: run the manual checklist with me

Open `docs/TEST_CHECKLIST.md` and go through sections 3 to 8 in order. For each item, you call the MCP tool yourself (start with `fl_ping`), then ask me to confirm what I see in FL Studio where the checklist says so (colours, undo history, Ctrl+Z results). Use a throwaway project with a few channels named like "Kick", "Snare", "Serum", "Reese", "Vox"; ask me to create one if the project is empty.

Two steps may require a one-line code change, and the checklist says exactly which:
- Step 5: if Ctrl+Z does not revert a change, set `UNDO_FLAGS = midi.UF_Knob` in `fl_controller/device_FLMCP.py`.
- Step 6: if `#FF0000` shows as blue, set `COLOR_ORDER = "RGB"` in the same file.

If you change that file: rerun `uv run pytest`, rerun the install script, tell me to press "Reload script" in FL's Script output window (or toggle Enable), and re-test. Then commit with a clear message explaining what real FL showed and `git push`, so the desktop can pull it.

## Rules

- Do not modify any code except the two tunables above, and only when the checklist calls for it.
- Never add exec/eval or new commands to the controller.
- Every mutating tool creates an undo point; still, do not run `apply_organization_template` with `dry_run=false` on a real project unless I explicitly say so. Use a throwaway project for the checklist.
- When finished, give me a summary table: each checklist section, pass/fail, the API version FL reported, which byte order and undo flag ended up correct, and the commit hash if you pushed anything.
