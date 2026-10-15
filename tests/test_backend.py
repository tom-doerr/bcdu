import errno
import sys
from pathlib import Path

import pytest

from bcdu.backend import Backend, BackendError, mount_for, run, validate
from bcdu.model import Policy


def test_mounts_use_path_boundaries_and_decode_spaces():
    info = "1 0 0:1 / / rw - ext4 dev rw\n2 1 0:2 / /pool rw - bcachefs dev rw\n"
    info += "3 1 0:3 / /space\\040here rw - bcachefs dev rw\n"
    assert mount_for(Path("/pool/sub"), info).fs_type == "bcachefs"
    assert mount_for(Path("/pool-other"), info).fs_type == "ext4"
    assert mount_for(Path("/space here/sub"), info).path == Path("/space here")


@pytest.mark.parametrize("key,value", [("compression", "zstd:16"), ("compression", "lz4:2"),
                                       ("data_replicas", "0"), ("erasure_code", "yes"),
                                       ("foreground_target", "$(echo nope)"),
                                       ("unknown", "1")])
def test_reject_invalid_policy(key, value):
    with pytest.raises(BackendError):
        validate(key, value)


def test_runner_limits_output_and_reports_errors():
    with pytest.raises(BackendError, match="limit"):
        run([sys.executable, "-c", "print('x' * 2000)"], limit=100)
    with pytest.raises(BackendError, match="specific error"):
        run([sys.executable, "-c", "import sys; sys.exit('specific error')"])
    with pytest.raises(BackendError, match="timed out"):
        run([sys.executable, "-c", "import time; time.sleep(5)"], timeout=0.05)


def test_apply_rechecks_path_and_uses_argument_array(tmp_path, monkeypatch):
    path = tmp_path / "name with spaces; touch nope"
    path.touch()
    backend = Backend(tmp_path, binary="bcachefs")
    monkeypatch.setattr(backend, "require_bcachefs", lambda _: None)
    monkeypatch.setattr(backend, "supported_options", lambda: {"compression"})
    policy = Policy({"compression": "lz4"}, {})
    monkeypatch.setattr(backend, "policy", lambda _: policy)
    edit = backend.prepare(path, "compression", "zstd:3")
    calls = []

    def command(*args, **kwargs):
        calls.append(args)
        policy.explicit["compression"] = "zstd:3"
        policy.effective["compression"] = "zstd:3"
        return "", ""

    monkeypatch.setattr(backend, "command", command)
    assert "zstd:3" in backend.apply(edit)
    assert calls == [("set-file-option", "--compression=zstd:3", "--", str(path))]
    with pytest.raises(BackendError, match="changed since preview"):
        backend.apply(edit)
    backend.read_only = True
    with pytest.raises(BackendError, match="Read-only"):
        backend.prepare(path, "compression", "lz4")


def test_successful_exit_does_not_prove_policy_changed(tmp_path, monkeypatch):
    path = tmp_path / "file"
    path.touch()
    backend = Backend(tmp_path, binary="bcachefs")
    monkeypatch.setattr(backend, "require_bcachefs", lambda _: None)
    monkeypatch.setattr(backend, "supported_options", lambda: {"compression"})
    monkeypatch.setattr(backend, "policy", lambda _: Policy({"compression": "lz4"},
                                                           {"compression": "lz4"}))
    monkeypatch.setattr(backend, "command", lambda *a, **kw: ("", ""))
    edit = backend.prepare(path, "compression", "zstd:3")
    with pytest.raises(BackendError, match="could not be verified"):
        backend.apply(edit)


def test_unset_effective_attributes_use_filesystem_defaults(tmp_path, monkeypatch):
    from bcdu.backend import Mount

    path = tmp_path / "file"
    path.touch()
    backend = Backend(tmp_path)
    backend.mount = Mount(tmp_path, "bcachefs")

    def attribute(path, key, **kwargs):
        if key == "bcachefs_effective.compression":
            return b"zstd:3"
        raise OSError(errno.ENODATA, "unset")

    monkeypatch.setattr("bcdu.backend.os.getxattr", attribute)
    monkeypatch.setattr(backend, "filesystem_defaults", lambda: {"compression": "lz4",
                                                               "data_replicas": "3"})
    policy = backend.policy(path)
    assert policy.effective == {"compression": "zstd:3", "data_replicas": "3"}
    assert policy.from_filesystem == {"data_replicas"}
    assert policy.explicit == {}
