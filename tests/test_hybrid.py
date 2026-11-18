"""Call mode through opal-controller ("hybrid").

The controller boots the camera's own firmware into RAM, which adds a
control channel to Call mode. These tests pin the domain facts (core), the
controller adapter against a fake process and a real Unix socket, and the
backend's routing of focus.
"""

import socket
import threading
from pathlib import Path

import pytest

from opal_c1.adapters import controller_proc
from opal_c1.adapters.controller_proc import (
    HybridBackend,
    HybridConfig,
    HybridController,
    hybrid_config,
)
from opal_c1.core import model
from opal_c1.core.model import Mode


# -- core -----------------------------------------------------------------

def test_opal_firmware_in_ram_is_call_mode():
    assert model.mode_for_identity("f63d", "0410") is Mode.CALL
    assert model.mode_for_identity("f63b", "0410") is Mode.CALL      # hybrid
    assert model.mode_for_identity("f63b", "0100") is Mode.STUDIO    # stock DepthAI
    assert model.mode_for_identity("f63b", "0110") is Mode.STUDIO
    assert model.mode_for_identity("f63c", "0100") is None           # bootloader


def test_hybrid_adds_focus_to_call_and_nothing_else():
    plain = model.controls_for(Mode.CALL)
    hybrid = model.controls_for(Mode.CALL, model.HYBRID_CALL_CONTROLS)
    assert "focus" not in plain
    assert hybrid - plain == {"focus"}
    # A backend cannot smuggle in controls the core has not vetted.
    assert model.controls_for(Mode.CALL, frozenset({"wb", "effect"})) == plain
    # Studio is unaffected.
    assert model.controls_for(Mode.STUDIO, model.HYBRID_CALL_CONTROLS) == model.controls_for(Mode.STUDIO)


def test_focus_refusal_in_plain_call_says_what_is_missing():
    assert model.refusal_reason(Mode.CALL, "focus", model.HYBRID_CALL_CONTROLS) is None
    why = model.refusal_reason(Mode.CALL, "focus")
    assert why and "opal-controller" in why
    assert "Studio" in model.refusal_reason(Mode.CALL, "wb")


def test_sticky_focus_replays_only_in_hybrid_call():
    sticky = {"focus": 140, "brightness": 5}
    assert model.sticky_for_mode(sticky, Mode.CALL) == {"brightness": 5}
    assert model.sticky_for_mode(sticky, Mode.CALL, model.HYBRID_CALL_CONTROLS) == sticky


# -- configuration ----------------------------------------------------------

def _install(tmp_path, detection=True):
    data = tmp_path / "share/opal-controller"
    data.mkdir(parents=True)
    (data / "firmware.mvcmd").write_bytes(b"fw")
    (data / "camTuning.bin").write_bytes(b"tuning")
    if detection:
        (data / "detection.blob").write_bytes(b"nn")
    binary = tmp_path / "opalctl"
    binary.write_text("#!/bin/sh\n")
    return {
        "XDG_DATA_HOME": str(tmp_path / "share"),
        "XDG_RUNTIME_DIR": str(tmp_path / "run"),
        "DECOMPOSER_OPALCTL": str(binary),
        "HOME": str(tmp_path),
    }


def test_hybrid_is_the_default_when_everything_is_installed(tmp_path):
    cfg = hybrid_config(_install(tmp_path))
    assert cfg is not None
    assert cfg.detection is not None
    assert cfg.socket == tmp_path / "run/decomposer/opal-controller.sock"


def test_hybrid_can_be_turned_off(tmp_path):
    env = _install(tmp_path)
    env["DECOMPOSER_HYBRID"] = "0"
    assert hybrid_config(env) is None


def test_no_hybrid_without_the_cameras_firmware(tmp_path):
    env = _install(tmp_path)
    (tmp_path / "share/opal-controller/firmware.mvcmd").unlink()
    assert hybrid_config(env) is None


def test_detection_network_is_optional(tmp_path):
    assert hybrid_config(_install(tmp_path, detection=False)).detection is None


# -- the controller adapter -------------------------------------------------

class FakeService:
    """Stands in for `opalctl serve`: a real Unix socket, scripted states."""

    def __init__(self, path: Path, states):
        self.states = list(states)   # one per status request; last repeats
        self.seen = []
        self.exited = None
        path.parent.mkdir(parents=True, exist_ok=True)
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(str(path))
        self.server.listen(8)
        self.server.settimeout(0.05)
        self._stop = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        while not self._stop.is_set():
            try:
                conn, _ = self.server.accept()
            except OSError:
                continue
            line = conn.recv(256).decode().strip()
            self.seen.append(line)
            if line == "status":
                state = self.states.pop(0) if len(self.states) > 1 else self.states[0]
                reply = f"ok {state}"
            elif line == "quit":
                reply, self.exited = "ok", 0
            elif line == "focus 999":
                reply = "error focus takes 0..255"
            else:
                reply = "ok"
            conn.sendall(reply.encode() + b"\n")
            conn.close()

    # the Popen surface the adapter uses
    def poll(self):
        return self.exited

    @property
    def returncode(self):
        return self.exited

    def wait(self, timeout=None):
        return self.exited

    def terminate(self):
        self.exited = -15

    def kill(self):
        self.exited = -9

    def close(self):
        self._stop.set()
        self.thread.join(timeout=1)
        self.server.close()


