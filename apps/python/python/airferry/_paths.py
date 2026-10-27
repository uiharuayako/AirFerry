"""Validate filesystem locality before looking up caller-controlled paths.

Linux reads its kernel mount registry. Darwin reads cached mount flags with
MNT_NOWAIT, which does not refresh remote filesystem statistics. Neither path
uses statfs(target), a mount subprocess, or a network API. As with ordinary
filesystem validation, concurrent mount/symlink changes require OS isolation.
"""

from __future__ import annotations

import ctypes
import os
import re
import stat
import sys
import tempfile
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

# Unknown filesystems, including arbitrary FUSE providers, fail closed. A local
# block device is treated as local according to the OS filesystem registry.
_LOCAL_LINUX_FILESYSTEMS = frozenset(
    {
        "ext2",
        "ext3",
        "ext4",
        "xfs",
        "btrfs",
        "f2fs",
        "jfs",
        "reiserfs",
        "reiser4",
        "nilfs2",
        "zfs",
        "bcachefs",
        "tmpfs",
        "ramfs",
        "rootfs",
        "overlay",
        "overlayfs",
        "aufs",
        "vfat",
        "msdos",
        "exfat",
        "ntfs",
        "ntfs3",
        "udf",
        "iso9660",
        "erofs",
        "squashfs",
        "devtmpfs",
    }
)
_MNT_NOWAIT = 2
_MNT_LOCAL = 0x00001000


@dataclass(frozen=True)
class _Mount:
    point: PurePosixPath
    filesystem: str
    local: bool


def _unescape_mount_field(value: str) -> str:
    return re.sub(r"\\([0-7]{3})", lambda match: chr(int(match[1], 8)), value)


def _parse_mountinfo(text: str) -> tuple[_Mount, ...]:
    """Parse /proc/self/mountinfo without resolving any mounted path.

    Field layout: https://docs.kernel.org/filesystems/proc.html
    """
    mounts = []
    for line in text.splitlines():
        if not line.strip():
            continue
        before, separator, after = line.partition(" - ")
        fields, extra = before.split(), after.split()
        if not separator or len(fields) < 6 or len(extra) < 3:
            raise ValueError(
                "Cannot verify local storage: invalid Linux mount registry"
            )
        point = PurePosixPath(_unescape_mount_field(fields[4]))
        if not point.is_absolute():
            raise ValueError("Cannot verify local storage: non-absolute mount point")
        filesystem = extra[0].lower()
        source = _unescape_mount_field(extra[1])
        local = filesystem in _LOCAL_LINUX_FILESYSTEMS
        # fuseblk is commonly local NTFS. Only its actual block-device variant
        # is allowed; sshfs, rclone, bindfs, and unclassified providers are not.
        if filesystem == "fuseblk" and source.startswith("/dev/"):
            local = True
        mounts.append(_Mount(point, filesystem, local))
    if not any(mount.point == PurePosixPath("/") for mount in mounts):
        raise ValueError("Cannot verify local storage: root mount is missing")
    return tuple(mounts)


class _DarwinStatfs64(ctypes.Structure):
    # The public 64-bit Darwin ABI, shared by x86_64 and arm64:
    # https://github.com/apple-oss-distributions/xnu/blob/main/bsd/sys/mount.h
    _fields_ = [
        ("f_bsize", ctypes.c_uint32),
        ("f_iosize", ctypes.c_int32),
        ("f_blocks", ctypes.c_uint64),
        ("f_bfree", ctypes.c_uint64),
        ("f_bavail", ctypes.c_uint64),
        ("f_files", ctypes.c_uint64),
        ("f_ffree", ctypes.c_uint64),
        ("f_fsid", ctypes.c_int32 * 2),
        ("f_owner", ctypes.c_uint32),
        ("f_type", ctypes.c_uint32),
        ("f_flags", ctypes.c_uint32),
        ("f_fssubtype", ctypes.c_uint32),
        ("f_fstypename", ctypes.c_char * 16),
        ("f_mntonname", ctypes.c_char * 1024),
        ("f_mntfromname", ctypes.c_char * 1024),
        ("f_flags_ext", ctypes.c_uint32),
        ("f_reserved", ctypes.c_uint32 * 7),
    ]


