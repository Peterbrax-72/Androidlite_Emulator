"""Input and frames for a running VM, through QMP. Standard library only.

Input is expressed as normalized events so one recording works at any resolution:
  {"op": "move", "x": 0..1, "y": 0..1}
  {"op": "down", "x": 0..1, "y": 0..1, "btn": "left" | "right" | "middle"}
  {"op": "up", "btn": ...}
  {"op": "key", "key": <QEMU qcode>, "down": bool}
  {"op": "wait", "ms": int}          (only inside macros; the sender sleeps)
"""
from __future__ import annotations

import logging
import queue
import threading
import time

from . import qmp

log = logging.getLogger("android-lite")

ABS_MAX = 32767                        # absolute range of QEMU's usb-tablet

# Tk keysym -> QEMU qcode, for keys that are not a plain letter or digit.
_KEYSYMS = {
    "space": "spc", "Return": "ret", "KP_Enter": "kp_enter", "BackSpace": "backspace",
    "Tab": "tab", "ISO_Left_Tab": "tab", "Escape": "esc", "Left": "left", "Right": "right",
    "Up": "up", "Down": "down", "Home": "home", "End": "end", "Prior": "pgup", "Next": "pgdn",
    "Insert": "insert", "Delete": "delete", "Caps_Lock": "caps_lock",
    "Shift_L": "shift", "Shift_R": "shift_r", "Control_L": "ctrl", "Control_R": "ctrl_r",
    "Alt_L": "alt", "Alt_R": "alt_r", "Super_L": "meta_l", "Super_R": "meta_r",
    "minus": "minus", "equal": "equal", "bracketleft": "bracket_left",
    "bracketright": "bracket_right", "backslash": "backslash", "semicolon": "semicolon",
    "apostrophe": "apostrophe", "grave": "grave_accent", "comma": "comma", "period": "dot",
    "slash": "slash", "XF86AudioRaiseVolume": "volumeup", "XF86AudioLowerVolume": "volumedown",
    "XF86AudioMute": "audiomute",
}
# Keys that need Shift on a US layout: keysym -> base qcode.
_SHIFTED = {
    "exclam": "1", "at": "2", "numbersign": "3", "dollar": "4", "percent": "5",
    "asciicircum": "6", "ampersand": "7", "asterisk": "8", "parenleft": "9", "parenright": "0",
    "underscore": "minus", "plus": "equal", "braceleft": "bracket_left",
    "braceright": "bracket_right", "bar": "backslash", "colon": "semicolon",
    "quotedbl": "apostrophe", "less": "comma", "greater": "dot", "question": "slash",
    "asciitilde": "grave_accent",
}
for _n in range(1, 13):
    _KEYSYMS[f"F{_n}"] = f"f{_n}"

BUTTONS = ("left", "right", "middle")


def resolve_keysym(keysym: str) -> tuple[str, bool] | None:
    """Return (qcode, needs_shift) for a Tk keysym, or None if we cannot send it."""
    if keysym in _KEYSYMS:
        return _KEYSYMS[keysym], False
    if len(keysym) == 1 and keysym.isalnum():
        if keysym.isupper():
            return keysym.lower(), True
        return keysym, False
    if keysym in _SHIFTED:
        return _SHIFTED[keysym], True
    return None


# ---------- normalized events -----------------------------------------------
def ev_move(x: float, y: float) -> dict:
    return {"op": "move", "x": _clamp01(x), "y": _clamp01(y)}


def ev_down(x: float, y: float, btn: str = "left") -> dict:
    return {"op": "down", "x": _clamp01(x), "y": _clamp01(y), "btn": btn}


def ev_up(btn: str = "left") -> dict:
    return {"op": "up", "btn": btn}


def ev_key(key: str, down: bool) -> dict:
    return {"op": "key", "key": key, "down": bool(down)}


def _clamp01(v: float) -> float:
    return min(1.0, max(0.0, float(v)))


def to_qmp(events: list[dict]) -> list[dict]:
    """Convert normalized events to QMP input-send-event entries (waits are dropped)."""
    out: list[dict] = []
    for ev in events:
        op = ev.get("op")
        if op == "move":
            out += _abs(ev["x"], ev["y"])
        elif op == "down":
            out += _abs(ev["x"], ev["y"])
            out.append({"type": "btn", "data": {"down": True, "button": ev.get("btn", "left")}})
        elif op == "up":
            out.append({"type": "btn", "data": {"down": False, "button": ev.get("btn", "left")}})
        elif op == "key":
            out.append({"type": "key", "data": {"down": ev["down"],
                                                "key": {"type": "qcode", "data": ev["key"]}}})
    return out


def _abs(x: float, y: float) -> list[dict]:
    return [{"type": "abs", "data": {"axis": "x", "value": round(_clamp01(x) * ABS_MAX)}},
            {"type": "abs", "data": {"axis": "y", "value": round(_clamp01(y) * ABS_MAX)}}]


# ---------- sending ------------------------------------------------------------
class Sender:
    """Sends input to one VM in order, on its own thread, so the UI never waits on QMP."""

    def __init__(self, port: int):
        self.port = port
        self._q: queue.Queue = queue.Queue()
        self.failed = False
        self._thread = threading.Thread(target=self._run, name=f"input-{port}", daemon=True)
        self._thread.start()

    def send(self, events: list[dict]) -> None:
        if events:
            self._q.put(list(events))

    def close(self) -> None:
        self._q.put(None)

    def _run(self) -> None:
        while True:
            batch = self._q.get()
            if batch is None:
                return
            pending: list[dict] = []
            for ev in batch:
                if ev.get("op") == "wait":
                    self._flush(pending)
                    pending = []
                    time.sleep(max(0, ev.get("ms", 0)) / 1000)
                else:
                    pending.append(ev)
            self._flush(pending)

    def _flush(self, pending: list[dict]) -> None:
        self.failed = _deliver(self.port, pending, self.failed)


def _deliver(port: int, events: list[dict], was_failing: bool) -> bool:
    """Send events now. Returns True if the send failed."""
    payload = to_qmp(events)
    if not payload:
        return was_failing
    try:
        qmp.command(port, "input-send-event", {"events": payload}, timeout=5.0)
        return False
    except (OSError, ValueError, qmp.QMPError) as e:
        if not was_failing:                  # log once per outage, not per event
            log.warning("Input to VM on port %s failed: %s", port, e)
        return True


def play_blocking(port: int, events: list[dict]) -> None:
    """Play a list (with waits) in the calling thread. Used by the CLI, which must wait for it."""
    pending: list[dict] = []
    failing = False
    for ev in events:
        if ev.get("op") == "wait":
            failing = _deliver(port, pending, failing)
            pending = []
            time.sleep(max(0, ev.get("ms", 0)) / 1000)
        else:
            pending.append(ev)
    _deliver(port, pending, failing)


_senders: dict[int, Sender] = {}
_senders_lock = threading.Lock()


def sender_for(port: int) -> Sender:
    with _senders_lock:
        s = _senders.get(port)
        if s is None:
            s = _senders[port] = Sender(port)
        return s


def drop_sender(port: int) -> None:
    with _senders_lock:
        s = _senders.pop(port, None)
    if s:
        s.close()


# ---------- frames -------------------------------------------------------------
def grab_frame(port: int, tmp_path: str) -> bytes:
    """Current guest screen as PPM bytes. Raises OSError/ValueError/QMPError if the VM is not answering."""
    return qmp.screendump_bytes(port, tmp_path)
