"""
Stem analysis on synthetic WAVs: pure tones in known bands, written in the
formats FL Studio can export (16/24-bit PCM, 32-bit float, mono and stereo).
"""

from __future__ import annotations

import struct

import numpy as np
import pytest

from fl_mcp.stems import analyze_folder, read_wav

RATE = 44100


def write_wav(path, samples: np.ndarray, *, bits=16, float_fmt=False, channels=1):
    """Minimal WAV writer so we control the format tag and bit depth exactly."""
    if channels == 2:
        samples = np.repeat(samples, 2)
    if float_fmt:
        tag, data = 3, samples.astype("<f4").tobytes()
    elif bits == 16:
        tag, data = 1, (samples * 32767).astype("<i2").tobytes()
    elif bits == 24:
        ints = (samples * (2 ** 23 - 1)).astype(np.int32)
        data = b"".join(int(v).to_bytes(3, "little", signed=True) for v in ints)
        tag = 1
    else:
        raise ValueError(bits)
    block = channels * bits // 8
    fmt = struct.pack("<HHIIHH", tag, channels, RATE, RATE * block, block, bits)
    body = b"WAVE" + b"fmt " + struct.pack("<I", len(fmt)) + fmt + b"data" + struct.pack("<I", len(data)) + data
    path.write_bytes(b"RIFF" + struct.pack("<I", len(body)) + body)


def tone(hz, seconds=1.0, amp=0.5):
    t = np.arange(int(RATE * seconds)) / RATE
    return (amp * np.sin(2 * np.pi * hz * t)).astype(np.float32)


@pytest.fixture
def stems_dir(tmp_path):
    write_wav(tmp_path / "song_kick.wav", tone(50), bits=16, channels=2)
    write_wav(tmp_path / "song_bass.wav", tone(100), bits=24)
    write_wav(tmp_path / "song_sub2.wav", tone(90, amp=0.25), float_fmt=True, bits=32)
    write_wav(tmp_path / "song_hat.wav", tone(8000, amp=0.1), float_fmt=True, bits=32)
    write_wav(tmp_path / "song_empty.wav", np.zeros(RATE, dtype=np.float32), bits=16)
    (tmp_path / "notes.txt").write_text("ignored")
    return tmp_path


def test_read_wav_formats(stems_dir):
    for name, hz in (("song_kick", 50), ("song_bass", 100), ("song_sub2", 90), ("song_hat", 8000)):
        samples, rate = read_wav(str(stems_dir / f"{name}.wav"))
        assert rate == RATE and len(samples) == RATE
        # 16-bit and 24-bit quantisation stay well inside this tolerance
        assert np.allclose(samples[:200], tone(hz)[:200] * (0.5 if name == "song_sub2" else 1) * (0.2 if name == "song_hat" else 1), atol=2e-3)
    short, _ = read_wav(str(stems_dir / "song_bass.wav"), max_seconds=0.25)
    assert len(short) == RATE // 4
    (stems_dir / "bad.wav").write_bytes(b"RIFF\x00\x00\x00\x00JUNK")
    with pytest.raises(ValueError):
        read_wav(str(stems_dir / "bad.wav"))


def test_analyze_folder(stems_dir):
    r = analyze_folder(str(stems_dir), max_seconds=10)
    by = {s["name"]: s for s in r["stems"]}
    assert set(by) == {"song_kick", "song_bass", "song_sub2", "song_hat", "song_empty"}
    assert by["song_empty"]["silent"] is True and "bands_pct" not in by["song_empty"]

    assert by["song_kick"]["dominant"] == ["sub"] and by["song_kick"]["bands_pct"]["sub"] > 90
    assert by["song_bass"]["dominant"] == ["bass"]
    assert by["song_hat"]["dominant"] == ["air"]
    # top spectral peak lands on the tone, within a 1/6-octave region
    assert abs(by["song_bass"]["top_peaks"][0]["hz"] - 100) < 15
    assert by["song_bass"]["top_peaks"][0]["rel_db"] == 0.0
    assert abs(by["song_hat"]["centroid_hz"] - 8000) < 400
    assert by["song_kick"]["peak_db"] == pytest.approx(-6.0, abs=0.1)

    # bass and sub2 both live in the bass band; nothing else overlaps
    assert [(c["band"], sorted(c["stems"])) for c in r["clashes"]] == [("bass", ["song_bass", "song_sub2"])]
    assert r["clashes"][0]["severity"] > 90
    order = [l["name"] for l in r["loudness_order"]]
    assert set(order[:2]) == {"song_kick", "song_bass"} and order[2:] == ["song_sub2", "song_hat"]
    assert r["errors"] == [] and r["bands_hz"]["bass"] == [60, 150]

    (stems_dir / "broken.wav").write_bytes(b"nope")
    r = analyze_folder(str(stems_dir))
    assert r["errors"] == [{"file": "broken.wav", "error": f"{stems_dir / 'broken.wav'} is not a RIFF/WAVE file"}]
    with pytest.raises(ValueError):
        analyze_folder(str(stems_dir / "missing"))
