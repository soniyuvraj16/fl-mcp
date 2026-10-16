"""
Offline stem analysis.

FL Studio's scripting API exposes no audio, so "what frequencies does this
track have" can only be answered from exported stems: File > Export > WAV with
"Split mixer tracks" writes one WAV per mixer track.  This module reads those
files and reports where each one's energy sits, plus which stems fight over
the same band.  It runs on the server side (normal Python, numpy available),
never inside FL.

The WAV reader is deliberately our own: the stdlib `wave` module rejects the
32-bit float files FL writes by default.
"""

from __future__ import annotations

import math
import os
import struct

import numpy as np

# (name, low Hz, high Hz) -- the usual mixing vocabulary.
BANDS = [
    ("sub", 20, 60),
    ("bass", 60, 150),
    ("low_mid", 150, 400),
    ("mid", 400, 1000),
    ("high_mid", 1000, 3000),
    ("presence", 3000, 6000),
    ("air", 6000, 20000),
]

FRAME = 8192                 # FFT size; ~5 Hz bins at 44.1 kHz
SILENCE_DB = -80.0           # RMS below this = treat the stem as empty
DOMINANT_SHARE = 0.15        # a band holding >= 15% of a stem's energy is "dominant"
CLASH_SHARE = 0.20           # both stems >= 20% in one band = clash


def _db(x: float) -> float:
    return -math.inf if x <= 0 else 20 * math.log10(x)


def _fmt_db(x: float) -> float | None:
    return None if x == -math.inf else round(x, 1)


# ---------------------------------------------------------------------------
# WAV reading
# ---------------------------------------------------------------------------

