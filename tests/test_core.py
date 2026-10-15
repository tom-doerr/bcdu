import os
from pathlib import Path
from threading import Event

from bcdu.parse import parse_usage
from bcdu.scan import scan_directory


def test_scan_counts_pathnames_and_skips_links(tmp_path: Path):
    directory = tmp_path / "directory"
    directory.mkdir()
    (directory / "data").write_bytes(b"a" * 12345)
    os.link(directory / "data", directory / "hardlink")
    (tmp_path / "loop").symlink_to(tmp_path, target_is_directory=True)
    with (tmp_path / "sparse").open("wb") as stream:
        stream.truncate(1024 * 1024)
    scan = scan_directory(tmp_path, Event(), lambda _: None)
    assert scan.complete and scan.files == 3
    assert scan.logical == 1024 * 1024 + 2 * 12345
    assert scan.allocated < scan.logical
    assert next(e for e in scan.entries if e.kind == "link").logical == 0


def test_cancelled_scan_is_not_complete(tmp_path: Path):
    stop = Event()
    stop.set()
    (tmp_path / "file").touch()
    result = scan_directory(tmp_path, stop, lambda _: None)
    assert not result.complete


def test_usage_keeps_empty_healthy_column():
    report = "\n".join([
        "Filesystem: synthetic", "Size: 100000", "Used: 9000", "Online reserved: 100",
        "Replicated:", f"{'':6}{'undegraded':>14}{'-1x':>14}",
        f"{'2x:':6}{7000:>14}{200:>14}", "Erasure coded (data+parity):",
        f"{'':6}{'undegraded':>14}{'-1x':>14}", f"{'6+2:':6}{'':>14}{1800:>14}",
        "Compression:", "type compressed uncompressed average extent size",
        "zstd 1000 3000 512", "hdd.one (device 1): sdb rw 50000 4500 9%",
        "hdd.two (device 2): dev-2 offline - - -",
    ])
    usage = parse_usage(report)
    assert not usage.error
    assert usage.capacity == 100000 and usage.used == 9000
    assert usage.degraded == 2000
    assert usage.layouts[-1].kind == "ec" and usage.layouts[-1].missing == 1
    assert usage.compression[0].ratio == 3
    assert usage.devices[1].state == "offline" and usage.devices[1].used is None


def test_unknown_report_is_not_zero_usage_or_healthy():
    usage = parse_usage("different CLI version")
    assert usage.used is None and usage.degraded is None and usage.error
