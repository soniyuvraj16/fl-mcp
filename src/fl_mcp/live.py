"""
Live capture of FL Studio's output via WASAPI loopback.

Windows lets a process record whatever a playback device is rendering
("loopback").  FL Studio ASIO and plain WASAPI/DirectSound devices render
through Windows, so their output is capturable; a hardware ASIO driver or
ASIO4ALL talks to the interface directly and loopback hears silence.

pyaudiowpatch (a PyAudio fork with loopback support) is Windows-only and
imported lazily so the server still starts without it.
"""

from __future__ import annotations

import threading
import time

import numpy as np

FRAMES_PER_BUFFER = 1024


def _pyaudio():
    try:
        import pyaudiowpatch as pa
    except ImportError:
        raise RuntimeError(
            "pyaudiowpatch is not installed (Windows only); run `uv sync` in the fl-mcp folder"
        ) from None
    return pa


def loopback_devices() -> list[dict]:
    """Every WASAPI loopback device, default first."""
    pa = _pyaudio()
    p = pa.PyAudio()
    try:
        try:
            default = p.get_default_wasapi_loopback()["index"]
        except Exception:
            default = None
        out = []
        for i in range(p.get_device_count()):
            d = p.get_device_info_by_index(i)
            if d.get("isLoopbackDevice"):
                out.append({"index": i, "name": d["name"], "rate": int(d["defaultSampleRate"]),
                            "channels": int(d["maxInputChannels"]), "default": i == default})
        out.sort(key=lambda d: not d["default"])
        return out
    finally:
        p.terminate()


def record(seconds: float, device_index: int | None = None) -> tuple[np.ndarray, int]:
    """Capture `seconds` of the loopback device as mono float32.  Returns
    (samples, rate).  Uses a callback stream with a deadline: when nothing is
    rendering, WASAPI loopback delivers no packets at all, and a blocking
    read would hang forever."""
    pa = _pyaudio()
    p = pa.PyAudio()
    try:
        if device_index is None:
            dev = p.get_default_wasapi_loopback()
        else:
            dev = p.get_device_info_by_index(device_index)
            if not dev.get("isLoopbackDevice"):
                raise RuntimeError("device %d (%s) is not a loopback device" % (device_index, dev["name"]))
        rate = int(dev["defaultSampleRate"])
        channels = int(dev["maxInputChannels"])
        want = int(seconds * rate)
        chunks: list[np.ndarray] = []
        got = 0
        done = threading.Event()

        def on_data(in_data, frame_count, time_info, status):
            nonlocal got
            chunks.append(np.frombuffer(in_data, dtype=np.float32).copy())
            got += frame_count
            if got >= want:
                done.set()
                return (None, pa.paComplete)
            return (None, pa.paContinue)

        stream = p.open(format=pa.paFloat32, channels=channels, rate=rate, input=True,
                        input_device_index=dev["index"], frames_per_buffer=FRAMES_PER_BUFFER,
                        stream_callback=on_data)
        try:
            done.wait(seconds + 2.0)                  # deadline: silence delivers no packets
        finally:
            stream.stop_stream()
            stream.close()
    finally:
        p.terminate()

    if not chunks:
        return np.zeros(want, dtype=np.float32), rate
    data = np.concatenate(chunks)
    data = data[: len(data) - len(data) % channels].reshape(-1, channels).mean(axis=1)
    return data[:want].astype(np.float32), rate
