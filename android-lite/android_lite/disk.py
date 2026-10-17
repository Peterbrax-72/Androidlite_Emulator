"""Raw disk images. On Windows the file is marked sparse (NTFS), so unused space is not allocated.

Standard library only: the Windows call goes through ctypes.
"""
from __future__ import annotations

import os
import sys

MIB = 1024 * 1024
_FSCTL_SET_SPARSE = 0x000900C4


def _mark_sparse(f) -> bool:
    """Ask NTFS to store the file sparsely. No-op (returns True) outside Windows."""
    if sys.platform != "win32":
        return True
    try:
        import ctypes
        import msvcrt
        from ctypes import wintypes

        handle = wintypes.HANDLE(msvcrt.get_osfhandle(f.fileno()))
        returned = wintypes.DWORD(0)
        ok = ctypes.windll.kernel32.DeviceIoControl(
            handle, _FSCTL_SET_SPARSE, None, 0, None, 0, ctypes.byref(returned), None)
        return bool(ok)
    except (OSError, AttributeError, ValueError):
        return False


def create_disk(path: str, gb: int) -> str:
    """Create an empty raw disk of gb gigabytes. Takes almost no space until the guest writes."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "wb") as f:
        _mark_sparse(f)
        f.truncate(gb * 1024 ** 3)
    return path


def sparse_copy(src: str, dst: str, progress=None) -> str:
    """Copy a raw disk, skipping all-zero blocks so the copy stays small on disk."""
    total = os.path.getsize(src)
    os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
    done = 0
    with open(src, "rb") as s, open(dst, "wb") as d:
        _mark_sparse(d)
        while True:
            block = s.read(MIB)
            if not block:
                break
            if block.strip(b"\x00"):          # contains non-zero data: write it
                d.write(block)
            else:                              # all zeros: leave a hole
                d.seek(len(block), os.SEEK_CUR)
            done += len(block)
            if progress:
                progress(done, total)
        d.truncate(total)
    return dst
