"""The shared owner-controlled file helpers, used by daemon and panel alike."""

import os

import pytest

from opal_c1.files import (
    atomic_write_json,
    read_regular_json,
    unlink_regular,
    xdg_user_dir,
)


def test_atomic_write_then_read_round_trips_with_private_mode(tmp_path):
    path = tmp_path / "nested" / "panel.json"
    atomic_write_json(path, {"x": 1}, 1024)
    assert read_regular_json(path, 1024) == {"x": 1}
    assert os.stat(path).st_mode & 0o777 == 0o600
    assert [p.name for p in path.parent.iterdir()] == ["panel.json"]


def test_reads_refuse_links_and_oversize(tmp_path):
    target = tmp_path / "outside.json"
    target.write_text("{}")
    link = tmp_path / "link.json"
    link.symlink_to(target)
    with pytest.raises(ValueError, match="safely read"):
        read_regular_json(link, 1024)
    big = tmp_path / "big.json"
    big.write_bytes(b"[" + b" " * 64 + b"]")
    with pytest.raises(ValueError, match="larger than"):
        read_regular_json(big, 32)


def test_writes_refuse_to_replace_a_link_and_deletes_only_regular_files(tmp_path):
    target = tmp_path / "outside.json"
    target.write_text("{}")
    link = tmp_path / "link.json"
    link.symlink_to(target)
    with pytest.raises(ValueError, match="unsafe"):
        atomic_write_json(link, {"x": 1}, 1024)
    with pytest.raises(ValueError, match="unsafe"):
        unlink_regular(link)
    assert target.read_text() == "{}"
    real = tmp_path / "real.json"
    real.write_text("{}")
    unlink_regular(real)
    assert not real.exists()


def test_xdg_user_dir_reads_user_dirs_the_way_the_desktop_does(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    (tmp_path / "cfg").mkdir()
    (tmp_path / "cfg" / "user-dirs.dirs").write_text(
        "# comment\n"
        'XDG_PICTURES_DIR="$HOME/Photos"\n'
        'XDG_VIDEOS_DIR="/srv/media/video"\n'
        'XDG_MUSIC_DIR="relative/is/ignored"\n'
    )
    assert xdg_user_dir("PICTURES", "Pictures") == tmp_path / "Photos"
    assert xdg_user_dir("VIDEOS", "Videos").as_posix() == "/srv/media/video"
    assert xdg_user_dir("MUSIC", "Music") == tmp_path / "Music"
    assert xdg_user_dir("DOCUMENTS", "Documents") == tmp_path / "Documents"


def test_xdg_user_dir_falls_back_without_a_listing(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "missing"))
    assert xdg_user_dir("PICTURES", "Pictures") == tmp_path / "Pictures"
