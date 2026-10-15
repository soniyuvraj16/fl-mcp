"""
Wire protocol shared with the FL-side controller (fl_controller/device_FLMCP.py).

The controller cannot import this module (it runs in FL's own interpreter
with no access to our venv), so the framing logic is deliberately tiny and
duplicated byte-for-byte there.  If you change anything here, change it there
too.

Frame layout
------------
    F0 7D 46 4C  fid_hi fid_lo  idx_hi idx_lo  tot_hi tot_lo  <payload>  F7

* 0x7D is the manufacturer id reserved for non-commercial use, so we never
  collide with a real hardware device.
* 'F' 'L' is our own sub-id.
* fid is a 14-bit frame id grouping the chunks of one message.
* idx / tot are the chunk index and chunk count (14-bit each).
* payload is printable ASCII (0x20-0x7E), produced by json.dumps(...,
  ensure_ascii=True).  That is what keeps the stream 7-bit safe: SysEx data
  bytes must never have the high bit set, and 0xF7 must only appear as the
  terminator.

mido hides the F0/F7 bytes from us -- a mido 'sysex' Message carries only
the bytes in between -- so encode()/decode() work on that inner slice.
"""

from __future__ import annotations

import json
from typing import Any

MANUFACTURER_ID = 0x7D
SUB_ID = (0x46, 0x4C)           # 'F', 'L'
HEADER_LEN = 9                  # inner header: 7D 46 4C fid fid idx idx tot tot
DEFAULT_CHUNK_PAYLOAD = 240


def _split14(v: int) -> tuple[int, int]:
    return (v >> 7) & 0x7F, v & 0x7F


def _u14(hi: int, lo: int) -> int:
    return ((hi & 0x7F) << 7) | (lo & 0x7F)


def encode_message(obj: Any, frame_id: int, chunk_payload: int = DEFAULT_CHUNK_PAYLOAD) -> list[bytes]:
    """JSON-encode obj and split it into SysEx *inner* byte strings.

    Each returned item is what goes into mido.Message('sysex', data=...).
    """
    payload = json.dumps(obj, ensure_ascii=True, separators=(",", ":")).encode("ascii")
    chunks = [payload[i:i + chunk_payload] for i in range(0, len(payload), chunk_payload)] or [b""]
    total = len(chunks)
    frames = []
    for idx, chunk in enumerate(chunks):
        head = bytes((MANUFACTURER_ID,) + SUB_ID + _split14(frame_id & 0x3FFF) + _split14(idx) + _split14(total))
        frames.append(head + chunk)
    return frames


class Reassembler:
    """Collects frames and yields complete decoded JSON messages.

    Frames of different messages may interleave (they won't in practice,
    because the controller answers one request at a time, but it costs
    nothing to be correct).
    """

    def __init__(self) -> None:
        self._inbox: dict[int, dict] = {}

    def feed(self, inner: bytes) -> Any | None:
        """Feed one SysEx inner payload.  Returns a decoded message when one
        completes, None otherwise.  Frames that aren't ours are ignored."""
        if len(inner) < HEADER_LEN:
            return None
        if inner[0] != MANUFACTURER_ID or (inner[1], inner[2]) != SUB_ID:
            return None
        fid = _u14(inner[3], inner[4])
        idx = _u14(inner[5], inner[6])
        total = max(1, _u14(inner[7], inner[8]))
        chunk = bytes(inner[HEADER_LEN:])
        entry = self._inbox.setdefault(fid, {"total": total, "parts": {}})
        entry["parts"][idx] = chunk
        if len(entry["parts"]) < entry["total"]:
            return None
        del self._inbox[fid]
        payload = b"".join(entry["parts"][i] for i in range(entry["total"]))
        return json.loads(payload.decode("ascii"))