def read_wav(path: str, max_seconds: float | None = None) -> tuple[np.ndarray, int]:
    """Return (mono float32 samples in -1..1, sample rate).

    Handles 8/16/24/32-bit PCM and 32/64-bit float, plain or WAVE_FORMAT_EXTENSIBLE.
    """
    with open(path, "rb") as f:
        head = f.read(12)
        if len(head) < 12 or head[:4] != b"RIFF" or head[8:12] != b"WAVE":
            raise ValueError("%s is not a RIFF/WAVE file" % path)
        fmt = None
        data = None
        while True:
            hdr = f.read(8)
            if len(hdr) < 8:
                break
            cid, size = struct.unpack("<4sI", hdr)
            if cid == b"fmt ":
                fmt = f.read(size)
            elif cid == b"data":
                data = f.read(size)
            else:
                f.seek(size, os.SEEK_CUR)
            if size % 2:                      # chunks are word-aligned
                f.seek(1, os.SEEK_CUR)
            if fmt is not None and data is not None:
                break
    if fmt is None or data is None:
        raise ValueError("%s has no fmt/data chunk" % path)

    tag, channels, rate, _byte_rate, _align, bits = struct.unpack("<HHIIHH", fmt[:16])
    if tag == 0xFFFE and len(fmt) >= 26:      # extensible: real tag is in the sub-format GUID
        tag = struct.unpack("<H", fmt[24:26])[0]

    if tag == 3:                              # IEEE float
        dtype = {32: "<f4", 64: "<f8"}.get(bits)
        if dtype is None:
            raise ValueError("%s: unsupported float bit depth %d" % (path, bits))
        samples = np.frombuffer(data, dtype=dtype).astype(np.float32)
    elif tag == 1:                            # PCM
        if bits == 8:
            samples = (np.frombuffer(data, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
        elif bits == 16:
            samples = np.frombuffer(data, dtype="<i2").astype(np.float32) / 32768.0
        elif bits == 24:
            raw = np.frombuffer(data, dtype=np.uint8)
            raw = raw[: len(raw) - len(raw) % 3].reshape(-1, 3)
            ints = (raw[:, 0].astype(np.int32) | (raw[:, 1].astype(np.int32) << 8)
                    | (raw[:, 2].astype(np.int32) << 16))
            ints = np.where(ints >= 1 << 23, ints - (1 << 24), ints)
            samples = ints.astype(np.float32) / float(1 << 23)
        elif bits == 32:
            samples = np.frombuffer(data, dtype="<i4").astype(np.float32) / 2147483648.0
        else:
            raise ValueError("%s: unsupported PCM bit depth %d" % (path, bits))
    else:
        raise ValueError("%s: unsupported WAV format tag %d" % (path, tag))

    if channels > 1:
        samples = samples[: len(samples) - len(samples) % channels].reshape(-1, channels).mean(axis=1)
    if max_seconds is not None:
        samples = samples[: int(max_seconds * rate)]
    return samples, rate


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------

def _power_spectrum(samples: np.ndarray, rate: int) -> tuple[np.ndarray, np.ndarray]:
    """Average |FFT|^2 over Hann-windowed frames.  Returns (freqs, power)."""
    if len(samples) < FRAME:
        samples = np.pad(samples, (0, FRAME - len(samples)))
    window = np.hanning(FRAME)
    n_frames = len(samples) // FRAME
    acc = np.zeros(FRAME // 2 + 1)
    for i in range(n_frames):
        frame = samples[i * FRAME:(i + 1) * FRAME] * window
        acc += np.abs(np.fft.rfft(frame)) ** 2
    return np.fft.rfftfreq(FRAME, 1.0 / rate), acc / max(n_frames, 1)


def _top_peaks(freqs: np.ndarray, power: np.ndarray, top: int) -> list[dict]:
    """Strongest 1/6-octave regions, reported as centre Hz and dB below the loudest."""
    edges = 30.0 * 2 ** (np.arange(0, 56) / 6.0)          # 30 Hz .. ~17 kHz
    region_power = []
    for lo, hi in zip(edges[:-1], edges[1:]):
        m = (freqs >= lo) & (freqs < hi)
        region_power.append((float(power[m].sum()), math.sqrt(lo * hi)))
    best = max(p for p, _ in region_power) or 1.0
    ranked = sorted(range(len(region_power)), key=lambda i: -region_power[i][0])
    out, taken = [], set()
    for i in ranked:
        if region_power[i][0] <= 0 or any(abs(i - j) <= 1 for j in taken):
            continue                                          # skip neighbours of a chosen peak
        taken.add(i)
        out.append({"hz": int(round(region_power[i][1])),
                    "rel_db": round(10 * math.log10(region_power[i][0] / best), 1)})
        if len(out) >= top:
            break
    return out


ST_WINDOW_S = 0.4            # short-term loudness window
ST_HOP_S = 0.1
ONSET_FRAME_S = 0.01         # envelope resolution for transient detection
ONSET_RISE_DB = 6.0          # a frame this far above the recent envelope is an onset
ONSET_FLOOR_DB = -40.0       # ...as long as it is audible
ONSET_REFRACTORY_S = 0.05


def _frame_rms(samples: np.ndarray, win: int, hop: int) -> np.ndarray:
    if len(samples) < win:
        return np.array([math.sqrt(float(np.mean(samples ** 2)))]) if len(samples) else np.array([0.0])
    n = 1 + (len(samples) - win) // hop
    idx = np.arange(win)[None, :] + hop * np.arange(n)[:, None]
    return np.sqrt(np.mean(samples[idx] ** 2, axis=1))


def dynamics(samples: np.ndarray, rate: int) -> dict:
    """How the level moves over time: crest factor, loudness range, transients.

    Spectrum says *where* a track sits; this says whether it is a steady pad
    or a hitting drum, which decides compression and how it masks others.
    """
    peak = float(np.max(np.abs(samples))) if len(samples) else 0.0
    rms = float(np.sqrt(np.mean(samples ** 2))) if len(samples) else 0.0
    crest = (_db(peak) - _db(rms)) if rms > 0 else 0.0

    st = _frame_rms(samples, int(rate * ST_WINDOW_S), int(rate * ST_HOP_S))
    st_db = np.array([_db(v) for v in st if _db(v) > SILENCE_DB])
    lra = float(np.percentile(st_db, 95) - np.percentile(st_db, 10)) if len(st_db) >= 3 else 0.0

    frame = max(1, int(rate * ONSET_FRAME_S))
    env = _frame_rms(samples, frame, frame)
    env_db = np.array([_db(v) for v in env])
    onsets, last = 0, -10 ** 9
    refractory = int(ONSET_REFRACTORY_S / ONSET_FRAME_S)
    for i in range(1, len(env_db)):
        recent = env_db[max(0, i - 10):i]                 # previous 100 ms
        recent = recent[np.isfinite(recent)]
        ref = float(np.median(recent)) if len(recent) else -math.inf
        if env_db[i] >= ONSET_FLOOR_DB and env_db[i] - ref >= ONSET_RISE_DB and i - last > refractory:
            onsets += 1
            last = i
    seconds = len(samples) / rate if rate else 0.0
    per_s = onsets / seconds if seconds > 0 else 0.0

    if crest >= 12 and per_s >= 1:
        character = "percussive"
    elif crest < 8 and per_s < 1:
        character = "sustained"
    else:
        character = "mixed"
    return {"crest_db": round(crest, 1), "loudness_range_db": round(lra, 1),
            "transients_per_s": round(per_s, 1), "character": character}


def spectrum_power(samples: np.ndarray, rate: int) -> np.ndarray:
    """Averaged power spectrum of `samples` (same framing as analyze_stem), for
    use as the `bleed` argument of analyze_stem."""
    return _power_spectrum(samples, rate)[1]


def analyze_stem(samples: np.ndarray, rate: int, top: int = 3,
                 bleed: np.ndarray | None = None) -> dict:
    """Per-stem report.  `bleed` is a power spectrum (from spectrum_power) of
    sound that is present in every capture but does not belong to this stem,
    e.g. channels routed straight to Master when soloing inserts; it is
    subtracted (power is additive for unrelated sources) before the band
    shares, centroid and peaks are computed.  Levels and dynamics stay raw."""
    peak = float(np.max(np.abs(samples))) if len(samples) else 0.0
    rms = float(np.sqrt(np.mean(samples ** 2))) if len(samples) else 0.0
    result = {"duration_s": round(len(samples) / rate, 2), "rate": rate,
              "peak_db": _fmt_db(_db(peak)), "rms_db": _fmt_db(_db(rms))}
    if _db(rms) < SILENCE_DB:
        result["silent"] = True
        return result

    freqs, power = _power_spectrum(samples, rate)
    if bleed is not None and len(bleed) == len(power):
        power = np.maximum(power - bleed, 0.0)
        result["bleed_removed"] = True
        if float(power[(freqs >= 20) & (freqs <= 20000)].sum()) <= 1e-3 * float(bleed.sum()):
            result["silent"] = True          # nothing left once the bleed is gone
            result["bleed_only"] = True
            return result
    audible = (freqs >= 20) & (freqs <= 20000)
    total = float(power[audible].sum()) or 1.0
    bands = {}
    for name, lo, hi in BANDS:
        m = (freqs >= lo) & (freqs < hi)
        bands[name] = round(100.0 * float(power[m].sum()) / total, 1)
    centroid = float((freqs[audible] * power[audible]).sum() / total)
    result.update({
        "silent": False,
        "bands_pct": bands,
        "dominant": [n for n, _, _ in BANDS if bands[n] >= 100 * DOMINANT_SHARE],
        "centroid_hz": int(round(centroid)),
        "top_peaks": _top_peaks(freqs, power, top),
        "dynamics": dynamics(samples, rate),
    })
    return result


def summarize(stems: list[dict]) -> dict:
    """Cross-stem view: band clashes and loudness order.  Each stem dict needs
    "name" plus whatever analyze_stem produced."""
    live = [s for s in stems if not s.get("silent")]
    clashes = []
    for i in range(len(live)):
        for j in range(i + 1, len(live)):
            for name, _, _ in BANDS:
                a, b = live[i]["bands_pct"][name], live[j]["bands_pct"][name]
                if a >= 100 * CLASH_SHARE and b >= 100 * CLASH_SHARE:
                    clashes.append({"band": name, "stems": [live[i]["name"], live[j]["name"]],
                                    "shares_pct": [a, b], "severity": round(min(a, b), 1)})
    clashes.sort(key=lambda c: -c["severity"])
    loudness = sorted(({"name": s["name"], "rms_db": s["rms_db"], "peak_db": s["peak_db"]}
                       for s in live), key=lambda r: -(r["rms_db"] or -999))
    return {"clashes": clashes, "loudness_order": loudness}


def analyze_folder(folder: str, max_seconds: float = 60.0, top: int = 3) -> dict:
    if not os.path.isdir(folder):
        raise ValueError("not a folder: %s" % folder)
    files = sorted(f for f in os.listdir(folder) if f.lower().endswith(".wav"))
    if not files:
        raise ValueError("no .wav files in %s" % folder)

    stems, errors = [], []
    for fn in files:
        path = os.path.join(folder, fn)
        try:
            samples, rate = read_wav(path, max_seconds)
            info = analyze_stem(samples, rate, top)
        except (OSError, ValueError) as e:
            errors.append({"file": fn, "error": str(e)})
            continue
        info["file"] = fn
        info["name"] = os.path.splitext(fn)[0]
        stems.append(info)

    out = {
        "folder": folder,
        "bands_hz": {n: [lo, hi] for n, lo, hi in BANDS},
        "stems": stems,
        "errors": errors,
    }
    out.update(summarize(stems))
    return out
