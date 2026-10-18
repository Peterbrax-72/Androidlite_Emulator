"""Core logic: QEMU discovery and install, VM command line, lifecycle, APK staging.

No UI code here, so the GUI, the CLI and the tests all share it.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

from . import config, disk, instances, qmp

log = logging.getLogger("alite")
USER_AGENT = f"AndroidLite/{config.APP_VERSION}"
MIB = 1024 * 1024
FAST_FAIL_SECONDS = 10     # exit this quickly with an error => accelerator failed, retry with TCG
GRACE_SECONDS = 20         # time Android gets to power off before a forced quit
SHARED_README = ("Android Lite shared folder.\r\n"
                 "Copy .apk files here, then open them from Android's file manager (USB storage).\r\n")
_setup_lock = threading.Lock()   # only one download/install of QEMU or the ISO at a time


class SetupError(RuntimeError):
    """A problem the user can act on (missing QEMU, bad checksum, ...)."""


# ---------- logging ----------------------------------------------------------
def setup_logging(p: dict) -> None:
    """Attach file and console logging once per process."""
    if getattr(log, "_alite_ready", False):
        return
    os.makedirs(p["logs"], exist_ok=True)
    log.setLevel(logging.INFO)
    fh = logging.FileHandler(p["log"], encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(fh)
    if sys.stderr is not None:          # None in a --windowed build
        sh = logging.StreamHandler(sys.stderr)
        sh.setFormatter(logging.Formatter("[android-lite] %(message)s"))
        log.addHandler(sh)
    log._alite_ready = True


# ---------- downloads --------------------------------------------------------
def _open(url: str, timeout: int = 60):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    return urllib.request.urlopen(req, timeout=timeout)


def download(url: str, dest: str, progress=None) -> str:
    """Download url to dest. Writes to dest.part first so partial files never look complete."""
    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    log.info("Downloading %s", url)
    tmp = dest + ".part"
    try:
        with _open(url) as resp, open(tmp, "wb") as out:
            total = int(resp.headers.get("Content-Length") or 0)
            done = 0
            while True:
                chunk = resp.read(MIB)
                if not chunk:
                    break
                out.write(chunk)
                done += len(chunk)
                if progress:
                    progress(done, total)
        os.replace(tmp, dest)
    except OSError as e:
        raise SetupError(f"Download failed ({url}): {e}") from e
    log.info("Saved %s (%d MB)", dest, os.path.getsize(dest) // MIB)
    return dest


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(MIB), b""):
            h.update(block)
    return h.hexdigest()


# ---------- QEMU -------------------------------------------------------------
def find_qemu(p: dict, settings: dict | None = None) -> str | None:
    """Locate qemu-system-x86_64. Order: env override, settings, bundled, per-user install, PATH."""
    settings = settings or {}
    candidates = [os.environ.get("ALITE_QEMU"), settings.get("qemu_path")]
    if getattr(sys, "frozen", False):   # QEMU shipped next to the .exe (MSIX bundle)
        candidates.append(os.path.join(os.path.dirname(sys.executable), config.QEMU_EXE))
    candidates += [os.path.join(p["qemu_home"], config.QEMU_EXE), shutil.which(config.QEMU_EXE)]
    if sys.platform == "win32":
        candidates.append(r"C:\Program Files\qemu\qemu-system-x86_64.exe")
    for c in candidates:
        if c and os.path.isfile(c):
            return c
    return None


def install_qemu(p: dict, settings: dict, progress=None) -> str:
    """One-time install of QEMU for the current user (no admin rights needed)."""
    if sys.platform != "win32":
        raise SetupError("Automatic QEMU install is Windows-only. Install QEMU and set "
                         "ALITE_QEMU or settings.qemu_path.")
    listing_url = settings.get("qemu_listing_url") or config.QEMU_LISTING_URL
    try:
        with _open(listing_url) as r:
            listing = r.read().decode("utf-8", "replace")
    except OSError as e:
        raise SetupError(f"Could not read the QEMU download list: {e}") from e
    builds = re.findall(r"qemu-w64-setup-(\d{8})\.exe", listing)
    if not builds:
        raise SetupError(f"No QEMU installer found at {listing_url}")
    name = f"qemu-w64-setup-{max(builds)}.exe"
    installer = download(listing_url + name, os.path.join(tempfile.gettempdir(), name), progress)
    log.info("Installing QEMU silently into %s", p["qemu_home"])
    try:
        # NSIS installer: /S = silent, /D= must be the last argument (install dir)
        rc = subprocess.run([installer, "/S", "/D=" + p["qemu_home"]], timeout=900).returncode
    finally:
        try:
            os.remove(installer)
        except OSError:
            pass
    qemu = os.path.join(p["qemu_home"], config.QEMU_EXE)
    if rc != 0 or not os.path.isfile(qemu):
        raise SetupError(f"QEMU installer failed (exit {rc}). See the log. "
                         "It may need administrator rights.")
    log.info("QEMU installed: %s", qemu)
    return qemu


def detect_accel(qemu: str) -> tuple[str, str]:
    """Return (accel, description): whpx on Windows, kvm on Linux, else tcg (software)."""
    try:
        out = subprocess.run([qemu, "-accel", "help"], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired) as e:
        log.warning("Could not query QEMU accelerators: %s", e)
        return "tcg", "software emulation (QEMU did not respond)"
    text = (out.stdout + out.stderr).lower()
    if sys.platform == "win32" and "whpx" in text:
        return "whpx", "Windows Hypervisor Platform"
    if sys.platform.startswith("linux") and "kvm" in text and os.access("/dev/kvm", os.R_OK | os.W_OK):
        return "kvm", "KVM"
    return "tcg", "software emulation (slow)"


# ---------- images, disks, shared folders ------------------------------------
def ensure_iso(p: dict, settings: dict, progress=None) -> str:
    iso = p["iso"]
    with _setup_lock:
        if not os.path.isfile(iso):
            download(settings["iso_url"], iso, progress)
            expected = (settings.get("iso_sha256") or "").strip().lower()
            digest = sha256_file(iso)
            log.info("ISO SHA256: %s", digest)
            if expected and digest != expected:
                os.remove(iso)
                raise SetupError("ISO checksum does not match the official SHA256. File removed, try again.")
    return iso


def ensure_qemu(p: dict, settings: dict, progress=None) -> str:
    with _setup_lock:
        qemu = find_qemu(p, settings)
        if qemu:
            log.info("QEMU found: %s", qemu)
            return qemu
        return install_qemu(p, settings, progress)


def setup_base(p: dict, settings: dict, progress=None) -> tuple[str, str]:
    """Make QEMU and the Android image ready. Returns (qemu, iso)."""
    qemu = ensure_qemu(p, settings, progress)
    iso = ensure_iso(p, settings, progress)
    log.info("QEMU and the Android image are ready")
    return qemu, iso


def ensure_disk(path: str, gb: int = config.DISK_GB) -> bool:
    if os.path.isfile(path):
        return False
    log.info("Creating %d GB virtual disk", gb)
    disk.create_disk(path, gb)
    return True


def ensure_shared(path: str) -> str:
    os.makedirs(path, exist_ok=True)
    readme = os.path.join(path, "README.txt")
    if not os.path.isfile(readme):
        with open(readme, "w", encoding="utf-8") as f:
            f.write(SHARED_README)
    return path


def install_apk(shared_dir: str, apk_path: str) -> str:
    """Copy an APK into an instance's shared folder so Android can see it. Returns the new path."""
    if not os.path.isfile(apk_path):
        raise SetupError(f"File not found: {apk_path}")
    if not apk_path.lower().endswith(".apk"):
        raise SetupError("Only .apk files can be installed.")
    with open(apk_path, "rb") as f:
        magic = f.read(4)
    if magic != b"PK\x03\x04":
        raise SetupError("That file is not a valid APK (APKs are zip archives).")
    ensure_shared(shared_dir)
    name = re.sub(r"[^A-Za-z0-9._-]", "_", os.path.basename(apk_path))[-80:]
    dest = os.path.join(shared_dir, name)
    shutil.copyfile(apk_path, dest)
    log.info("Copied %s to the shared folder", name)
    return dest


