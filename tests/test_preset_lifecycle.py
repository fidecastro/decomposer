"""Preset persistence, startup restoration, and safe file handling."""

import json
import os

import pytest

from opal_c1.daemon import Daemon, PRESET_JSON_MAX, preset_dir, preset_state_file


@pytest.fixture
def daemon(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    instance = Daemon()
    instance._snapshot = {"controls": {"brightness": 111, "iso": 800}}
    return instance


def test_save_becomes_active_and_restores_on_next_launch(daemon):
    daemon.state.look = "noir"
    daemon.state.strength = 0.42
    daemon.state.zoom = 1.75
    saved = daemon.save_preset("Desk")

    assert saved["active_preset"] == "Desk"
    assert saved["presets"] == ["Desk"]
    assert (os.stat(preset_state_file()).st_mode & 0o777) == 0o600

    restarted = Daemon()
    restarted._restore_startup_preset("call")
    assert restarted.state.active_preset == "Desk"
    assert restarted.state.look == "noir"
    assert restarted.state.strength == 0.42
    assert restarted.state.zoom == 1.75
    assert restarted._sticky == {"brightness": 111, "iso": 800}


def test_saving_selected_preset_updates_it(daemon):
    daemon.state.zoom = 1.25
    daemon.save_preset("Desk")
    daemon.state.zoom = 2.5
    daemon.save_preset("Desk")

    raw = json.loads((preset_dir() / "call/Desk.json").read_text())
    assert raw["zoom"] == 2.5
    assert daemon.list_presets()[0]["name"] == "Desk"


def test_delete_returns_full_status_and_clears_last_selection(daemon):
    daemon.save_preset("Desk")
    result = daemon.delete_preset("Desk")

    assert result["deleted"] == "Desk"
    assert result["active_preset"] is None
    assert result["presets"] == []
    state = json.loads(preset_state_file().read_text())
    assert state["last_by_mode"] == {}


def test_linked_preset_is_neither_loaded_nor_deleted(daemon, tmp_path):
    target = tmp_path / "outside.json"
    target.write_text('{"mode": "call", "look": "noir"}')
    mode_dir = preset_dir() / "call"
    mode_dir.mkdir(parents=True)
    (mode_dir / "Linked.json").symlink_to(target)

    with pytest.raises(ValueError, match="safely read"):
        daemon.load_preset("Linked")
    with pytest.raises(ValueError, match="unsafe preset"):
        daemon.delete_preset("Linked")
    assert target.exists()


def test_oversized_preset_is_rejected_before_json_parse(daemon):
    mode_dir = preset_dir() / "call"
    mode_dir.mkdir(parents=True)
    path = mode_dir / "Huge.json"
    path.write_bytes(b"{" + b" " * PRESET_JSON_MAX + b"}")

    with pytest.raises(ValueError, match="larger than"):
        daemon.load_preset("Huge")
    assert daemon.list_presets() == []