def _darwin_mount_records(records: Iterable[_DarwinStatfs64]) -> tuple[_Mount, ...]:
    mounts = []
    for record in records:
        point = PurePosixPath(os.fsdecode(bytes(record.f_mntonname)))
        filesystem = bytes(record.f_fstypename).decode("ascii", "strict").lower()
        if not point.is_absolute():
            raise ValueError("Cannot verify local storage: invalid Darwin mount point")
        local = bool(record.f_flags & _MNT_LOCAL) and filesystem != "autofs"
        mounts.append(_Mount(point, filesystem, local))
    if not any(mount.point == PurePosixPath("/") for mount in mounts):
        raise ValueError("Cannot verify local storage: root mount is missing")
    return tuple(mounts)


def _darwin_mounts() -> tuple[_Mount, ...]:
    # getfsstat uses a caller-owned buffer, avoiding getmntinfo's shared
    # mutable result. MNT_NOWAIT returns cached data without querying mounts.
    # https://developer.apple.com/library/archive/documentation/System/Conceptual/ManPages_iPhoneOS/man2/getfsstat.2.html
    try:
        # Select the public 64-bit-structure symbol, rather than the deprecated
        # getfsstat64 compatibility API. Apple's ABI maps differ by CPU:
        # xnu/libsyscall/Platforms/MacOSX/{arm64,x86_64}/syscall.map.
        symbol = {
            "arm64": "getfsstat",
            "aarch64": "getfsstat",
            "x86_64": "getfsstat$INODE64",
            "amd64": "getfsstat$INODE64",
        }.get(os.uname().machine.lower())
        if symbol is None or ctypes.sizeof(ctypes.c_void_p) != 8:
            raise ValueError("Cannot verify local storage on this Darwin architecture")
        libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
        getfsstat = getattr(libc, symbol)
        getfsstat.argtypes = [
            ctypes.POINTER(_DarwinStatfs64),
            ctypes.c_int,
            ctypes.c_int,
        ]
        getfsstat.restype = ctypes.c_int
        count = getfsstat(None, 0, _MNT_NOWAIT)
        if count <= 0 or count > 100_000:
            raise OSError("invalid mount count")
        # A small margin avoids silently omitting a newly added mount between
        # the count query and the second cached query. Retry if it fills up.
        for _ in range(3):
            capacity = count + 16
            records = (_DarwinStatfs64 * capacity)()
            actual = getfsstat(records, ctypes.sizeof(records), _MNT_NOWAIT)
            if actual < 0:
                raise OSError(ctypes.get_errno(), "getfsstat failed")
            if actual < capacity:
                return _darwin_mount_records(records[:actual])
            count = actual
    except (AttributeError, OSError, UnicodeError) as error:
        raise ValueError(
            "Cannot verify local storage using Darwin's cached mount registry"
        ) from error
    raise ValueError("Cannot verify local storage: mount registry changed repeatedly")


def _mount_table() -> tuple[_Mount, ...]:
    if sys.platform.startswith("linux"):
        try:
            text = Path("/proc/self/mountinfo").read_text(
                encoding="utf-8", errors="surrogateescape"
            )
        except OSError as error:
            raise ValueError(
                "Cannot verify local storage using /proc/self/mountinfo"
            ) from error
        return _parse_mountinfo(text)
    if sys.platform == "darwin":
        return _darwin_mounts()
    raise ValueError("Local filesystem verification supports Windows, Linux, and macOS")


