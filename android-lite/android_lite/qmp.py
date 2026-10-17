"""Minimal QMP (QEMU Machine Protocol) client and screenshot conversion. Standard library only."""
from __future__ import annotations

import json
import os
import socket
import struct
import threading
import time
import zlib


class QMPError(RuntimeError):
    """QEMU answered with an error, or the connection failed in a way the caller should see."""


class QMP:
    """One connection to a QEMU instance started with -qmp tcp:127.0.0.1:PORT,server=on,wait=off."""

    def __init__(self, port: int, timeout: float = 5.0):
        self._sock = socket.create_connection(("127.0.0.1", port), timeout=timeout)
        self._f = self._sock.makefile("rwb")
        try:
            self._f.readline()                 # greeting
            self.execute("qmp_capabilities")
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        for obj in (self._f, self._sock):
            try:
                obj.close()
            except OSError:
                pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def execute(self, command: str, arguments: dict | None = None):
        msg = {"execute": command}
        if arguments:
            msg["arguments"] = arguments
        self._f.write((json.dumps(msg) + "\n").encode())
        self._f.flush()
        while True:
            line = self._f.readline()
            if not line:
                raise QMPError("QMP connection closed")
            resp = json.loads(line)
            if "return" in resp:
                return resp["return"]
            if "error" in resp:
                raise QMPError(resp["error"].get("desc", "QMP error"))
            # anything else is an asynchronous event: keep reading


# QEMU serves one QMP client at a time, so calls to the same VM are serialized in this process.
_port_locks: dict[int, threading.Lock] = {}
_port_locks_guard = threading.Lock()


def _lock_for(port: int) -> threading.Lock:
    with _port_locks_guard:
        return _port_locks.setdefault(port, threading.Lock())


def command(port: int, name: str, arguments: dict | None = None, timeout: float = 5.0):
    with _lock_for(port):
        with QMP(port, timeout) as q:
            return q.execute(name, arguments)


def screendump_bytes(port: int, tmp_path: str, timeout: float = 10.0) -> bytes:
    """Return the guest screen as binary PPM bytes. QEMU writes the file before it replies."""
    tmp_path = os.path.abspath(tmp_path)
    os.makedirs(os.path.dirname(tmp_path), exist_ok=True)
    command(port, "screendump", {"filename": tmp_path}, timeout=timeout)
    try:
        with open(tmp_path, "rb") as f:
            return f.read()
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass


def ppm_size(data: bytes) -> tuple[int, int]:
    _magic, w, h, _maxval, _off = _ppm_header(data)
    return w, h


def query_status(port: int, timeout: float = 1.0) -> str | None:
    """'running', 'paused', ... or None if nothing answers on that port."""
    try:
        return command(port, "query-status", timeout=timeout)["status"]
    except (OSError, ValueError, QMPError, KeyError):
        return None


def screendump_png(port: int, png_path: str, timeout: float = 20.0) -> str:
    """Save the guest screen to png_path (via a temporary PPM from QEMU)."""
    png_path = os.path.abspath(png_path)
    tmp = png_path + ".ppm"
    os.makedirs(os.path.dirname(png_path), exist_ok=True)
    deadline = time.monotonic() + timeout
    while True:
        try:
            command(port, "screendump", {"filename": tmp})     # QEMU needs an absolute path
            break
        except (OSError, ValueError, QMPError) as e:
            if time.monotonic() > deadline:
                raise QMPError(f"screendump failed: {e}") from e
            time.sleep(0.5)
    try:
        ppm_to_png(tmp, png_path)
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    return png_path


def _ppm_header(data: bytes):
    """Return (magic, width, height, maxval, offset_of_pixels) for a PPM file."""
    tokens: list[bytes] = []
    pos, n = 0, len(data)
    while len(tokens) < 4:
        while pos < n and data[pos:pos + 1].isspace():
            pos += 1
        if pos < n and data[pos:pos + 1] == b"#":
            while pos < n and data[pos:pos + 1] not in (b"\n", b""):
                pos += 1
            continue
        start = pos
        while pos < n and not data[pos:pos + 1].isspace():
            pos += 1
        if start == pos:
            raise ValueError("malformed PPM header")
        tokens.append(data[start:pos])
    magic, w, h, maxval = tokens
    return magic, int(w), int(h), int(maxval), pos + 1   # one whitespace byte after maxval


def ppm_to_png(src: str, dst: str) -> str:
    """Convert a binary PPM (P6, 8-bit) to PNG using only zlib and struct."""
    with open(src, "rb") as f:
        data = f.read()
    magic, w, h, maxval, offset = _ppm_header(data)
    if magic != b"P6" or maxval != 255:
        raise ValueError("unsupported PPM (need P6, maxval 255)")
    row = w * 3
    pixels = data[offset: offset + row * h]
    if len(pixels) != row * h:
        raise ValueError("PPM is truncated")
    raw = b"".join(b"\x00" + pixels[y * row:(y + 1) * row] for y in range(h))

    def chunk(kind: bytes, body: bytes) -> bytes:
        return (struct.pack(">I", len(body)) + kind + body
                + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF))

    png = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))   # 8-bit RGB
           + chunk(b"IDAT", zlib.compress(raw, 6))
           + chunk(b"IEND", b""))
    with open(dst, "wb") as f:
        f.write(png)
    return dst
