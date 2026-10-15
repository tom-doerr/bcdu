"""Linux interfaces and a separately installed bcachefs CLI. Never invokes a shell."""

import ctypes
import errno
import json
import os
import re
import selectors
import shutil
import stat
import struct
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from .model import Policy, Usage
from .parse import parse_usage

OPTIONS = {
    "compression": "New writes: none, lz4, gzip[:1-9], zstd[:1-15].",
    "background_compression": "Background rewrite compression: none, lz4, gzip, zstd[:level].",
    "data_replicas": "Desired data durability (1–4). With EC, bcachefs caps this at 3.",
    "erasure_code": "0 disables EC; 1 requests EC. Existing data may need reconciliation.",
    "data_checksum": "Data checksum: none, crc32c, crc64, xxhash. Disabling reduces detection.",
    "foreground_target": "Device/group label for new writes, or 0 for no target.",
    "background_target": "Device/group label for background placement, or 0 for no target.",
    "promote_target": "Device/group label for read promotion, or 0 for no target.",
}


class BackendError(RuntimeError):
    pass


def run(args: list[str], timeout: float = 25, limit: int = 8 * 1024 * 1024) -> tuple[str, str]:
    env = {**os.environ, "LC_ALL": "C", "NO_COLOR": "1", "TERM": "dumb"}
    try:
        process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   stdin=subprocess.DEVNULL, env=env)
    except OSError as exc:
        raise BackendError(str(exc)) from exc
    output = {"out": bytearray(), "err": bytearray()}
    deadline = time.monotonic() + timeout
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ, "out")
            selector.register(process.stderr, selectors.EVENT_READ, "err")
            while selector.get_map():
                if time.monotonic() >= deadline:
                    raise BackendError("Command timed out. A settings change may have partially applied.")
                for key, _ in selector.select(timeout=0.1):
                    chunk = os.read(key.fileobj.fileno(), 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                    else:
                        output[key.data].extend(chunk)
                        if sum(map(len, output.values())) > limit:
                            raise BackendError("Report exceeded the 8 MiB limit; inspection stopped.")
            process.wait(timeout=max(0.1, deadline - time.monotonic()))
        stdout, stderr = (bytes(output[key]).decode(errors="replace") for key in ("out", "err"))
        if process.returncode:
            raise BackendError((stderr or stdout or f"Exit status {process.returncode}").strip())
        return stdout, stderr
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()
        process.stdout.close()
        process.stderr.close()


@dataclass(frozen=True)
class Mount:
    path: Path
    fs_type: str


def mount_for(path: Path, mountinfo: str | None = None) -> Mount:
    resolved = path.resolve()
    text = Path("/proc/self/mountinfo").read_text() if mountinfo is None else mountinfo
    mounts = []
    for line in text.splitlines():
        try:
            fields, extra = line.split(" - ", 1)
            encoded = fields.split()[4]
            mount = Path(re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), encoded))
            if resolved.is_relative_to(mount):
                mounts.append(Mount(mount, extra.split()[0]))
        except (ValueError, IndexError):
            continue
    return max(mounts, key=lambda m: len(m.path.parts)) if mounts else Mount(Path("/"), "unknown")


def subvolume_id(path: Path) -> int | None:
    libc = ctypes.CDLL(None, use_errno=True)
    if not hasattr(libc, "statx"):
        return None
    buf = ctypes.create_string_buffer(256)
    call = libc.statx
    call.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_uint, ctypes.c_void_p]
    call.restype = ctypes.c_int
    if call(-100, os.fsencode(path), 0x100, 0x8000, buf) != 0:
        return None
    mask = struct.unpack_from("=I", buf.raw)[0]
    return struct.unpack_from("=Q", buf.raw, 0xA0)[0] if mask & 0x8000 else None


