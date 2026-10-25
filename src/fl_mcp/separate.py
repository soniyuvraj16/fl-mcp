"""
Stem separation through the audio-separator command line (the UVR models:
BS-Roformer, MDX-Net, Demucs).  It is a separate, heavy install, so it lives
outside this project's environment:

    uv tool install "audio-separator[cpu]"           # or [gpu] with a CUDA torch

The server shells out to the executable rather than importing torch itself.
"""

from __future__ import annotations

import glob
import os
import shutil
import subprocess
import time

DEFAULT_MODEL = "model_bs_roformer_ep_317_sdr_12.9755.ckpt"   # best vocal model as of 2026
STEM_NAMES = {"vocals": "Vocals", "instrumental": "Instrumental", "all": None}


def find_executable() -> str | None:
    """audio-separator from FL_MCP_SEPARATOR, PATH, or uv's tool bin."""
    env = os.environ.get("FL_MCP_SEPARATOR")
    if env and os.path.isfile(env):
        return env
    found = shutil.which("audio-separator")
    if found:
        return found
    for cand in (os.path.expanduser("~/.local/bin/audio-separator.exe"),
                 os.path.expanduser("~/.local/bin/audio-separator")):
        if os.path.isfile(cand):
            return cand
    return None


def find_ffmpeg_dir() -> str | None:
    """audio-separator shells out to `ffmpeg`; find it on PATH or in a winget install."""
    found = shutil.which("ffmpeg")
    if found:
        return os.path.dirname(found)
    local = os.environ.get("LOCALAPPDATA", "")
    pats = [os.path.join(local, "Microsoft", "WinGet", "Packages", "Gyan.FFmpeg*", "**", "bin", "ffmpeg.exe"),
            os.path.join(local, "Microsoft", "WinGet", "Links", "ffmpeg.exe"),
            os.path.join("C:\\", "ffmpeg", "bin", "ffmpeg.exe")]
    for pat in pats:
        hits = glob.glob(pat, recursive=True)
        if hits:
            return os.path.dirname(hits[0])
    return None


def separate(file: str, output_dir: str | None = None, stems: str = "vocals",
             model: str | None = None, output_format: str = "WAV",
             timeout_s: float = 3600.0, runner=subprocess.run) -> dict:
    """Run the separator on `file`. Returns the written stem files."""
    if not os.path.isfile(file):
        raise ValueError("file not found: %s" % file)
    if stems not in STEM_NAMES:
        raise ValueError("stems must be one of %s" % ", ".join(STEM_NAMES))
    exe = find_executable()
    if not exe:
        raise RuntimeError("audio-separator is not installed; run: uv tool install \"audio-separator[cpu]\" "
                           "(or [gpu]) and make sure it is on PATH or set FL_MCP_SEPARATOR to the executable")
    output_dir = output_dir or os.path.join(os.path.dirname(os.path.abspath(file)), "stems")
    os.makedirs(output_dir, exist_ok=True)
    cmd = [exe, file, "--output_dir", output_dir, "--output_format", output_format,
           "-m", model or DEFAULT_MODEL, "--use_soundfile", "--log_level", "warning"]
    single = STEM_NAMES[stems]
    if single:
        cmd += ["--single_stem", single]
    env = dict(os.environ)
    ffdir = find_ffmpeg_dir()
    if ffdir:
        env["PATH"] = ffdir + os.pathsep + env.get("PATH", "")
    else:
        raise RuntimeError("ffmpeg was not found; audio-separator needs it on PATH "
                           "(Windows: winget install Gyan.FFmpeg)")
    before = {f: os.path.getmtime(os.path.join(output_dir, f)) for f in os.listdir(output_dir)}
    t0 = time.time()
    proc = runner(cmd, capture_output=True, text=True, timeout=timeout_s, env=env)
    elapsed = round(time.time() - t0, 1)
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-8:]
        raise RuntimeError("audio-separator failed (exit %d): %s" % (proc.returncode, " | ".join(tail)))
    written = sorted(os.path.join(output_dir, f) for f in os.listdir(output_dir)
                     if f not in before or os.path.getmtime(os.path.join(output_dir, f)) > before[f])
    return {"input": file, "output_dir": output_dir, "model": model or DEFAULT_MODEL, "stems": stems,
            "files": written, "seconds": elapsed, "executable": exe}
