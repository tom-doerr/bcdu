from dataclasses import dataclass, field
from pathlib import Path


def size(value: int | None) -> str:
    if value is None:
        return "—"
    number = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB", "PiB"):
        if abs(number) < 1024 or unit == "PiB":
            return f"{number:,.0f} B" if unit == "B" else f"{number:,.1f} {unit}"
        number /= 1024
    return "—"


@dataclass
class Entry:
    path: Path
    kind: str
    logical: int = 0
    allocated: int = 0
    files: int = 0
    errors: int = 0
    complete: bool = True
    note: str = ""


@dataclass
class Scan:
    path: Path
    entries: list[Entry] = field(default_factory=list)
    files: int = 0
    errors: int = 0
    complete: bool = False

    @property
    def logical(self) -> int:
        return sum(e.logical for e in self.entries)

    @property
    def allocated(self) -> int:
        return sum(e.allocated for e in self.entries)


@dataclass
class Policy:
    effective: dict[str, str] = field(default_factory=dict)
    explicit: dict[str, str] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)


@dataclass
class Compression:
    algorithm: str
    compressed: int
    uncompressed: int
    average_extent: int | None = None

    @property
    def ratio(self) -> float | None:
        return self.uncompressed / self.compressed if self.compressed else None


@dataclass
class Layout:
    kind: str
    geometry: str
    missing: int
    bytes: int


@dataclass
class Device:
    label: str
    name: str
    state: str
    capacity: int | None
    used: int | None


@dataclass
class Usage:
    capacity: int | None = None
    used: int | None = None
    reserved: int | None = None
    free: int | None = None
    compression: list[Compression] = field(default_factory=list)
    layouts: list[Layout] = field(default_factory=list)
    devices: list[Device] = field(default_factory=list)
    raw: str = ""
    error: str = ""

    @property
    def degraded(self) -> int | None:
        if not self.layouts:
            return None
        return sum(row.bytes for row in self.layouts if row.missing > 0)
