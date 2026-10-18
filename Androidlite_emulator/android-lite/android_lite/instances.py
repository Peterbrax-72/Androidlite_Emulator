"""Multi-instance registry. Each instance has its own disk, shared folder and resource settings.

Stored as instances.json in the data folder. Paths in the file are relative to the data folder.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import threading
import time
import uuid

from . import config, disk

log = logging.getLogger("alite")
MAIN_ID = "main"
NAME_MAX = 32
_lock = threading.RLock()


def _file(p: dict) -> str:
    return os.path.join(p["base"], "instances.json")


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M")


def _main_entry(s: dict) -> dict:
    return {
        "id": MAIN_ID, "name": "Android 1", "profile": s["profile"],
        "ram_mb": s["ram_mb"], "cpus": s["cpus"],
        "disk": "vm/android.img", "shared": "shared/main",
        "created": _now(), "qmp_port": None,
    }


def load(p: dict, s: dict) -> list[dict]:
    """All instances. The main instance is always present (it is recreated if missing)."""
    with _lock:
        items: list[dict] = []
        f = _file(p)
        if os.path.isfile(f):
            try:
                with open(f, encoding="utf-8") as fh:
                    data = json.load(fh)
                if isinstance(data, list):
                    items = [i for i in data if isinstance(i, dict) and "id" in i and "name" in i]
            except (OSError, ValueError) as e:
                log.warning("Ignoring unreadable instances.json: %s", e)
        if not any(i["id"] == MAIN_ID for i in items):
            items.insert(0, _main_entry(s))
        return items


def save(p: dict, items: list[dict]) -> None:
    with _lock:
        os.makedirs(p["base"], exist_ok=True)
        f = _file(p)
        tmp = f + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(items, fh, indent=2)
        os.replace(tmp, f)


def get(items: list[dict], ident: str | None) -> dict:
    """Find by id or name (case-insensitive). None means the main instance."""
    key = (ident or MAIN_ID).strip().lower()
    for i in items:
        if i["id"].lower() == key or i["name"].lower() == key:
            return i
    raise KeyError(f"No instance called '{ident}'.")


def resolve(p: dict, inst: dict) -> dict:
    """Absolute paths for an instance."""
    disk_path = os.path.join(p["base"], inst["disk"])
    return {
        "disk": disk_path,
        "shared": os.path.join(p["base"], inst["shared"]),
        "folder": os.path.dirname(disk_path),
    }


def _clean_name(name: str | None, items: list[dict], exclude: str | None = None) -> str:
    name = (name or "").strip()
    if not name:
        raise ValueError("The name cannot be empty.")
    if len(name) > NAME_MAX:
        raise ValueError(f"The name must be {NAME_MAX} characters or fewer.")
    for i in items:
        if i["id"] != exclude and i["name"].lower() == name.lower():
            raise ValueError(f"An instance called '{name}' already exists.")
    return name


def create(p: dict, s: dict, name: str, profile: str | None = None,
           ram_mb: int | None = None, cpus: int | None = None) -> dict:
    with _lock:
        items = load(p, s)
        name = _clean_name(name, items)
        prof = config.PROFILES.get(profile or s["profile"], config.PROFILES["standard"])
        settings = config.normalize({"profile": profile or s["profile"],
                                     "ram_mb": ram_mb or prof["ram_mb"],
                                     "cpus": cpus or prof["cpus"]})
        iid = uuid.uuid4().hex[:8]
        rel = f"instances/{iid}"
        entry = {"id": iid, "name": name, "profile": settings["profile"],
                 "ram_mb": settings["ram_mb"], "cpus": settings["cpus"],
                 "disk": f"{rel}/android.img", "shared": f"{rel}/shared",
                 "created": _now(), "qmp_port": None}
        disk.create_disk(resolve(p, entry)["disk"], config.DISK_GB)
        items.append(entry)
        save(p, items)
        log.info("Created instance '%s' (%s, %d MB, %d vCPU)", name, settings["profile"],
                 settings["ram_mb"], settings["cpus"])
        return entry


def clone(p: dict, s: dict, src_ident: str, name: str, progress=None) -> dict:
    with _lock:
        items = load(p, s)
        src = get(items, src_ident)
        src_disk = resolve(p, src)["disk"]
        if not os.path.isfile(src_disk):
            raise ValueError(f"'{src['name']}' has no disk yet. Install Android in it first.")
        name = _clean_name(name, items)
        iid = uuid.uuid4().hex[:8]
        rel = f"instances/{iid}"
        entry = dict(src, id=iid, name=name, disk=f"{rel}/android.img", shared=f"{rel}/shared",
                     created=_now(), qmp_port=None)
        log.info("Cloning '%s' to '%s' (copies the disk; this can take a minute)", src["name"], name)
        disk.sparse_copy(src_disk, resolve(p, entry)["disk"], progress)
        items.append(entry)
        save(p, items)
        log.info("Clone '%s' ready", name)
        return entry


def update(p: dict, s: dict, ident: str, **fields) -> dict:
    with _lock:
        items = load(p, s)
        entry = get(items, ident)
        if "name" in fields and fields["name"] is not None:
            entry["name"] = _clean_name(fields["name"], items, exclude=entry["id"])
        if "profile" in fields and fields["profile"]:
            entry["profile"] = fields["profile"]
        for k in ("ram_mb", "cpus"):
            if fields.get(k) is not None:
                entry[k] = fields[k]
        clamped = config.normalize({"profile": entry["profile"], "ram_mb": entry["ram_mb"],
                                    "cpus": entry["cpus"]})
        entry.update(ram_mb=clamped["ram_mb"], cpus=clamped["cpus"])
        save(p, items)
        return entry


def set_port(p: dict, s: dict, ident: str, port: int | None) -> None:
    """Remember the QMP port of a running instance so other processes can control it."""
    with _lock:
        items = load(p, s)
        get(items, ident)["qmp_port"] = port
        save(p, items)


def remove(p: dict, s: dict, ident: str) -> None:
    """Delete an instance's files and registry entry. The main instance is recreated empty on next load."""
    with _lock:
        items = load(p, s)
        entry = get(items, ident)
        r = resolve(p, entry)
        shutil.rmtree(r["folder"], ignore_errors=True)
        if entry["id"] == MAIN_ID:
            shutil.rmtree(r["shared"], ignore_errors=True)
        items = [i for i in items if i["id"] != entry["id"]]
        save(p, items)
        log.info("Removed instance '%s'", entry["name"])


def main_instance(p: dict, s: dict) -> dict:
    return get(load(p, s), MAIN_ID)
