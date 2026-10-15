"""Parse numeric bcachefs reports; missing fields stay unknown, never zero."""

import re

from .model import Compression, Device, Layout, Usage


def parse_usage(report: str) -> Usage:
    usage = Usage(raw=report)
    section = ""
    columns: list[tuple[int, int]] = []
    for line in report.splitlines():
        stripped = line.strip()
        match = re.match(r"^(Size|Used|Online reserved|Free):\s+(\d+)\b", stripped)
        if match:
            attr = {"Size": "capacity", "Used": "used", "Online reserved": "reserved",
                    "Free": "free"}[match[1]]
            setattr(usage, attr, int(match[2]))
        if stripped.startswith("Replicated:"):
            section, columns = "replicated", []
        elif stripped.startswith("Erasure coded"):
            section, columns = "ec", []
        elif stripped == "Compression:":
            section = "compression"
        elif "undegraded" in stripped:
            columns = [(m.end(), 0 if m[0] == "undegraded" else int(m[0][1:-1]))
                       for m in re.finditer(r"undegraded|-\d+x", line)]
        elif section in ("replicated", "ec") and columns:
            row = re.match(r"\s*(\d+x|\d+\+\d+):", line)
            if row:
                # Tables are right-aligned. split() would turn a blank healthy
                # column into a healthy value when only the -1x cell is present.
                for value in re.finditer(r"\d+", line[row.end():]):
                    end = row.end() + value.end()
                    anchor, missing = min(columns, key=lambda col: abs(col[0] - end))
                    if abs(anchor - end) <= 2:
                        usage.layouts.append(Layout(section, row[1], missing, int(value[0])))
        if section == "compression":
            match = re.match(r"\s*(\S+)\s+(\d+)\s+(\d+)(?:\s+(\d+))?\s*$", line)
            if match:
                usage.compression.append(Compression(match[1], int(match[2]), int(match[3]),
                                                     int(match[4]) if match[4] else None))
        device = re.match(r"\s*(.*?)\s+\(device\s+\d+\):\s+(\S+)\s+"
                          r"(rw|ro|failed|spare|offline)\s+([\d-]+)\s+([\d-]+)", line)
        if device:
            usage.devices.append(Device(device[1], device[2], device[3],
                                        int(device[4]) if device[4] != "-" else None,
                                        int(device[5]) if device[5] != "-" else None))
    if usage.capacity is None or usage.used is None:
        usage.error = "Unrecognized usage report; open the raw report to inspect it."
    return usage
