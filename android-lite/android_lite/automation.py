"""Keymaps, macros and the recorder. Standard library only.

Keymap: per instance, `keymap.json` in the instance folder. Each entry maps a key to a screen
position (normalized 0..1). Pressing the key taps that position while the key is held.

Macros: global, stored as `macros/<name>.json` in the data folder. A macro is a list of
normalized input events with a time offset `t` (seconds) from the first event.
"""
from __future__ import annotations

import json
import os
import re
import time

from . import vmio

KEYMAP_FILE = "keymap.json"
MACRO_VERSION = 1
_NAME_RE = re.compile(r"^[A-Za-z0-9 _.-]{1,32}$")


class AutomationError(ValueError):
    pass


# ---------- keymap -----------------------------------------------------------
def load_keymap(path: str) -> list[dict]:
    if not os.path.isfile(path):
        return []
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return []                          # a broken keymap should not stop the VM from opening
    out = []
    for e in raw.get("entries", []) if isinstance(raw, dict) else []:
        try:
            out.append(validate_entry(e))
        except (AutomationError, KeyError, TypeError, ValueError):
            continue
    return out


def save_keymap(path: str, entries: list[dict]) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"version": 1, "entries": [validate_entry(e) for e in entries]}, f, indent=2)
    os.replace(tmp, path)


def validate_entry(e: dict) -> dict:
    key = str(e["key"]).strip()
    if not key:
        raise AutomationError("empty key")
    x, y = float(e["x"]), float(e["y"])
    if not (0 <= x <= 1 and 0 <= y <= 1):
        raise AutomationError("position must be between 0 and 1")
    return {"key": key, "x": round(x, 4), "y": round(y, 4)}


def keymap_events(entries: list[dict], qcode: str, down: bool) -> list[dict] | None:
    """Events for a mapped key, or None if the key has no mapping."""
    for e in entries:
        if e["key"] == qcode:
            if down:
                return [vmio.ev_move(e["x"], e["y"]), vmio.ev_down(e["x"], e["y"], "left")]
            return [vmio.ev_up("left")]
    return None


# ---------- macros -----------------------------------------------------------
def macros_dir(base: str) -> str:
    return os.path.join(base, "macros")


def check_macro_name(name: str) -> str:
    name = (name or "").strip()
    if not _NAME_RE.match(name):
        raise AutomationError("use 1-32 letters, digits, spaces, '-', '_' or '.'")
    return name


def list_macros(base: str) -> list[str]:
    d = macros_dir(base)
    if not os.path.isdir(d):
        return []
    return sorted(f[:-5] for f in os.listdir(d) if f.endswith(".json"))


def save_macro(base: str, name: str, events: list[dict]) -> str:
    name = check_macro_name(name)
    if not events:
        raise AutomationError("nothing was recorded")
    d = macros_dir(base)
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, name + ".json")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"version": MACRO_VERSION, "name": name, "events": events}, f, indent=2)
    os.replace(tmp, path)
    return path


def load_macro(base: str, name: str) -> list[dict]:
    name = check_macro_name(name)
    path = os.path.join(macros_dir(base), name + ".json")
    if not os.path.isfile(path):
        raise AutomationError(f"no macro called '{name}'")
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return list(data.get("events", []))


def delete_macro(base: str, name: str) -> None:
    name = check_macro_name(name)
    path = os.path.join(macros_dir(base), name + ".json")
    if os.path.isfile(path):
        os.remove(path)


def to_playback(events: list[dict]) -> list[dict]:
    """Turn recorded events (with time offsets) into sendable events with wait gaps."""
    out: list[dict] = []
    prev = 0.0
    for ev in events:
        t = float(ev.get("t", prev))
        gap = t - prev
        if gap > 0.005:
            out.append({"op": "wait", "ms": int(gap * 1000)})
        prev = max(prev, t)
        out.append({k: v for k, v in ev.items() if k != "t"})
    return out


class Recorder:
    """Collects the input events a viewer sends while recording."""

    def __init__(self):
        self.events: list[dict] = []
        self._t0: float | None = None

    def add(self, events: list[dict]) -> None:
        for ev in events:
            if ev.get("op") == "wait":
                continue
            now = time.monotonic()
            if self._t0 is None:
                self._t0 = now
            rec = dict(ev)
            rec["t"] = round(now - self._t0, 3)
            self.events.append(rec)
