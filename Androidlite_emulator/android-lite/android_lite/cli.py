"""Command-line front end: the same operations as the GUI, for scripts and tests."""
from __future__ import annotations

import argparse
import os
import sys
import time

from . import config, core, instances, qmp

PROFILE_CHOICES = list(config.PROFILES)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="AndroidLite",
                                 description="Lightweight Android-x86 instances for Windows (QEMU + WHPX)")
    sub = ap.add_subparsers(dest="cmd", required=True, metavar="COMMAND")
    sub.add_parser("status", help="show engine, acceleration, RAM and instance states")
    sub.add_parser("setup", help="install QEMU and download the Android image (once)")
    sub.add_parser("list", help="list instances")

    n = sub.add_parser("new", help="create an instance")
    n.add_argument("name")
    n.add_argument("--profile", choices=PROFILE_CHOICES)
    n.add_argument("--ram", type=int, help="RAM in MB")
    n.add_argument("--cpus", type=int, help="virtual CPUs")

    c = sub.add_parser("clone", help="copy an instance, including its installed apps")
    c.add_argument("source")
    c.add_argument("name")

    e = sub.add_parser("edit", help="change an instance's name or resources")
    e.add_argument("id", nargs="?", default="main")
    e.add_argument("--name")
    e.add_argument("--profile", choices=PROFILE_CHOICES)
    e.add_argument("--ram", type=int)
    e.add_argument("--cpus", type=int)

    r = sub.add_parser("remove", help="delete an instance and its files")
    r.add_argument("id")

    for cmd, text in [("install", "boot the Android installer in an instance"),
                      ("start", "boot an installed instance (blocks until it closes)"),
                      ("stop", "power off a running instance"),
                      ("pause", "freeze a running instance"),
                      ("resume", "resume a paused instance"),
                      ("shared", "print the instance's shared folder")]:
        x = sub.add_parser(cmd, help=text)
        x.add_argument("id", nargs="?", default="main")

    s = sub.add_parser("screenshot", help="save a PNG of a running instance")
    s.add_argument("id", nargs="?", default="main")
    s.add_argument("--out", help="output file (default: screenshots folder)")

    a = sub.add_parser("apk", help="copy an APK into an instance's shared folder")
    a.add_argument("file")
    a.add_argument("--to", default="main", metavar="ID")

    v = sub.add_parser("view", help="open the window for a running instance (screen, keys, touch)")
    v.add_argument("id", nargs="?", default="main")

    sub.add_parser("macros", help="list recorded macros")
    pl = sub.add_parser("play", help="play a macro on a running instance")
    pl.add_argument("name")
    pl.add_argument("--to", default="main", metavar="ID")
    return ap


def _progress(done: int, total: int) -> None:
    if total and (done == total or done % (64 * core.MIB) < core.MIB):
        print(f"  {done * 100 // total}%", flush=True)


def _live_port(p: dict, s: dict, entry: dict) -> int | None:
    """QMP port of a running instance, or None. Clears stale registry entries."""
    port = entry.get("qmp_port")
    if port and qmp.query_status(port, timeout=1.0) is not None:
        return port
    if port:
        instances.set_port(p, s, entry["id"], None)
    return None


def _state(p, s, entry) -> str:
    port = _live_port(p, s, entry)
    if not port:
        return "stopped"
    return "paused" if qmp.query_status(port, 1.0) == "paused" else "running"


def _cmd_status(p, s) -> int:
    qemu = core.find_qemu(p, s)
    accel = core.detect_accel(qemu) if qemu else ("-", "QEMU not installed")
    host = config.host_ram_mb()
    print(f"Engine        : {qemu or 'not installed'}")
    print(f"Acceleration  : {accel[0]} - {accel[1]}")
    print(f"Android image : {'ready' if os.path.isfile(p['iso']) else 'not downloaded'}")
    print(f"Host RAM      : {f'{host / 1024:.1f} GB' if host else 'unknown'}")
    print(f"Data folder   : {p['base']}")
    print("Instances:")
    for it in instances.load(p, s):
        print(f"  {it['id']:<10} {it['name']:<20} {_state(p, s, it):<8} {it['ram_mb']} MB, {it['cpus']} vCPU")
    return 0


def _cmd_list(p, s) -> int:
    print(f"{'ID':<10} {'NAME':<20} {'STATE':<9} {'PROFILE':<9} {'RAM':>7} {'vCPU':>5}  DISK")
    for it in instances.load(p, s):
        disk_ok = os.path.isfile(instances.resolve(p, it)["disk"])
        print(f"{it['id']:<10} {it['name']:<20} {_state(p, s, it):<9} {it['profile']:<9} "
              f"{it['ram_mb']:>5} MB {it['cpus']:>5}  {'ready' if disk_ok else 'no disk'}")
    return 0


def _cmd_new(p, s, args) -> int:
    entry = instances.create(p, s, args.name, args.profile, args.ram, args.cpus)
    print(f"Created '{entry['name']}' (id {entry['id']}). Next: alite install {entry['id']}")
    return 0


def _cmd_clone(p, s, args) -> int:
    entry = instances.clone(p, s, args.source, args.name, progress=_progress)
    print(f"Cloned to '{entry['name']}' (id {entry['id']})")
    return 0