def _require_local(path: PurePosixPath, mounts: tuple[_Mount, ...]) -> None:
    matches = [mount for mount in mounts if path.is_relative_to(mount.point)]
    if not matches:
        raise ValueError(f"Cannot verify local filesystem for {path}")
    deepest = max(len(mount.point.parts) for mount in matches)
    # Ambiguous stacked mounts also fail closed if one candidate is remote.
    for mount in matches:
        if len(mount.point.parts) == deepest and not mount.local:
            raise ValueError(
                f"AirFerry requires local storage; {path} uses unsupported or network filesystem {mount.filesystem}"
            )


def _resolve_posix(
    path: str,
    mounts: tuple[_Mount, ...],
    *,
    lstat: Callable = os.lstat,
    readlink: Callable = os.readlink,
) -> PurePosixPath:
    """Walk one component at a time, validating its mount before lstat."""
    parsed = PurePosixPath(path)
    if not parsed.is_absolute() or path.startswith("//"):
        raise ValueError("Expected a local absolute POSIX path")
    remaining = deque(parsed.parts[1:])
    resolved = PurePosixPath("/")
    _require_local(resolved, mounts)
    links = 0
    while remaining:
        component = remaining.popleft()
        if component == "..":
            resolved = resolved.parent
            continue
        candidate = resolved / component
        _require_local(candidate, mounts)
        try:
            info = lstat(str(candidate))
        except FileNotFoundError:
            # Output files need not exist. Continue lexical mount checks; do
            # not skip later lstat calls because '..' may return to an existing
            # parent containing a symlink.
            resolved = candidate
            continue
        if stat.S_ISLNK(info.st_mode):
            links += 1
            if links > 40:
                raise ValueError("Too many symbolic links in local path")
            target = os.fsdecode(readlink(str(candidate)))
            if target.startswith("//"):
                raise ValueError("AirFerry cannot use network symlink targets")
            parsed_target = PurePosixPath(target)
            if parsed_target.is_absolute():
                resolved = PurePosixPath("/")
                remaining.extendleft(reversed(parsed_target.parts[1:]))
            else:
                remaining.extendleft(reversed(parsed_target.parts))
        else:
            resolved = candidate
    _require_local(resolved, mounts)
    return resolved


def local_path(value: str | os.PathLike[str]) -> Path:
    """Return a validated local path; reject remote mounts before path lookup."""
    raw = os.fspath(value)
    if not isinstance(raw, str):
        raise TypeError("paths must be str or PathLike[str]")
    if raw.startswith(("\\\\", "//")) or re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", raw):
        raise ValueError(
            "AirFerry only accepts local filesystem paths, not URLs or network shares"
        )
    path = Path(raw).absolute()
    if os.name == "nt":
        get_drive_type = ctypes.windll.kernel32.GetDriveTypeW
        get_drive_type.argtypes = [ctypes.c_wchar_p]
        get_drive_type.restype = ctypes.c_uint
        if get_drive_type(path.anchor) == 4:
            raise ValueError("AirFerry cannot use mapped network drives")
        for part in [*reversed(path.parents), path]:
            try:
                attributes = part.lstat().st_file_attributes
            except FileNotFoundError:
                break
            if attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                raise ValueError(
                    f"AirFerry requires local paths without reparse points: {part}"
                )
        return path
    return Path(_resolve_posix(str(path), _mount_table()))


def local_temp_root() -> Path:
    """Validate temporary-directory candidates before tempfile probes them."""
    # Validate configured paths even if tempfile already cached another path.
    # Its candidate-list helper only builds strings; gettempdir would actually
    # try creating files and must run after these locality checks.
    for name in ("TMPDIR", "TEMP", "TMP"):
        if candidate := os.environ.get(name):
            local_path(candidate)
    if tempfile.tempdir is not None:
        return local_path(tempfile.tempdir)
    candidates = getattr(tempfile, "_candidate_tempdir_list", None)
    if candidates is None:
        raise ValueError(
            "Cannot verify temporary storage candidates on this Python runtime"
        )
    for candidate in candidates():
        local_path(candidate)
    return local_path(tempfile.gettempdir())