# ---------- VM command line --------------------------------------------------
def _qemu_path_opt(path: str) -> str:
    return str(path).replace(",", ",,")     # QEMU option values use ',' as separator


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def build_qemu_args(ram_mb: int, cpus: int, accel: str, disk_path: str, iso: str | None = None,
                    display: str | None = None, qmp_port: int | None = None,
                    paused: bool = False, shared_dir: str | None = None) -> list[str]:
    """Minimal device set: one disk, one NIC (user-mode NAT), tablet + keyboard, optional shared folder."""
    cpu_model = "host" if accel in ("whpx", "kvm") else "max"
    args = [
        "-name", "Android-Lite",
        "-machine", "pc",
        "-accel", accel,
        "-cpu", cpu_model,
        "-smp", str(cpus),
        "-m", f"{ram_mb}M",
        "-drive", f"file={_qemu_path_opt(disk_path)},format=raw,if=ide,index=0",
        "-vga", "std",
        "-device", "usb-ehci,id=usb",
        "-device", "usb-tablet",
        "-device", "usb-kbd",
        "-netdev", "user,id=net0",
        "-device", "e1000,netdev=net0",
    ]
    if shared_dir:
        # Host folder exposed to Android as a USB drive (QEMU's virtual FAT driver)
        args += ["-drive", f"file=fat:rw:{_qemu_path_opt(shared_dir)},format=raw,if=none,id=share",
                 "-device", "usb-storage,drive=share"]
    if iso:
        args += ["-cdrom", _qemu_path_opt(iso), "-boot", "order=d"]
    else:
        args += ["-boot", "order=c"]
    if display:
        args += ["-display", display]
    if qmp_port:
        args += ["-qmp", f"tcp:127.0.0.1:{qmp_port},server=on,wait=off"]
    if paused:
        args.append("-S")
    return args


