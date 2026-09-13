"""Bounded live-adjustment undo at the daemon IPC boundary."""

from opal_c1.core import model
from opal_c1.core.model import Mode
from opal_c1.daemon import Daemon


class _Backend:
    """Records every control request, so a test can say exactly what an undo
    sent to the camera - including the requests it must not send."""

    def __init__(self, mode=Mode.CALL):
        self.mode = mode
        self.calls: list = []

    def apply_controls(self, values):
        self.calls.append(dict(values))
        return dict(values), {}

    def read_controls(self):
        return {}


def _daemon(tmp_path, monkeypatch, mode=Mode.CALL, readback=None):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    daemon = Daemon()
    daemon.state.mode = mode.value
    daemon._backend = _Backend(mode)
    # The poller's hardware readback: under auto-exposure it always carries
    # the exposure and gain the ISP is currently using.
    daemon._snapshot["controls"] = dict(
        readback if readback is not None
        else {"brightness": 90, "exposure": 12000, "iso": 400}
    )
    return daemon


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


def test_undo_of_a_look_change_never_touches_the_camera(tmp_path, monkeypatch):
    # The reported bug: undoing `look chrome` re-sent the whole readback,
    # and exposure/iso in it switched auto-exposure to Manual Mode.
    daemon = _daemon(tmp_path, monkeypatch)
    daemon.handle({"cmd": "set_look", "look": "chrome"})
    daemon.handle({"cmd": "undo"})
    daemon.handle({"cmd": "redo"})
    assert daemon._backend.calls == []
    assert daemon.state.look == "chrome"


def test_undo_of_manual_exposure_returns_the_camera_to_auto(tmp_path, monkeypatch):
    daemon = _daemon(tmp_path, monkeypatch)
    daemon.handle({"cmd": "set_camera", "values": {"exposure": 5000}})
    assert daemon._backend.calls == [{"exposure": 5000}]
    assert daemon._sticky == {"exposure": 5000}

    undone = daemon.handle({"cmd": "undo"})
    assert undone["undone"] == "exposure"
    # Not the readback value 12000, which would pin the camera to manual:
    # the user had never set exposure, so it goes back to automatic.
    assert daemon._backend.calls[-1] == {"exposure": -1}
    assert "exposure" not in daemon._sticky
    assert "notes" not in undone

    redone = daemon.handle({"cmd": "redo"})
    assert redone["redone"] == "exposure"
    assert daemon._backend.calls[-1] == {"exposure": 5000}
    assert daemon._sticky == {"exposure": 5000}


def test_undo_restores_only_the_control_the_request_touched(tmp_path, monkeypatch):
    daemon = _daemon(tmp_path, monkeypatch)
    daemon.handle({"cmd": "set_camera", "values": {"brightness": 140}})
    daemon.handle({"cmd": "undo"})
    # brightness has no automatic mode, so its previous readback is the
    # previous value; exposure and iso were not part of the request.
    assert daemon._backend.calls == [{"brightness": 140}, {"brightness": 90}]
    assert daemon.status()["controls"]["exposure"] == 12000


def test_undo_returns_to_the_users_earlier_request_when_there_was_one(
    tmp_path, monkeypatch
):
    daemon = _daemon(tmp_path, monkeypatch)
    daemon.handle({"cmd": "set_camera", "values": {"exposure": 5000}})
    daemon._undo_history[-1]["at"] = 0.0  # outside the coalescing window
    daemon.handle({"cmd": "set_camera", "values": {"exposure": 9000}})
    daemon.handle({"cmd": "undo"})
    assert daemon._backend.calls[-1] == {"exposure": 5000}
    assert daemon._sticky == {"exposure": 5000}
    daemon.handle({"cmd": "undo"})
    assert daemon._backend.calls[-1] == {"exposure": -1}
    assert daemon._sticky == {}


def test_studio_focus_undo_returns_to_autofocus(tmp_path, monkeypatch):
    daemon = _daemon(
        tmp_path, monkeypatch, mode=Mode.STUDIO,
        readback={"focus": 87, "wb": 4500, "exposure": 12000, "iso": 400},
    )
    daemon.handle({"cmd": "set_camera", "values": {"focus": 120}})
    undone = daemon.handle({"cmd": "undo"})
    assert daemon._backend.calls == [{"focus": 120}, {"focus": -1}]
    assert "focus" not in daemon._sticky
    # And status agrees with the hardware rather than with a stale copy.
    assert undone["controls"]["focus"] == -1


def test_preset_load_undo_restores_the_controls_it_applied(tmp_path, monkeypatch):
    daemon = _daemon(tmp_path, monkeypatch)
    daemon._snapshot["controls"] = {"brightness": 200, "exposure": 12000}
    daemon.save_preset("Bright")
    daemon._snapshot["controls"] = {"brightness": 90, "exposure": 12000}
    daemon._clear_undo()

    loaded = daemon.handle({"cmd": "preset_load", "name": "Bright"})
    assert loaded["applied"] == {"brightness": 200, "exposure": 12000}
    daemon.handle({"cmd": "undo"})
    # brightness back to its readback, exposure back to automatic; nothing
    # else, and nothing sent twice.
    assert daemon._backend.calls[-1] == {"brightness": 90, "exposure": -1}


def test_region_taps_and_repeated_values_leave_no_history(tmp_path, monkeypatch):
    daemon = _daemon(tmp_path, monkeypatch)
    daemon.handle({"cmd": "set_camera", "values": {"af_region": [10, 10, 50, 50]}})
    assert daemon.status()["can_undo"] is False
    daemon.handle({"cmd": "set_camera", "values": {"brightness": 140}})
    daemon._undo_history[-1]["at"] = 0.0
    daemon.handle({"cmd": "set_camera", "values": {"brightness": 140}})
    assert len(daemon._undo_history) == 1


def test_readback_drift_alone_does_not_create_history(tmp_path, monkeypatch):
    daemon = _daemon(tmp_path, monkeypatch)
    before = daemon._undo_snapshot()
    daemon._snapshot["controls"]["exposure"] = 13000  # the ISP hunting
    daemon._record_undo({"cmd": "set_zoom"}, before, daemon._undo_snapshot(), [])
    assert daemon.status()["can_undo"] is False


def test_restore_rule_prefers_intent_then_auto_then_readback():
    sticky = {"exposure": 5000, "effect": "sepia"}
    readback = {"brightness": 90, "exposure": 12000, "iso": 400}
    values, unknown = model.restore_values(
        ["exposure", "iso", "brightness", "effect", "scene", "wb", "hue"],
        sticky, readback,
    )
    assert values == {
        "exposure": 5000,   # the user's earlier request
        "iso": -1,          # never requested: back to automatic
        "brightness": 90,   # a plain slider: its previous readback
        "effect": "sepia",
        "scene": "off",
        "wb": -1,
    }
    assert unknown == ["hue"]
