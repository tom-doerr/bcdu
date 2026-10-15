"""Streaming scans: retain one directory level, never an entire NAS tree."""

import os
import stat
import time
from collections.abc import Callable
from pathlib import Path
from threading import Event

from .model import Entry, Scan


def scan_directory(path: Path, stop: Event, progress: Callable[[Scan], None]) -> Scan:
    result = Scan(path)
    device = path.stat().st_dev
    last_update = 0.0

    def update(force: bool = False) -> None:
        nonlocal last_update
        if force or time.monotonic() - last_update > 0.2:
            last_update = time.monotonic()
            progress(result)

    def count(target: Path, entry: Entry) -> None:
        try:
            st = target.lstat()
            if stat.S_ISLNK(st.st_mode):
                return
            if st.st_dev != device or (target != path and os.path.ismount(target)):
                entry.note = "Mount boundary skipped"
                return
            if stat.S_ISREG(st.st_mode):
                entry.logical += st.st_size
                entry.allocated += st.st_blocks * 512
                entry.files += 1
                result.files += 1
            elif stat.S_ISDIR(st.st_mode):
                pending.append(target)
        except OSError:
            entry.errors += 1
            result.errors += 1

    try:
        with os.scandir(path) as children:
            for child in children:
                if stop.is_set():
                    return result
                try:
                    kind = ("link" if child.is_symlink() else
                            "dir" if child.is_dir(follow_symlinks=False) else "file")
                    result.entries.append(Entry(Path(child.path), kind, complete=False))
                except OSError:
                    result.errors += 1
    except OSError as exc:
        raise OSError(f"Cannot read {path}: {exc.strerror}") from exc
    update(True)

    # Each pathname is counted. Deliberately no inode-only deduplication: bcachefs
    # snapshots can reuse inode numbers. Shared extents/hard links are documented.
    for entry in result.entries:
        pending: list[Path] = []
        count(entry.path, entry)
        while pending and not stop.is_set():
            directory = pending.pop()
            try:
                with os.scandir(directory) as children:
                    for child in children:
                        if stop.is_set():
                            break
                        count(Path(child.path), entry)
                        update()
            except OSError:
                entry.errors += 1
                result.errors += 1
        if stop.is_set():
            return result
        entry.complete = True
        if entry.kind == "link":
            entry.note = "Symlink; not followed or counted"
        update()
    result.complete = True
    update(True)
    return result