def run_vm(qemu: str, qemu_log: str, *, disk_path: str, ram_mb: int, cpus: int,
           iso: str | None = None, shared_dir: str | None = None, display: str | None = None,
           on_start=None, accel: str | None = None, name: str = "VM") -> int:
    """Run one VM and block until it exits.

    on_start(proc, qmp_port) is called each time QEMU is launched. If WHPX/KVM fails
    immediately, the VM is retried once with software emulation.
    """
    if accel is None:
        accel, why = detect_accel(qemu)
        log.info("Acceleration: %s (%s)", accel, why)
    attempts = [accel] if accel == "tcg" else [accel, "tcg"]
    os.makedirs(os.path.dirname(qemu_log), exist_ok=True)
    rc = 0
    for i, acc in enumerate(attempts):
        port = free_port()
        args = [qemu, *build_qemu_args(ram_mb, cpus, acc, disk_path, iso=iso, display=display,
                                       qmp_port=port, shared_dir=shared_dir)]
        log.info("Starting %s: %d MB RAM, %d vCPU, accel=%s%s", name, ram_mb, cpus, acc,
                 ", installer attached" if iso else "")
        with open(qemu_log, "a", encoding="utf-8", errors="replace") as qlog:
            qlog.write("\n$ " + subprocess.list2cmdline(args) + "\n")
            qlog.flush()
            proc = subprocess.Popen(args, stdout=qlog, stderr=subprocess.STDOUT)
        if on_start:
            on_start(proc, port)
        started = time.monotonic()
        rc = proc.wait()
        fast_fail = time.monotonic() - started < FAST_FAIL_SECONDS
        if rc != 0 and fast_fail and not getattr(proc, "alite_stop", False) and i + 1 < len(attempts):
            log.warning("%s exited at once (code %s) with %s; retrying with software emulation",
                        name, rc, acc)
            continue
        if rc == 0:
            log.info("%s closed normally", name)
        else:
            log.error("%s exited with code %s. Details: %s", name, rc, qemu_log)
        return rc
    return rc


def start_instance(p: dict, s: dict, inst: dict, *, install: bool = False,
                   progress=None, on_start=None) -> int:
    """Start an instance. install=True boots the Android installer (and downloads what is missing)."""
    r = instances.resolve(p, inst)
    if install:
        qemu, iso = setup_base(p, s, progress)
        ensure_disk(r["disk"])
        log.info("Install mode for '%s': choose Installation in the Android menu, create a partition, "
                 "use ext4, install GRUB, then reboot.", inst["name"])
    else:
        qemu = find_qemu(p, s)
        if not qemu:
            raise SetupError("QEMU is not installed yet. Click 'Set up' first.")
        if not os.path.isfile(r["disk"]):
            raise SetupError(f"'{inst['name']}' has no disk yet. Use 'Install Android' first.")
        iso = None
    ensure_shared(r["shared"])
    qemu_log = os.path.join(p["logs"], f"qemu-{inst['id']}.log")
    # Headless: the viewer window shows the screen, so QEMU gets no window of its own.
    rc = run_vm(qemu, qemu_log, disk_path=r["disk"], ram_mb=inst["ram_mb"], cpus=inst["cpus"],
                iso=iso, shared_dir=r["shared"], display="none", on_start=on_start,
                name=f"'{inst['name']}'")
    if install:
        mark_installed(r["folder"])
    return rc


INSTALLED_MARK = "installed.txt"


def mark_installed(folder: str) -> None:
    """Remember that the Android installer has been run for this instance."""
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, INSTALLED_MARK), "w", encoding="utf-8") as f:
        f.write(time.strftime("%Y-%m-%d %H:%M:%S") + "\n")


def is_installed(folder: str) -> bool:
    return os.path.isfile(os.path.join(folder, INSTALLED_MARK))


# ---------- control of a running VM (works from any process) -----------------
def _alive(proc, port: int) -> bool:
    if proc is not None:
        return proc.poll() is None
    return qmp.query_status(port, timeout=1.0) is not None


def stop_vm(port: int, proc=None, grace: float = GRACE_SECONDS) -> str:
    """Ask Android to power off; force-quit QEMU if it does not finish within grace seconds."""
    if not _alive(proc, port):
        return "not running"
    try:
        qmp.command(port, "system_powerdown")
    except (OSError, ValueError, qmp.QMPError) as e:
        log.warning("Power-off request failed: %s", e)
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        if not _alive(proc, port):
            log.info("Android powered off cleanly")
            return "powered off"
        time.sleep(0.5)
    log.warning("Android did not power off within %d s; forcing QEMU to quit", grace)
    try:
        qmp.command(port, "quit")
    except (OSError, ValueError, qmp.QMPError):
        pass
    if proc is not None:
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
    return "forced quit"


def pause_vm(port: int, pause: bool) -> str:
    """Freeze or resume the guest. A paused VM uses almost no CPU, which helps with several instances."""
    qmp.command(port, "stop" if pause else "cont")
    return qmp.query_status(port) or "unknown"


def screenshot(port: int, png_path: str) -> str:
    return qmp.screendump_png(port, png_path)
