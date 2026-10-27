"""Mount-policy tests use synthetic kernel tables and never mount a share."""

import stat
from pathlib import PurePosixPath
from types import SimpleNamespace

import pytest
from airferry import _paths


def _table(remote_type="nfs"):
    return _paths._parse_mountinfo(
        "1 0 8:1 / / rw - ext4 /dev/root rw\n"
        f"2 1 0:2 / /mnt/remote rw - {remote_type} server:/data rw\n"
        "3 1 0:3 / /tmp rw - tmpfs tmpfs rw\n"
        "4 1 0:4 / /mnt/with\\040space rw - cifs //server/share rw\n"
    )


@pytest.mark.parametrize(
    "filesystem",
    [
        "nfs",
        "nfs4",
        "cifs",
        "smb3",
        "fuse.sshfs",
        "fuse.rclone",
        "autofs",
        "9p",
        "unknown",
    ],
)
def test_remote_or_unknown_mount_rejected_before_target_metadata(filesystem):
    touched = []

    def lstat(path):
        touched.append(path)
        return SimpleNamespace(st_mode=stat.S_IFDIR)

    with pytest.raises(ValueError, match="filesystem"):
        _paths._resolve_posix("/mnt/remote/file.bin", _table(filesystem), lstat=lstat)
    assert "/mnt/remote" not in touched
    assert "/mnt/remote/file.bin" not in touched


def test_mount_escaping_and_path_boundaries():
    mounts = _table()
    with pytest.raises(ValueError, match="cifs"):
        _paths._require_local(PurePosixPath("/mnt/with space/data"), mounts)
    _paths._require_local(PurePosixPath("/mnt/remote-looking/file"), mounts)
    _paths._require_local(PurePosixPath("/tmp/local"), mounts)


def test_symlink_remote_target_rejected_before_lookup():
    touched = []

    def lstat(path):
        touched.append(path)
        return SimpleNamespace(
            st_mode=stat.S_IFLNK if path == "/home/link" else stat.S_IFDIR
        )

    with pytest.raises(ValueError, match="nfs"):
        _paths._resolve_posix(
            "/home/link/file.bin",
            _table(),
            lstat=lstat,
            readlink=lambda _: "/mnt/remote",
        )
    assert "/mnt/remote" not in touched


def test_local_symlinks_and_parent_components_are_resolved():
    def lstat(path):
        if path == "/absent":
            raise FileNotFoundError(path)
        return SimpleNamespace(
            st_mode=stat.S_IFLNK if path == "/home/link" else stat.S_IFDIR
        )

    result = _paths._resolve_posix(
        "/absent/../home/link/new.mp4",
        _table(),
        lstat=lstat,
        readlink=lambda _: "../tmp",
    )
    assert result == PurePosixPath("/tmp/new.mp4")


def test_symlink_loop_is_bounded():
    with pytest.raises(ValueError, match="Too many"):
        _paths._resolve_posix(
            "/loop",
            _table(),
            lstat=lambda _: SimpleNamespace(st_mode=stat.S_IFLNK),
            readlink=lambda _: "/loop",
        )


def test_malformed_registry_fails_closed():
    with pytest.raises(ValueError):
        _paths._parse_mountinfo("invalid")
    with pytest.raises(ValueError, match="root mount"):
        _paths._parse_mountinfo("1 0 0:1 / /tmp rw - tmpfs tmpfs rw")


def test_darwin_uses_local_flags_and_rejects_autofs():
    records = []
    for point, filesystem, flags in [
        (b"/", b"apfs", _paths._MNT_LOCAL),
        (b"/Volumes/remote", b"smbfs", 0),
        (b"/net", b"autofs", _paths._MNT_LOCAL),
    ]:
        record = _paths._DarwinStatfs64()
        record.f_mntonname, record.f_fstypename, record.f_flags = (
            point,
            filesystem,
            flags,
        )
        records.append(record)
    mounts = _paths._darwin_mount_records(records)
    _paths._require_local(PurePosixPath("/Users/example/file"), mounts)
    with pytest.raises(ValueError, match="smbfs"):
        _paths._require_local(PurePosixPath("/Volumes/remote/file"), mounts)
    with pytest.raises(ValueError, match="autofs"):
        _paths._require_local(PurePosixPath("/net/host"), mounts)


@pytest.mark.parametrize(
    "machine,symbol",
    [("arm64", "getfsstat"), ("x86_64", "getfsstat$INODE64")],
)
def test_darwin_mount_query_only_requests_cached_information(monkeypatch, machine, symbol):
    calls = []

    class Getfsstat:
        def __call__(self, records, size, flags):
            calls.append((records is None, size, flags))
            assert flags == _paths._MNT_NOWAIT
            if records is not None:
                records[0].f_mntonname = b"/"
                records[0].f_fstypename = b"apfs"
                records[0].f_flags = _paths._MNT_LOCAL
                records[1].f_mntonname = b"/Volumes/network"
                records[1].f_fstypename = b"nfs"
            return 2

    def library(path, **kwargs):
        assert path == "/usr/lib/libSystem.B.dylib"
        return SimpleNamespace(**{symbol: Getfsstat()})

    monkeypatch.setattr(
        _paths.os, "uname", lambda: SimpleNamespace(machine=machine), raising=False
    )
    monkeypatch.setattr(_paths.ctypes, "CDLL", library)
    mounts = _paths._darwin_mounts()
    assert len(calls) == 2
    assert calls[0] == (True, 0, _paths._MNT_NOWAIT)
    assert mounts[0].local
    assert not mounts[1].local


def test_darwin_unknown_architecture_fails_before_loading_abi(monkeypatch):
    monkeypatch.setattr(
        _paths.os, "uname", lambda: SimpleNamespace(machine="unknown"), raising=False
    )
    monkeypatch.setattr(
        _paths.ctypes, "CDLL", lambda *_a, **_kw: pytest.fail("unknown ABI was used")
    )
    with pytest.raises(ValueError, match="architecture"):
        _paths._darwin_mounts()


@pytest.mark.parametrize("name", ["TMPDIR", "TEMP", "TMP"])
def test_remote_temp_setting_rejected_before_gettempdir(monkeypatch, name):
    monkeypatch.setenv(name, "//server/temporary")
    monkeypatch.setattr(
        _paths.tempfile,
        "gettempdir",
        lambda: pytest.fail("gettempdir probed before validation"),
    )
    with pytest.raises(ValueError, match="network shares"):
        _paths.local_temp_root()


def test_cached_remote_tempdir_rejected_without_probe(monkeypatch):
    for name in ("TMPDIR", "TEMP", "TMP"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(_paths.tempfile, "tempdir", "//server/temporary")
    monkeypatch.setattr(
        _paths.tempfile, "gettempdir", lambda: pytest.fail("gettempdir must not run")
    )
    with pytest.raises(ValueError, match="network shares"):
        _paths.local_temp_root()


def test_all_default_temp_candidates_checked_before_probe(monkeypatch):
    for name in ("TMPDIR", "TEMP", "TMP"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(_paths.tempfile, "tempdir", None)
    monkeypatch.setattr(
        _paths.tempfile, "_candidate_tempdir_list", lambda: ["//server/fallback"]
    )
    monkeypatch.setattr(
        _paths.tempfile, "gettempdir", lambda: pytest.fail("remote fallback was probed")
    )
    with pytest.raises(ValueError, match="network shares"):
        _paths.local_temp_root()


def test_real_local_path_and_temp_root_work(tmp_path):
    assert _paths.local_path(tmp_path / "new.mp4").name == "new.mp4"
    assert _paths.local_temp_root().is_dir()