@pytest.fixture
def rig(tmp_path):
    config = HybridConfig(
        binary="/opt/opalctl", firmware=tmp_path / "fw", tuning=tmp_path / "tun",
        detection=tmp_path / "nn", socket=tmp_path / "run/ctl.sock",
    )
    made = {}

    def build(states):
        def spawn(argv, **_kw):
            made["argv"] = argv
            made["service"] = FakeService(config.socket, states)
            return made["service"]

        return HybridController(config, spawn=spawn, sleep=lambda _s: None)

    yield build, made
    if "service" in made:
        made["service"].close()


def test_start_waits_for_ready_and_passes_the_graph_options(rig):
    build, made = rig
    controller = build(["waiting", "settling", "settling", "ready"])
    controller.start(timeout=5)
    assert controller.ready()
    argv = made["argv"]
    assert argv[:2] == ["/opt/opalctl", "serve"]
    assert "--mic-nodes" in argv and "--detection-blob" in argv
    assert made["service"].seen.count("status") >= 4


def test_start_fails_cleanly_when_the_controller_exits(rig):
    build, made = rig
    controller = build(["waiting"])

    def spawn_then_die(argv, **_kw):
        made["service"] = FakeService(controller.config.socket, ["waiting"])
        made["service"].exited = 4
        return made["service"]

    controller._spawn = spawn_then_die
    with pytest.raises(RuntimeError, match="exited during start"):
        controller.start(timeout=5)
    assert not controller.alive()


def test_start_times_out_and_stops_the_controller(rig):
    build, made = rig
    ticks = iter(range(0, 10_000, 30))
    controller = build(["settling"])
    controller._clock = lambda: next(ticks)
    with pytest.raises(RuntimeError, match="did not become ready"):
        controller.start(timeout=100)
    assert "quit" in made["service"].seen
    assert not controller.alive()


def test_stop_asks_the_controller_to_quit(rig):
    build, made = rig
    controller = build(["ready"])
    controller.start(timeout=5)
    controller.stop()
    assert made["service"].seen[-1] == "quit"
    assert not controller.alive()
    controller.stop()  # idempotent


def test_commands_and_refusals(rig):
    build, made = rig
    controller = build(["ready"])
    controller.start(timeout=5)
    controller.set_focus(140)
    controller.set_autofocus()
    assert made["service"].seen[-2:] == ["focus 140", "autofocus"]
    with pytest.raises(ValueError, match="0..255"):
        controller.set_focus(999)


def test_a_silent_controller_is_not_ready(tmp_path):
    config = HybridConfig("/opt/opalctl", tmp_path / "fw", tmp_path / "tun", None, tmp_path / "nobody.sock")
    controller = HybridController(config)
    assert controller.state() is None
    assert not controller.ready()


# -- the backend ------------------------------------------------------------

class RecordingController:
    def __init__(self, fail=None):
        self.calls, self.fail = [], fail

    def set_focus(self, v):
        if self.fail:
            raise self.fail
        self.calls.append(("focus", v))

    def set_autofocus(self):
        self.calls.append(("autofocus",))


def test_backend_is_call_mode_with_focus(monkeypatch):
    backend = HybridBackend(RecordingController(), node_resolver=lambda: "/dev/video0")
    assert backend.mode is Mode.CALL
    assert backend.extra_controls == model.HYBRID_CALL_CONTROLS


def test_focus_goes_to_the_controller_and_minus_one_means_auto(monkeypatch):
    monkeypatch.setattr(controller_proc.UvcBackend, "apply_controls", lambda self, values: ({}, {}))
    controller = RecordingController()
    backend = HybridBackend(controller, node_resolver=lambda: "/dev/video0")
    assert backend.apply_controls({"focus": 140}) == ({"focus": 140}, {})
    assert backend.apply_controls({"focus": -1}) == ({"focus": -1}, {})
    assert controller.calls == [("focus", 140), ("autofocus",)]


def test_a_controller_failure_is_a_refusal_not_a_crash(monkeypatch):
    monkeypatch.setattr(controller_proc.UvcBackend, "apply_controls", lambda self, values: ({"brightness": 3}, {}))
    backend = HybridBackend(RecordingController(fail=OSError("not answering")), node_resolver=lambda: "/dev/video0")
    applied, refused = backend.apply_controls({"brightness": 3, "focus": 140})
    assert applied == {"brightness": 3}
    assert "not answering" in refused["focus"]
