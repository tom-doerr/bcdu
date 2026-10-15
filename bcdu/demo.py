"""Entirely synthetic data for screenshots and a zero-I/O interactive demo."""

from pathlib import Path

from .backend import BackendError, Edit, Mount, validate
from .model import Compression, Device, Entry, Layout, Policy, Scan, Usage

GIB = 1024**3
TIB = 1024**4
ROOT = Path("/demo/pool")


class DemoBackend:
    mount = Mount(ROOT, "bcachefs")

    def __init__(self, read_only: bool = False):
        self.read_only = read_only
        self.overrides: dict[Path, dict[str, str]] = {}

    def policy(self, path: Path) -> Policy:
        effective = {"compression": "lz4", "background_compression": "zstd:3",
                     "data_replicas": "2", "erasure_code": "1", "data_checksum": "crc32c",
                     "foreground_target": "ssd", "background_target": "hdd",
                     "promote_target": "ssd"}
        for ancestor in [*reversed(path.parents), path]:
            effective.update(self.overrides.get(ancestor, {}))
        return Policy(effective, self.overrides.get(path, {}).copy())

    def usage(self) -> Usage:
        return Usage(capacity=72*TIB, used=47*TIB, reserved=2*GIB, free=25*TIB,
                     compression=[Compression("zstd", 12*TIB, 29*TIB, 128*1024),
                                  Compression("lz4", 4*TIB, 6*TIB, 64*1024),
                                  Compression("incompressible", 9*TIB, 9*TIB, 256*1024)],
                     layouts=[Layout("replicated", "2x", 0, 9*TIB),
                              Layout("ec", "6+2", 0, 37*TIB),
                              Layout("ec", "6+2", 1, TIB)],
                     devices=[Device(f"hdd.archive{i}", f"sd{chr(97+i)}", "rw", 12*TIB,
                                     int((6.4 + i*.22)*TIB)) for i in range(6)] +
                             [Device("hdd.archive6", "dev-6", "offline", None, None),
                              Device("ssd.hot", "nvme0n1", "rw", 2*TIB, 630*GIB)],
                     raw="Synthetic demo. No filesystem was queried or changed.")

    def prepare(self, path: Path, option: str, value: str) -> Edit:
        if self.read_only:
            raise BackendError("Read-only mode: policy editing is disabled.")
        return Edit(path, option, validate(option, value), (0, 0),
                    self.policy(path).explicit.get(option), not path.suffix)

    def apply(self, edit: Edit) -> str:
        if self.read_only:
            raise BackendError("Read-only mode")
        values = self.overrides.setdefault(edit.path, {})
        if edit.value == "-":
            values.pop(edit.option, None)
        else:
            values[edit.option] = edit.value
        return "Demo policy updated in memory. No filesystem was changed."

    def inspect_extents(self, path: Path) -> str:
        return ("SYNTHETIC EXTENTS — no disk access\n\nObserved compression: zstd\n"
                "Reported durability: 2\nEC stripe references: 8\n\n"
                "Actual inspection queries only the selected inode and snapshot.\n"
                "Requires a recent bcachefs CLI and kernel online-query support.")


def demo_scan(path: Path) -> Scan:
    if path == ROOT:
        specs = [("media", "dir", 27.4*TIB, 27.4*TIB, 18432),
                 ("backups", "dir", 14.2*TIB, 13.9*TIB, 124028),
                 ("datasets", "dir", 8.6*TIB, 8.6*TIB, 904516),
                 ("projects", "dir", 1.8*TIB, 1.7*TIB, 62894),
                 ("virtual-machines", "dir", 1.2*TIB, 420*GIB, 28),
                 ("incoming", "dir", 94*GIB, 94*GIB, 362),
                 ("README.txt", "file", 2048, 4096, 1)]
    elif len(path.relative_to(ROOT).parts) < 3:
        specs = [("archive", "dir", 1200*GIB, 1200*GIB, 2084),
                 ("collection.zst", "file", 430*GIB, 430*GIB, 1),
                 ("index.json", "file", 82*1024, 84*1024, 1)]
    else:
        specs = [("sample.bin", "file", 14*GIB, 14*GIB, 1)]
    entries = [Entry(path / name, kind, int(logical), int(allocated), count)
               for name, kind, logical, allocated, count in specs]
    return Scan(path, entries, sum(e.files for e in entries), complete=True)
