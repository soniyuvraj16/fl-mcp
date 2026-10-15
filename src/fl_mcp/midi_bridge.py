"""
FLBridge: request/response over MIDI SysEx via loopMIDI.

Lifecycle
---------
* Ports are opened lazily on the first request, so the MCP server starts even
  when FL Studio (or loopMIDI) is not running yet.  Each failure gives a
  message that says which port is missing and lists the ports that exist.
* mido delivers incoming SysEx on a background thread owned by python-rtmidi.
  The callback feeds a Reassembler; when a whole JSON message arrives we look
  up the pending request by its "id" and wake the waiting thread.
* Requests are serialised with a lock.  FL answers in order, and one in-flight
  request keeps the failure modes simple (a timeout cannot be confused with a
  late answer to a different call).

Port naming
-----------
loopMIDI shows a port under the name you typed, but Windows may append a
number ("FLMCP_In 2") if a name is reused, so we match exact -> prefix ->
case-insensitive substring.  Override with FL_MCP_IN_PORT / FL_MCP_OUT_PORT.
"""

from __future__ import annotations

import itertools
import os
import threading
from typing import Any

import mido

from .protocol import DEFAULT_CHUNK_PAYLOAD, Reassembler, encode_message


class FLConnectionError(RuntimeError):
    """Raised when we can't reach FL (port missing, no reply, bad reply)."""


class FLCommandError(RuntimeError):
    """Raised when FL ran the command but reported an error."""


def _pick_port(wanted: str, available: list[str]) -> str | None:
    if wanted in available:
        return wanted
    for name in available:
        if name.startswith(wanted):
            return name
    low = wanted.lower()
    for name in available:
        if low in name.lower():
            return name
    return None


class FLBridge:
    def __init__(
        self,
        in_port: str | None = None,
        out_port: str | None = None,
        timeout: float | None = None,
        chunk_payload: int | None = None,
    ) -> None:
        # "in_port" is where WE send (FL's input), "out_port" is FL's output.
        self.in_port_name = in_port or os.environ.get("FL_MCP_IN_PORT", "FLMCP_In")
        self.out_port_name = out_port or os.environ.get("FL_MCP_OUT_PORT", "FLMCP_Out")
        self.timeout = timeout or float(os.environ.get("FL_MCP_TIMEOUT", "5"))
        self.chunk_payload = chunk_payload or int(os.environ.get("FL_MCP_CHUNK", DEFAULT_CHUNK_PAYLOAD))

        self._out: mido.ports.BaseOutput | None = None
        self._in: mido.ports.BaseInput | None = None
        self._lock = threading.Lock()            # one request at a time
        self._ids = itertools.count(1)
        self._frame_ids = itertools.count(0)
        self._reasm = Reassembler()
        self._pending: dict[int, tuple[threading.Event, list]] = {}
        self._pending_lock = threading.Lock()
        self._stray: list[Any] = []               # unsolicited messages (controller crash reports)

    # -- connection -----------------------------------------------------

    def connect(self) -> None:
        if self._out is not None and self._in is not None:
            return
        outs = mido.get_output_names()
        ins = mido.get_input_names()
        send_to = _pick_port(self.in_port_name, outs)
        recv_from = _pick_port(self.out_port_name, ins)
        problems = []
        if send_to is None:
            problems.append(f'loopMIDI port "{self.in_port_name}" not found among MIDI outputs {outs}')
        if recv_from is None:
            problems.append(f'loopMIDI port "{self.out_port_name}" not found among MIDI inputs {ins}')
        if problems:
            raise FLConnectionError(
                "; ".join(problems)
                + ". Create both ports in loopMIDI (see README) or set FL_MCP_IN_PORT / FL_MCP_OUT_PORT."
            )
        self._out = mido.open_output(send_to)
        self._in = mido.open_input(recv_from, callback=self._on_midi)

    def close(self) -> None:
        for p in (self._in, self._out):
            try:
                if p is not None:
                    p.close()
            except Exception:
                pass
        self._in = self._out = None

    def _reset(self) -> None:
        """Drop ports so the next request reopens them (e.g. FL restarted)."""
        self.close()
        self._reasm = Reassembler()

    # -- receive path (rtmidi thread) -----------------------------------

    def _on_midi(self, msg: mido.Message) -> None:
        if msg.type != "sysex":
            return
        try:
            decoded = self._reasm.feed(bytes(msg.data))
        except Exception as e:            # malformed JSON from FL
            decoded = {"id": None, "ok": False, "error": f"undecodable reply: {e}"}
        if decoded is None:
            return
        rid = decoded.get("id") if isinstance(decoded, dict) else None
        with self._pending_lock:
            slot = self._pending.get(rid)
        if slot is None:
            self._stray.append(decoded)
            return
        event, box = slot
        box.append(decoded)
        event.set()

    # -- send path --------------------------------------------------------

    def request(self, cmd: str, args: dict[str, Any] | None = None, timeout: float | None = None) -> Any:
        """Send one command and return its 'result'.  Raises on error/timeout."""
        with self._lock:
            self.connect()
            rid = next(self._ids)
            event, box = threading.Event(), []
            with self._pending_lock:
                self._pending[rid] = (event, box)
            try:
                for inner in encode_message({"id": rid, "cmd": cmd, "args": args or {}},
                                            next(self._frame_ids), self.chunk_payload):
                    self._out.send(mido.Message("sysex", data=inner))
                if not event.wait(timeout or self.timeout):
                    stray = "; ".join(str(s) for s in self._stray[-3:])
                    self._stray.clear()
                    self._reset()
                    raise FLConnectionError(
                        f'No reply from FL Studio to "{cmd}" within {timeout or self.timeout:.1f}s. '
                        "Check: FL Studio is running; Options > MIDI Settings shows FLMCP_In enabled "
                        'with controller type "FLMCP"; FLMCP_Out is enabled as an output with the '
                        "same Port number; View > Script output shows no errors."
                        + (f" Last unsolicited messages: {stray}" if stray else "")
                    )
            except OSError as e:          # port vanished (loopMIDI closed)
                self._reset()
                raise FLConnectionError(f"MIDI port error: {e}") from e
            finally:
                with self._pending_lock:
                    self._pending.pop(rid, None)

        reply = box[0]
        if not isinstance(reply, dict):
            raise FLConnectionError(f"Malformed reply: {reply!r}")
        if not reply.get("ok"):
            raise FLCommandError(reply.get("error", "unknown error"))
        return reply.get("result")


_bridge: FLBridge | None = None


def get_bridge() -> FLBridge:
    global _bridge
    if _bridge is None:
        _bridge = FLBridge()
    return _bridge