def _cmd_edit(p, s, args) -> int:
    entry = instances.update(p, s, args.id, name=args.name, profile=args.profile,
                             ram_mb=args.ram, cpus=args.cpus)
    print(f"Saved '{entry['name']}': {entry['ram_mb']} MB, {entry['cpus']} vCPU ({entry['profile']})")
    return 0


def _cmd_remove(p, s, args) -> int:
    entry = instances.get(instances.load(p, s), args.id)
    if _live_port(p, s, entry):
        print("error: stop the instance before removing it", file=sys.stderr)
        return 2
    instances.remove(p, s, args.id)
    print("Removed.")
    return 0


def _cmd_boot(p, s, args, install: bool) -> int:
    entry = instances.get(instances.load(p, s), args.id)
    if _live_port(p, s, entry):
        print(f"error: '{entry['name']}' is already running", file=sys.stderr)
        return 2
    def on_start(proc, port):
        instances.set_port(p, s, entry["id"], port)
        print(f"'{entry['name']}' is running (no window of its own). "
              f"Open it with: AndroidLite view {entry['id']}", flush=True)

    try:
        rc = core.start_instance(p, s, entry, install=install, progress=_progress, on_start=on_start)
    finally:
        instances.set_port(p, s, entry["id"], None)
    return rc


def _running(p, s, ident: str):
    entry = instances.get(instances.load(p, s), ident)
    port = _live_port(p, s, entry)
    if not port:
        print(f"error: '{entry['name']}' is not running", file=sys.stderr)
        return None, None
    return entry, port


def _cmd_stop(p, s, args) -> int:
    entry, port = _running(p, s, args.id)
    if not port:
        return 2
    print(core.stop_vm(port))
    instances.set_port(p, s, entry["id"], None)
    return 0


def _cmd_pause(p, s, args, pause: bool) -> int:
    entry, port = _running(p, s, args.id)
    if not port:
        return 2
    print(f"'{entry['name']}': {core.pause_vm(port, pause)}")
    return 0


def _cmd_screenshot(p, s, args) -> int:
    entry, port = _running(p, s, args.id)
    if not port:
        return 2
    out = args.out or os.path.join(p["base"], "screenshots",
                                   f"{entry['name']}-{time.strftime('%Y%m%d-%H%M%S')}.png")
    print(core.screenshot(port, out))
    return 0


def _cmd_apk(p, s, args) -> int:
    entry = instances.get(instances.load(p, s), args.to)
    dest = core.install_apk(instances.resolve(p, entry)["shared"], args.file)
    print(f"Copied to {dest}\nIn Android, open the Files app > USB storage and tap the APK.")
    return 0


def _cmd_shared(p, s, args) -> int:
    entry = instances.get(instances.load(p, s), args.id)
    print(core.ensure_shared(instances.resolve(p, entry)["shared"]))
    return 0


def _cmd_view(p, s, args) -> int:
    import tkinter as tk
    from . import viewer
    entry, port = _running(p, s, args.id)
    if not port:
        return 2
    root = tk.Tk()
    root.withdraw()
    viewer.Viewer(root, p, entry, port, on_close=lambda _w: root.destroy())
    root.mainloop()
    return 0


def _cmd_macros(p, s, args) -> int:
    from . import automation
    names = automation.list_macros(p["base"])
    print("\n".join(names) if names else "No macros yet. Record one from an instance window (Record).")
    return 0


def _cmd_play(p, s, args) -> int:
    from . import automation, vmio
    entry, port = _running(p, s, args.to)
    if not port:
        return 2
    events = automation.to_playback(automation.load_macro(p["base"], args.name))
    vmio.play_blocking(port, events)
    print(f"Played '{args.name}' on '{entry['name']}'")
    return 0


def run(args) -> int:
    p = config.paths()
    core.setup_logging(p)
    s = config.load_settings(p)
    try:
        if args.cmd == "status":
            return _cmd_status(p, s)
        if args.cmd == "setup":
            core.setup_base(p, s, _progress)
            print("Setup complete.")
            return 0
        if args.cmd == "list":
            return _cmd_list(p, s)
        if args.cmd == "new":
            return _cmd_new(p, s, args)
        if args.cmd == "clone":
            return _cmd_clone(p, s, args)
        if args.cmd == "edit":
            return _cmd_edit(p, s, args)
        if args.cmd == "remove":
            return _cmd_remove(p, s, args)
        if args.cmd == "install":
            return _cmd_boot(p, s, args, install=True)
        if args.cmd == "start":
            return _cmd_boot(p, s, args, install=False)
        if args.cmd == "stop":
            return _cmd_stop(p, s, args)
        if args.cmd == "pause":
            return _cmd_pause(p, s, args, True)
        if args.cmd == "resume":
            return _cmd_pause(p, s, args, False)
        if args.cmd == "screenshot":
            return _cmd_screenshot(p, s, args)
        if args.cmd == "apk":
            return _cmd_apk(p, s, args)
        if args.cmd == "view":
            return _cmd_view(p, s, args)
        if args.cmd == "macros":
            return _cmd_macros(p, s, args)
        if args.cmd == "play":
            return _cmd_play(p, s, args)
        if args.cmd == "shared":
            return _cmd_shared(p, s, args)
    except (core.SetupError, ValueError, KeyError, qmp.QMPError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    return 1
