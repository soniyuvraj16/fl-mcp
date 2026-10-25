"""separate_stems: command construction and result handling, with the executable faked."""

from __future__ import annotations

import os
import subprocess

import pytest

from fl_mcp import separate


def test_separate_builds_command_and_reports_new_files(tmp_path, monkeypatch):
    song = tmp_path / "song.flac"
    song.write_bytes(b"flac")
    out = tmp_path / "stems"
    out.mkdir()
    (out / "old.wav").write_bytes(b"old")
    monkeypatch.setattr(separate, "find_executable", lambda: "C:/fake/audio-separator.exe")
    monkeypatch.setattr(separate, "find_ffmpeg_dir", lambda: "C:/fake/ffmpeg/bin")
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        assert kw["env"]["PATH"].startswith("C:/fake/ffmpeg/bin")
        (out / "song_(Vocals)_model.wav").write_bytes(b"new")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    r = separate.separate(str(song), stems="vocals", runner=fake_run)
    cmd = calls[0]
    assert cmd[0] == "C:/fake/audio-separator.exe" and cmd[1] == str(song)
    assert cmd[cmd.index("--output_dir") + 1] == str(out)
    assert cmd[cmd.index("-m") + 1] == separate.DEFAULT_MODEL
    assert cmd[cmd.index("--single_stem") + 1] == "Vocals" and "--output_format" in cmd
    assert [os.path.basename(f) for f in r["files"]] == ["song_(Vocals)_model.wav"]   # old.wav not reported
    assert r["output_dir"] == str(out) and r["stems"] == "vocals"

    r = separate.separate(str(song), stems="all", model="x.onnx", output_dir=str(tmp_path / "o2"), runner=fake_run)
    assert "--single_stem" not in calls[1] and calls[1][calls[1].index("-m") + 1] == "x.onnx"
    assert os.path.isdir(tmp_path / "o2")


def test_separate_errors(tmp_path, monkeypatch):
    with pytest.raises(ValueError, match="not found"):
        separate.separate(str(tmp_path / "missing.wav"))
    song = tmp_path / "s.wav"; song.write_bytes(b"x")
    with pytest.raises(ValueError, match="stems must be"):
        separate.separate(str(song), stems="drums")
    monkeypatch.setattr(separate, "find_executable", lambda: None)
    with pytest.raises(RuntimeError, match="not installed"):
        separate.separate(str(song))
    monkeypatch.setattr(separate, "find_executable", lambda: "exe")
    monkeypatch.setattr(separate, "find_ffmpeg_dir", lambda: None)
    with pytest.raises(RuntimeError, match="ffmpeg"):
        separate.separate(str(song))
    monkeypatch.setattr(separate, "find_ffmpeg_dir", lambda: "C:/ff")
    failing = lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, "", "boom\nCUDA out of memory")
    with pytest.raises(RuntimeError, match="out of memory"):
        separate.separate(str(song), runner=failing)


def test_find_executable_prefers_env(tmp_path, monkeypatch):
    exe = tmp_path / "audio-separator.exe"; exe.write_bytes(b"")
    monkeypatch.setenv("FL_MCP_SEPARATOR", str(exe))
    assert separate.find_executable() == str(exe)
