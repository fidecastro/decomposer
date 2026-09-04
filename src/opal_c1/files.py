"""Owner-controlled files: bounded JSON reads, atomic writes, XDG user dirs.

The daemon and the panel each keep small private JSON documents (presets,
the last selection, panel preferences) under the user's config directory.
The rules are the same for both, so they live here once: never follow a
link, never read or write more than a bounded size, never replace something
that is not our own regular file, and publish a new version atomically.
"""

from __future__ import annotations

import json
import os
import secrets
import stat
from contextlib import suppress
from pathlib import Path

USER_DIRS_MAX = 64 * 1024


def read_regular_json(path: Path, maximum: int):
    """Read one bounded, owner-controlled regular file without following links."""
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as e:
        raise ValueError(f"cannot safely read {path}: {e.strerror}") from e
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise ValueError(f"refusing non-regular or foreign-owned file {path}")
        if info.st_size > maximum:
            raise ValueError(f"{path} is larger than {maximum} bytes")
        chunks, retained = [], 0
        while True:
            chunk = os.read(fd, min(16 * 1024, maximum + 1 - retained))
            if not chunk:
                break
            chunks.append(chunk)
            retained += len(chunk)
            if retained > maximum:
                raise ValueError(f"{path} is larger than {maximum} bytes")
        return json.loads(b"".join(chunks).decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as e:
        raise ValueError(f"invalid JSON in {path}: {e}") from e
    finally:
        os.close(fd)


def atomic_write_json(path: Path, value, maximum: int) -> None:
    """Publish a private JSON file atomically within a pinned directory."""
    payload = (json.dumps(value, indent=2) + "\n").encode("utf-8")
    if len(payload) > maximum:
        raise ValueError(f"JSON for {path} is larger than {maximum} bytes")
    path.parent.mkdir(parents=True, exist_ok=True)
    dir_flags = os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_DIRECTORY", 0)
    dir_fd = os.open(path.parent, dir_flags)
    temporary = f".{path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
    fd = None
    try:
        try:
            current = os.stat(path.name, dir_fd=dir_fd, follow_symlinks=False)
        except FileNotFoundError:
            current = None
        if current is not None and (
            not stat.S_ISREG(current.st_mode) or current.st_uid != os.getuid()
        ):
            raise ValueError(f"refusing to replace unsafe file {path}")
        fd = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
            0o600,
            dir_fd=dir_fd,
        )
        view = memoryview(payload)
        while view:
            written = os.write(fd, view)
            view = view[written:]
        os.fsync(fd)
        os.close(fd)
        fd = None
        os.replace(temporary, path.name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        os.fsync(dir_fd)
    finally:
        if fd is not None:
            os.close(fd)
        with suppress(FileNotFoundError):
            os.unlink(temporary, dir_fd=dir_fd)
        os.close(dir_fd)


def unlink_regular(path: Path) -> None:
    """Delete only an owned regular entry from a pinned directory."""
    dir_flags = os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_DIRECTORY", 0)
    dir_fd = os.open(path.parent, dir_flags)
    try:
        info = os.stat(path.name, dir_fd=dir_fd, follow_symlinks=False)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise ValueError(f"refusing to delete unsafe file {path}")
        os.unlink(path.name, dir_fd=dir_fd)
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def xdg_user_dir(name: str, fallback: str) -> Path:
    """The XDG user directory called `name` ("PICTURES", "VIDEOS", ...).

    Read from user-dirs.dirs the way GLib does, so a daemon without GLib
    lands captures where the desktop's own apps put theirs. A missing or
    unusable entry falls back to ~/<fallback>.
    """
    home = Path.home()
    config = Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config")
    wanted = f"XDG_{name.upper()}_DIR"
    try:
        listing = config / "user-dirs.dirs"
        if listing.stat().st_size <= USER_DIRS_MAX:
            for line in listing.read_text("utf-8", "replace").splitlines():
                key, sep, value = line.strip().partition("=")
                if not sep or key.strip() != wanted:
                    continue
                value = value.strip().strip('"')
                # The format allows exactly two shapes: $HOME-relative and
                # absolute. Anything else is ignored rather than guessed at.
                if value.startswith("$HOME/"):
                    return home / value[len("$HOME/"):]
                if value.startswith("/"):
                    return Path(value)
    except (OSError, ValueError):
        pass
    return home / fallback
