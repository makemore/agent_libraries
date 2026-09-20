#!/usr/bin/env python3
"""Seed narrow Mautic proxy trust on the first installation only.

Caller contract: run prepare-disk.py --check successfully first, hold the
maintenance and exclusive data locks, and keep the shared stack stopped.
This helper neither mounts storage nor creates/repairs the config directory.

Mautic 6's ConfigAwareTrait/ParameterLoader reads parameters_local.php before
the installer. This approved override trusts only Caddy's dedicated proxy address;
no env setting, PHP evaluation, local.php edit or existing-setting migration
is performed. Any non-identical override requires manual review.
"""

import argparse
from contextlib import contextmanager
import errno
import os
from pathlib import Path
import secrets
import stat
import sys


CONFIG = Path("/srv/business-tools/mautic/config")
FILENAME = "parameters_local.php"
CONTENT = b"<?php\n$parameters = ['trusted_proxies' => ['172.30.251.2']];\n"
ERROR = "Mautic proxy preparation refused; manual review required."


def require(condition):
    if not condition:
        raise ValueError(ERROR)


def require_owner(info):
    require(info.st_uid == 33 and info.st_gid == 33)


@contextmanager
def config_directory(directory):
    """Open every ancestor as a real directory, without resolving symlinks."""
    directory = Path(directory)
    require(directory.is_absolute() and ".." not in directory.parts)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptor = os.open(directory.anchor, flags)
    try:
        for component in directory.parts[1:]:
            child = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        info = os.fstat(descriptor)
        require_owner(info)
        require(stat.S_IMODE(info.st_mode) == 0o700)
        yield descriptor
    finally:
        os.close(descriptor)


def require_file(info):
    require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1)
    require_owner(info)
    require(stat.S_IMODE(info.st_mode) == 0o600)


def validate_existing(directory):
    """Compare bounded bytes, never parse/evaluate PHP or inspect local.php."""
    try:
        info = os.stat(FILENAME, dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        return False
    require_file(info)
    descriptor = os.open(
        FILENAME, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
    )
    with os.fdopen(descriptor, "rb") as stream:
        opened = os.fstat(stream.fileno())
        require_file(opened)
        require((opened.st_dev, opened.st_ino) == (info.st_dev, info.st_ino))
        require(stream.read(len(CONTENT) + 1) == CONTENT)
    return True


def sync_directory(descriptor):
    try:
        os.fsync(descriptor)
    except OSError as error:
        # Some filesystems do not implement directory fsync; real I/O errors
        # still fail closed. The production ext4 mount supports this operation.
        if error.errno not in (errno.EINVAL, errno.ENOTSUP):
            raise


def prepare(directory=CONFIG, check_only=False):
    """Validate an exact seed, or publish it without replacing any existing name."""
    with config_directory(directory) as parent:
        if validate_existing(parent):
            return
        require(not check_only)
        require(not os.listdir(parent))
        temporary = ".parameters-local-" + secrets.token_hex(16)
        descriptor = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600, dir_fd=parent,
        )
        try:
            with os.fdopen(descriptor, "wb") as stream:
                os.fchown(stream.fileno(), 33, 33)
                os.fchmod(stream.fileno(), 0o600)
                stream.write(CONTENT)
                stream.flush()
                os.fsync(stream.fileno())
            # Defend against unexpected arrivals as well as refusing overwrite
            # atomically below. Callers must still exclude concurrent app writes.
            require(os.listdir(parent) == [temporary])
            os.link(temporary, FILENAME, src_dir_fd=parent, dst_dir_fd=parent,
                    follow_symlinks=False)
        finally:
            os.unlink(temporary, dir_fd=parent)
            sync_directory(parent)
        require(validate_existing(parent))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    try:
        require(os.geteuid() == 0)
        prepare(check_only=args.check)
    except Exception:
        # PHP configuration and exception details may contain credentials.
        print(ERROR, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())