"""Bounded live-adjustment undo at the daemon IPC boundary."""

from opal_c1.core.model import Mode
from opal_c1.daemon import Daemon


def test_undo_restores_last_live_adjustment(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    daemon = Daemon()

    changed = daemon.handle({"cmd": "set_look", "look": "noir"})
    assert changed["can_undo"] is True
    assert changed["undo_label"] == "look"
    assert daemon.state.look == "noir"

    undone = daemon.handle({"cmd": "undo"})
    assert undone["undone"] == "look"
    assert undone["can_undo"] is False
    assert undone["can_redo"] is True
    assert undone["redo_label"] == "look"
    assert daemon.state.look == "none"

    redone = daemon.handle({"cmd": "redo"})
    assert redone["redone"] == "look"
    assert redone["can_redo"] is False
    assert redone["can_undo"] is True
    assert daemon.state.look == "noir"


def test_new_adjustment_after_undo_clears_redo(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    daemon = Daemon()

    daemon.handle({"cmd": "set_look", "look": "noir"})
    daemon.handle({"cmd": "undo"})
    assert daemon.status()["can_redo"] is True

    daemon.handle({"cmd": "set_zoom", "zoom": 1.5})
    status = daemon.status()
    assert status["can_redo"] is False
    assert status["redo_label"] is None


def test_history_barrier_clears_undo_and_redo(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    daemon = Daemon()
    daemon.save_preset("Barrier")

    daemon.handle({"cmd": "set_look", "look": "noir"})
    daemon.handle({"cmd": "undo"})
    assert daemon.status()["can_redo"] is True

    daemon.handle({"cmd": "preset_delete", "name": "Barrier"})
    status = daemon.status()
    assert status["can_undo"] is False
    assert status["can_redo"] is False


def test_slider_updates_coalesce_into_one_undo(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    daemon = Daemon()

    daemon.handle({"cmd": "set_zoom", "zoom": 1.25})
    daemon.handle({"cmd": "set_zoom", "zoom": 1.75})
    assert len(daemon._undo_history) == 1

    daemon.handle({"cmd": "undo"})
    assert daemon.state.zoom == 1.0
    assert daemon.status()["can_undo"] is False


def test_camera_undo_reapplies_previous_hardware_value(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    daemon = Daemon()

    class Backend:
        mode = Mode.CALL

        def __init__(self):
            self.values = {"brightness": 90}

        def apply_controls(self, values):
            self.values.update(values)
            return dict(values), {}

    backend = Backend()
    daemon._backend = backend
    daemon._snapshot["controls"] = {"brightness": 90}

    daemon.handle({
        "cmd": "set_camera", "values": {"brightness": 140},
    })
    assert backend.values["brightness"] == 140

    undone = daemon.handle({"cmd": "undo"})
    assert undone["undone"] == "brightness"
    assert backend.values["brightness"] == 90
    assert daemon.status()["controls"]["brightness"] == 90

    redone = daemon.handle({"cmd": "redo"})
    assert redone["redone"] == "brightness"
    assert backend.values["brightness"] == 140
    assert daemon.status()["controls"]["brightness"] == 140


def test_saving_does_not_erase_adjustment_undo_or_roll_back_selection(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    daemon = Daemon()
    daemon.handle({"cmd": "set_look", "look": "noir"})

    saved = daemon.handle({"cmd": "preset_save", "name": "Noir"})
    assert saved["active_preset"] == "Noir"
    assert saved["can_undo"] is True

    daemon.handle({"cmd": "undo"})
    assert daemon.state.look == "none"
    assert daemon.state.active_preset == "Noir"


def test_preset_load_undo_restores_values_and_previous_selection(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    daemon = Daemon()
    daemon.save_preset("Plain")
    daemon.state.look = "noir"
    daemon.save_preset("Noir")
    daemon.load_preset("Plain")
    daemon._clear_undo()

    daemon.handle({"cmd": "preset_load", "name": "Noir"})
    assert daemon.state.look == "noir"
    assert daemon.state.active_preset == "Noir"

    daemon.handle({"cmd": "undo"})
    assert daemon.state.look == "none"
    assert daemon.state.active_preset == "Plain"

    daemon.handle({"cmd": "redo"})
    assert daemon.state.look == "noir"
    assert daemon.state.active_preset == "Noir"


def test_preset_delete_clears_live_history(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    daemon = Daemon()
    daemon.save_preset("Noir")
    daemon.handle({"cmd": "set_look", "look": "noir"})
    assert daemon.status()["can_undo"] is True

    # Delete is not in live undo's scope, so it forms a history barrier rather
    # than leaving an Undo button that could imply the file will come back.
    deleted = daemon.handle({"cmd": "preset_delete", "name": "Noir"})
    assert deleted["can_undo"] is False
