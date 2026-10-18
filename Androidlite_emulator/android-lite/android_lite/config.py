"""Paths, profiles and persistent settings. No Tk or subprocess code here."""
from __future__ import annotations

import json
import logging
import os
import sys

APP_NAME = "AndroidLite"
APP_VERSION = "1.2.0"

ISO_NAME = "android-x86_64-9.0-r2.iso"
ISO_URL = ("https://sourceforge.net/projects/android-x86/files/"
           "Release%209.0/android-x86_64-9.0-r2.iso/download")
QEMU_LISTING_URL = "https://qemu.weilnetz.de/w64/"
QEMU_EXE = "qemu-system-x86_64.exe" if sys.platform == "win32" else "qemu-system-x86_64"

DISK_GB = 8
LITE_BELOW_HOST_GB = 8          # hosts with less RAM than this default to Lite

# Lite: fits PCs with 8 GB RAM. Standard: smoother UI on 16 GB machines.
PROFILES = {
    "standard": {"label": "Standard", "ram_mb": 2560, "cpus": 2},
    "lite": {"label": "Lite", "ram_mb": 1536, "cpus": 2},
}
RAM_RANGE = (1024, 16384)
CPU_RANGE = (1, 16)

DEFAULTS = {
    "profile": "standard",
    "ram_mb": PROFILES["standard"]["ram_mb"],
    "cpus": PROFILES["standard"]["cpus"],
    "qemu_path": "",                      # optional: use an existing QEMU
    "qemu_listing_url": QEMU_LISTING_URL,
    "iso_url": ISO_URL,
    "iso_sha256": "",                     # paste the official SHA256 to enforce it
}


def data_dir() -> str:
    """Per-user data folder. ALITE_HOME overrides it (used by tests and demos)."""
    env = os.environ.get("ALITE_HOME")
    if env:
        return env
    return os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), APP_NAME)


def paths(base: str | None = None) -> dict:
    base = base or data_dir()
    logs = os.path.join(base, "logs")
    return {
        "base": base,
        "qemu_home": os.path.join(base, "qemu"),
        "iso": os.path.join(base, ISO_NAME),
        "disk": os.path.join(base, "vm", "android.img"),
        "settings": os.path.join(base, "settings.json"),
        "logs": logs,
        "log": os.path.join(logs, "alite.log"),
        "qemu_log": os.path.join(logs, "qemu.log"),
    }


def host_ram_mb() -> int | None:
    """Total physical RAM of this PC in MB, or None if it cannot be read."""
    try:
        if sys.platform == "win32":
            import ctypes

            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            stat = MEMORYSTATUSEX()
            stat.dwLength = ctypes.sizeof(stat)
            if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
                return None
            return int(stat.ullTotalPhys // (1024 * 1024))
        with open("/proc/meminfo", encoding="utf-8") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) // 1024
    except (OSError, ValueError, AttributeError):
        return None
    return None


def recommended_profile(host_mb: int | None = None) -> str:
    if host_mb is None:
        host_mb = host_ram_mb()
    if host_mb and host_mb < LITE_BELOW_HOST_GB * 1024:
        return "lite"
    return "standard"


def ram_budget_ok(new_ram_mb: int, running_ram_mb: list[int], host_mb: int | None):
    """Would starting an instance with new_ram_mb overcommit this PC? Returns (ok, message)."""
    if not host_mb:
        return True, ""
    total = sum(running_ram_mb) + new_ram_mb
    if total <= int(host_mb * 0.85):
        return True, ""
    return False, (f"Running instances would use about {total / 1024:.1f} GB of the "
                   f"{host_mb / 1024:.1f} GB in this PC. Close an instance or lower its RAM "
                   "for smoother performance.")


def apply_profile(settings: dict, name: str) -> dict:
    prof = PROFILES[name]
    settings.update(profile=name, ram_mb=prof["ram_mb"], cpus=prof["cpus"])
    return settings


def _clamp(value, rng, default):
    try:
        v = int(value)
    except (TypeError, ValueError):
        return default
    return max(rng[0], min(rng[1], v))


def normalize(settings: dict) -> dict:
    """Force settings into valid ranges. Safe to call on any dict."""
    if settings.get("profile") not in PROFILES:
        settings["profile"] = "standard"
    prof = PROFILES[settings["profile"]]
    settings["ram_mb"] = _clamp(settings.get("ram_mb"), RAM_RANGE, prof["ram_mb"])
    settings["cpus"] = _clamp(settings.get("cpus"), CPU_RANGE, prof["cpus"])
    return settings


def load_settings(p: dict) -> dict:
    s = dict(DEFAULTS)
    s["profile"] = recommended_profile()
    apply_profile(s, s["profile"])
    if os.path.isfile(p["settings"]):
        try:
            with open(p["settings"], encoding="utf-8") as f:
                stored = json.load(f)
            if isinstance(stored, dict):
                s.update(stored)
        except (OSError, ValueError) as e:
            logging.getLogger("alite").warning("Ignoring unreadable settings.json: %s", e)
    return normalize(s)


def save_settings(p: dict, s: dict) -> None:
    os.makedirs(p["base"], exist_ok=True)
    with open(p["settings"], "w", encoding="utf-8") as f:
        json.dump(s, f, indent=2)