def validate(option: str, value: str) -> str:
    value = value.strip()
    if option not in OPTIONS:
        raise BackendError("Unsupported policy option")
    if value == "-":
        return value
    if not value or len(value) > 120 or any(ord(c) < 32 for c in value):
        raise BackendError("Enter a value, or - to restore inheritance.")
    if "compression" in option:
        match = re.fullmatch(r"(none|lz4|gzip|zstd)(?::(\d+))?", value)
        if not match or (match[2] and (match[1] not in ("gzip", "zstd") or
                                     not 1 <= int(match[2]) <= (9 if match[1] == "gzip" else 15))):
            raise BackendError("Use none, lz4, gzip[:1-9], or zstd[:1-15].")
    elif option == "data_replicas" and value not in ("1", "2", "3", "4"):
        raise BackendError("Data replicas must be 1–4; the installed kernel validates support.")
    elif option == "erasure_code" and value not in ("0", "1"):
        raise BackendError("Erasure coding must be 0 or 1.")
    elif option == "data_checksum" and value not in ("none", "crc32c", "crc64", "xxhash"):
        raise BackendError("Use none, crc32c, crc64, or xxhash.")
    elif option.endswith("target") and not re.fullmatch(r"[A-Za-z0-9_.:/-]+", value):
        raise BackendError("Use a device or group label, or 0 for no target.")
    return value


@dataclass
class Edit:
    path: Path
    option: str
    value: str
    inode: tuple[int, int]
    previous: str | None
    directory: bool


