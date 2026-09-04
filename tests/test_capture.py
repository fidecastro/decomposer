"""Still capture ownership and bounded subprocess behavior."""

import sys
import time
from pathlib import Path

import opal_c1.daemon as daemon_module
from opal_c1.daemon import Daemon, _run_bounded


class _LiveEngine:
    @staticmethod
    def alive():
        return True


def test_bounded_runner_kills_a_noisy_nonterminating_producer():
    started = time.monotonic()
    result = _run_bounded(
        [
            sys.executable,
            "-c",
            "import sys,time; "
            "sys.stderr.write('x' * 4096); sys.stderr.flush(); time.sleep(30)",
        ],
        timeout=0.1,
        cap=32,
    )
    assert time.monotonic() - started < 3
    assert result["code"] == -1
    assert result["stderr"] == "x" * 32


def test_daemon_finalizes_photo_when_panel_is_not_involved(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    daemon = Daemon()
    daemon._engine = _LiveEngine()
    temporary = tmp_path / ".part-test.png"
    final = tmp_path / "photo-test.png"
    temporary.touch()
    monkeypatch.setattr(
        daemon_module, "_photo_target", lambda: (str(temporary), final)
    )
    monkeypatch.setattr(
        daemon_module.shutil, "which", lambda name: f"/opt/tools/bin/{name}"
    )

    def capture(cmd, timeout, cap):
        # ffmpeg comes from PATH, as it does for the panel's recorder.
        assert cmd[0] == "/opt/tools/bin/ffmpeg"
        assert timeout == 15
        Path(cmd[-1]).write_bytes(b"complete png")
        return {"code": 0, "stderr": ""}

    monkeypatch.setattr(daemon_module, "_run_bounded", capture)

    result = daemon.handle({"cmd": "capture_photo"})
    assert result["ok"] is True
    assert result["saved"] == str(final)
    assert final.read_bytes() == b"complete png"
    assert not temporary.exists()


def test_failed_daemon_capture_removes_partial_file(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    daemon = Daemon()
    daemon._engine = _LiveEngine()
    temporary = tmp_path / ".part-test.png"
    final = tmp_path / "photo-test.png"
    temporary.touch()
    monkeypatch.setattr(
        daemon_module, "_photo_target", lambda: (str(temporary), final)
    )
    monkeypatch.setattr(daemon_module.shutil, "which", lambda name: "/usr/bin/ffmpeg")
    monkeypatch.setattr(
        daemon_module,
        "_run_bounded",
        lambda *_args, **_kwargs: {"code": 1, "stderr": "capture broke"},
    )

    result = daemon.handle({"cmd": "capture_photo"})
    assert result == {"ok": False, "error": "RuntimeError: capture broke"}
    assert not temporary.exists()
    assert not final.exists()


def test_missing_ffmpeg_is_a_clear_error_before_any_file_exists(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    daemon = Daemon()
    daemon._engine = _LiveEngine()
    monkeypatch.setattr(daemon_module.shutil, "which", lambda name: None)

    def no_target():
        raise AssertionError("no temporary file should be created")

    monkeypatch.setattr(daemon_module, "_photo_target", no_target)
    result = daemon.handle({"cmd": "capture_photo"})
    assert result["ok"] is False
    assert "ffmpeg" in result["error"]


def test_photos_land_in_the_xdg_pictures_directory(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "user-dirs.dirs").write_text(
        'XDG_DESKTOP_DIR="$HOME/Desktop"\nXDG_PICTURES_DIR="$HOME/Bilder"\n'
    )
    temporary, final = daemon_module._photo_target()
    assert final.parent == tmp_path / "Bilder" / "decomposer"
    assert Path(temporary).parent == final.parent
    Path(temporary).unlink()