class Backend:
    def __init__(self, path: Path, binary: str | None = None, read_only: bool = False):
        self.mount = mount_for(path)
        self.read_only = read_only
        self.binary = binary or shutil.which("bcachefs")
        if not self.binary and (Path.home() / "bin/bcachefs").is_file():
            self.binary = str(Path.home() / "bin/bcachefs")
        self._supported: set[str] | None = None

    def command(self, *args: str, **kwargs) -> tuple[str, str]:
        if not self.binary:
            raise BackendError("bcachefs-tools is not installed or not on PATH. Use --bcachefs-bin.")
        return run([self.binary, *args], **kwargs)

    def require_bcachefs(self, path: Path) -> None:
        if mount_for(path).fs_type != "bcachefs":
            raise BackendError("This path is not on a mounted bcachefs filesystem.")

    def usage(self) -> Usage:
        if self.mount.fs_type != "bcachefs":
            return Usage(error=f"{self.mount.fs_type} filesystem • bcachefs accounting unavailable")
        try:
            output, warning = self.command("fs", "usage", "-f", "compression", str(self.mount.path))
            usage = parse_usage(output)
            if warning:
                usage.error = warning.strip()
            return usage
        except BackendError as exc:
            return Usage(error=str(exc))

    def policy(self, path: Path) -> Policy:
        result = Policy()
        for prefix, values in (("bcachefs_effective.", result.effective),
                               ("bcachefs.", result.explicit)):
            for option in OPTIONS:
                try:
                    values[option] = os.getxattr(path, prefix + option,
                                               follow_symlinks=False).decode(errors="replace")
                except OSError as exc:
                    if exc.errno not in (errno.ENODATA, errno.ENOTSUP, errno.EINVAL):
                        message = exc.strerror or str(exc)
                        if message not in result.errors:
                            result.errors.append(message)
        return result

    def supported_options(self) -> set[str]:
        if self._supported is None:
            help_text, _ = self.command("set-file-option", "--help")
            self._supported = {key for key in OPTIONS if f"--{key}" in help_text}
        return self._supported

    def prepare(self, path: Path, option: str, value: str) -> Edit:
        if self.read_only:
            raise BackendError("Read-only mode: policy editing is disabled.")
        self.require_bcachefs(path)
        value = validate(option, value)
        if option not in self.supported_options():
            raise BackendError("Your bcachefs CLI does not expose this inode option.")
        st = path.lstat()
        if not (stat.S_ISDIR(st.st_mode) or stat.S_ISREG(st.st_mode)):
            raise BackendError("Select a regular file or directory; symlinks cannot be edited.")
        return Edit(path, option, value, (st.st_dev, st.st_ino),
                    self.policy(path).explicit.get(option), stat.S_ISDIR(st.st_mode))

    def apply(self, edit: Edit) -> str:
        # Revalidate after the confirmation screen; do not write to a replaced path.
        current = self.prepare(edit.path, edit.option, edit.value)
        if current.inode != edit.inode or current.previous != edit.previous:
            raise BackendError("Path or policy changed since preview. Review the change again.")
        _, warning = self.command("set-file-option", f"--{edit.option}={edit.value}",
                                  "--", str(edit.path), timeout=600)
        after = self.policy(edit.path)
        if edit.value == "-":
            verified = edit.option not in after.explicit
        else:
            verified = edit.option in after.explicit
        if not verified or after.errors:
            raise BackendError("Command completed, but the root attribute could not be verified. "
                               "The change may have partially applied; refresh before retrying.")
        effective = after.effective.get(edit.option, "unknown")
        message = f"Root policy read back: {edit.option} = {effective}."
        if edit.directory:
            message += " Recursive propagation requested; descendants were not individually audited."
        message += " Existing extent conversion runs separately in bcachefs."
        if warning:
            message += f"\nCLI warning (possible partial propagation): {warning.strip()}"
        return message

    def inspect_extents(self, path: Path) -> str:
        self.require_bcachefs(path)
        before = path.lstat()
        if not stat.S_ISREG(before.st_mode):
            raise BackendError("Extent inspection is for a selected regular file.")
        help_text, _ = self.command("list", "--help")
        if "kernel" not in help_text or "mounted" not in help_text:
            raise BackendError("This CLI does not advertise online kernel extent inspection. "
                               "Install tools with BCH_IOCTL_QUERY_BTREE_KEYS support.")
        subvol = subvolume_id(path)
        if subvol is None:
            raise BackendError("Kernel does not report STATX_SUBVOL; refusing an ambiguous snapshot query.")
        tree_text, _ = self.command("subvolume", "list-snapshots", "--json", str(path.parent))
        try:
            tree = json.loads(tree_text)
            snapshots = [n["id"] for n in tree["nodes"] if n.get("subvol") == subvol]
        except (ValueError, KeyError, TypeError) as exc:
            raise BackendError("Could not decode the subvolume/snapshot mapping.") from exc
        if len(snapshots) != 1 or not isinstance(snapshots[0], int) or snapshots[0] <= 0:
            raise BackendError("Cannot identify this file's snapshot unambiguously.")
        snapshot = snapshots[0]
        text, warning = self.command("list", "-b", "extents", "-s",
                                     f"{before.st_ino}:0:{snapshot}", "-e",
                                     f"{before.st_ino}:18446744073709551615:{snapshot}",
                                     "--colorize", "false", str(self.mount.path))
        after = path.lstat()
        if (before.st_ino, before.st_dev, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                after.st_ino, after.st_dev, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise BackendError("File changed during inspection. Refresh and retry.")
        durability = [int(v) for v in re.findall(r"durability:\s*(\d+)", text)]
        algorithms = sorted(set(re.findall(r"\bcompress\s+(\w+)", text)))
        ec = len(re.findall(r"\bstripe_ptr:", text))
        header = [f"Snapshot: {snapshot} • inode: {before.st_ino}",
                  f"Observed minimum reported durability: {min(durability) if durability else 'unknown'}",
                  f"Observed compression: {', '.join(algorithms) or 'not reported'}",
                  f"EC stripe references: {ec}",
                  "Durability annotations are not a scrub or a live device-health guarantee.",
                  "c_size / size below are stored / uncompressed sectors (512 B).",
                  "Shared/partial compression blobs and shared EC parity prevent an exact",
                  "exclusive per-file physical total. Directory policies are separate."]
        if warning:
            header.append(warning)
        return "\n".join(header) + "\n\n" + (text or "No extent keys returned (empty/sparse file).")
