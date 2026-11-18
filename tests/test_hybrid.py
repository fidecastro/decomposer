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


def test_hybrid_gives_call_every_studio_control():
    plain = model.controls_for(Mode.CALL)
    hybrid = model.controls_for(Mode.CALL, model.HYBRID_CALL_CONTROLS)
    assert "focus" not in plain
    assert hybrid - plain == model.STUDIO_ONLY_CONTROLS
    # A backend cannot smuggle in controls the core has not vetted.
    assert model.controls_for(Mode.CALL, frozenset({"warp_drive"})) == plain
    # Studio is unaffected.
    assert model.controls_for(Mode.STUDIO, model.HYBRID_CALL_CONTROLS) == model.controls_for(Mode.STUDIO)


def test_regions_scale_from_the_output_frame_to_the_sensor():
    assert model.to_sensor_region([0, 0, 1920, 1080], (1920, 1080)) == (0, 0, 3840, 2160)
    assert model.to_sensor_region([832, 412, 256, 256], (1920, 1080)) == (1664, 824, 512, 512)
    assert model.to_sensor_region([640, 360, 128, 72], (1280, 720)) == (1920, 1080, 384, 216)
    # Clamped to the sensor, never empty.
    assert model.to_sensor_region([1900, 1070, 256, 256], (1920, 1080)) == (3800, 2140, 40, 20)
    assert model.to_sensor_region([5000, 5000, 0, 0], (1920, 1080)) == (3839, 2159, 1, 1)


def test_exposure_and_iso_are_automatic_as_a_pair():
    sticky = {}
    model.remember_applied(sticky, {"exposure": -1, "iso": -1})
    assert model.merge_reported({"exposure": 10000, "iso": 400}, sticky) == {"exposure": -1, "iso": -1}
    # Setting one by hand ends automatic for both: the other reports its value.
    model.remember_applied(sticky, {"exposure": 8000})
    assert sticky == {"exposure": 8000}
    assert model.merge_reported({"exposure": 8000, "iso": 400}, sticky) == {"exposure": 8000, "iso": 400}
    # Region requests are aimed at a moment and are not remembered.
    model.remember_applied(sticky, {"af_region": [1, 2, 3, 4], "wb": -1})
    assert sticky == {"exposure": 8000, "wb": -1}


def test_focus_refusal_in_plain_call_says_what_is_missing():
    assert model.refusal_reason(Mode.CALL, "focus", model.HYBRID_CALL_CONTROLS) is None
    why = model.refusal_reason(Mode.CALL, "focus")
    assert why and "opal-controller" in why
    assert "opal-controller" in model.refusal_reason(Mode.CALL, "wb")


def test_sticky_focus_replays_only_in_hybrid_call():
    sticky = {"focus": 140, "wb": 4200, "effect": "mono", "brightness": 5}
    assert model.sticky_for_mode(sticky, Mode.CALL) == {"brightness": 5}
    assert model.sticky_for_mode(sticky, Mode.CALL, model.HYBRID_CALL_CONTROLS) == sticky


def test_repeated_young_deaths_escalate_from_reuse_to_fresh_to_flash():
    assert [model.hybrid_plan(n) for n in range(6)] == [
        "reuse", "reuse", "fresh", "fresh", "flash", "flash",
    ]


def test_a_session_that_keeps_ending_is_given_up():
    assert not model.hybrid_losses_exceeded([], now=5000)
    assert not model.hybrid_losses_exceeded([100, 200], now=300)
    assert model.hybrid_losses_exceeded([100, 200, 290], now=300)
    # Old losses are forgotten: one bad hour does not condemn the next day.
    assert not model.hybrid_losses_exceeded([100, 200, 290], now=100 + 3600 + 150)


# -- configuration ----------------------------------------------------------

def _install(tmp_path):
    data = tmp_path / "share/opal-controller"
    data.mkdir(parents=True)
    (data / "firmware.mvcmd").write_bytes(b"fw")
    (data / "camTuning.bin").write_bytes(b"tuning")
    binary = tmp_path / "opalctl"
    binary.write_text("#!/bin/sh\n")
    return {
        "DECOMPOSER_HYBRID": "1",
        "XDG_DATA_HOME": str(tmp_path / "share"),
        "XDG_RUNTIME_DIR": str(tmp_path / "run"),
        "DECOMPOSER_OPALCTL": str(binary),
        "HOME": str(tmp_path),
    }


def test_hybrid_is_used_when_asked_for_and_installed(tmp_path):
    cfg = hybrid_config(_install(tmp_path))
    assert cfg is not None
    assert cfg.socket == tmp_path / "run/decomposer/opal-controller.sock"


def test_hybrid_is_opt_in(tmp_path):
    # Installed is not enough: a hybrid session keeps the camera on, and
    # that is the user's choice to make.
    env = _install(tmp_path)
    del env["DECOMPOSER_HYBRID"]
    assert hybrid_config(env) is None
    env["DECOMPOSER_HYBRID"] = "0"
    assert hybrid_config(env) is None


def test_the_engine_is_told_to_keep_a_hybrid_camera_on():
    plain = model.EngineConfig(input="/dev/video0")
    hybrid = model.EngineConfig(input="/dev/video0", keep_camera_on=True)
    assert "--keep-camera-on" not in model.engine_cli_args(plain)
    assert "--keep-camera-on" in model.engine_cli_args(hybrid)
    assert hybrid.needs_restart_from(plain)


def test_no_hybrid_without_the_cameras_firmware(tmp_path):
    env = _install(tmp_path)
    (tmp_path / "share/opal-controller/firmware.mvcmd").unlink()
    assert hybrid_config(env) is None


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
                reply = f"ok {state}" + (" streaming=1 frames=90 lens=153 exposure_us=24992 iso=868" if state == "ready" else "")
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
        socket=tmp_path / "run/ctl.sock",
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
    assert "--mic-nodes" in argv and "--socket" in argv and "--reclaim" in argv
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


def test_commands_refusals_and_telemetry(rig):
    build, made = rig
    controller = build(["ready"])
    controller.start(timeout=5)
    controller.command("focus 140")
    controller.command("wb auto")
    assert made["service"].seen[-2:] == ["focus 140", "wb auto"]
    with pytest.raises(ValueError, match="0..255"):
        controller.command("focus 999")
    assert controller.telemetry() == {"streaming": 1, "frames": 90, "lens": 153, "exposure_us": 24992, "iso": 868}


def test_a_silent_controller_is_not_ready(tmp_path):
    config = HybridConfig("/opt/opalctl", tmp_path / "fw", tmp_path / "tun", tmp_path / "nobody.sock")
    controller = HybridController(config)
    assert controller.state() is None
    assert not controller.ready()


# -- the backend ------------------------------------------------------------

class RecordingController:
    def __init__(self, fail=None):
        self.lines, self.fail = [], fail

    def command(self, line):
        if self.fail:
            raise self.fail
        self.lines.append(line)


@pytest.fixture
def backend(monkeypatch):
    monkeypatch.setattr(controller_proc.UvcBackend, "apply_controls", lambda self, values: ({}, {}))
    monkeypatch.setattr(controller_proc.UvcBackend, "read_controls", lambda self: {"brightness": 127, "iso": 800})
    controller = RecordingController()
    b = HybridBackend(controller, frame_size=lambda: (1920, 1080), node_resolver=lambda: "/dev/video0")
    return b, controller


def test_backend_is_call_mode_with_the_studio_controls(backend):
    b, _ = backend
    assert b.mode is Mode.CALL
    assert b.extra_controls == model.STUDIO_ONLY_CONTROLS


def test_values_and_automatic_go_to_the_controller(backend):
    b, controller = backend
    assert b.apply_controls({"focus": 140, "wb": 4200}) == ({"focus": 140, "wb": 4200}, {})
    assert b.apply_controls({"focus": -1, "wb": -1}) == ({"focus": -1, "wb": -1}, {})
    assert controller.lines == ["focus 140", "wb 4200", "focus auto", "wb auto"]


def test_effects_scenes_and_regions(backend):
    b, controller = backend
    applied, refused = b.apply_controls({
        "effect": "Mono", "scene": "night",
        "af_region": [832, 412, 256, 256], "ae_region": [832, 412, 256, 256],
    })
    assert refused == {}
    assert applied["effect"] == "mono" and applied["af_region"] == [832, 412, 256, 256]
    # Exposure is metered before focus is triggered, both in sensor pixels.
    assert controller.lines == ["effect mono", "scene night", "ae-region 1664 824 512 512", "af-region 1664 824 512 512"]
    applied, refused = b.apply_controls({"effect": "sparkle"})
    assert applied == {} and "unknown effect" in refused["effect"]


def test_readback_reports_intent_for_what_the_firmware_cannot_report(backend):
    b, _ = backend
    assert b.read_controls() == {"brightness": 127, "iso": 800, "focus": -1, "wb": -1}
    b.apply_controls({"focus": 90, "wb": 5000})
    assert b.read_controls()["focus"] == 90 and b.read_controls()["wb"] == 5000
    # Tap-to-focus hands the lens back to autofocus.
    b.apply_controls({"af_region": [0, 0, 100, 100]})
    assert b.read_controls()["focus"] == -1


def test_auto_undoes_a_tap(backend):
    b, controller = backend
    b.apply_controls({"af_region": [100, 100, 256, 256], "ae_region": [100, 100, 256, 256]})
    controller.lines.clear()
    b.apply_controls({"focus": -1})
    assert controller.lines == ["focus auto", "ae-region 0 0 3840 2160"]
    controller.lines.clear()
    b.apply_controls({"focus": -1})  # nothing aimed any more: only a refocus
    assert controller.lines == ["focus auto"]


def test_replay_after_an_engine_restart_does_not_refocus(backend):
    b, controller = backend
    applied, refused = b.replay_controls({"focus": -1, "wb": -1, "effect": "mono"})
    assert controller.lines == ["effect mono"]
    assert applied == {"focus": -1, "wb": -1, "effect": "mono"} and refused == {}
    controller.lines.clear()
    b.replay_controls({"focus": 150, "wb": 4000})
    assert controller.lines == ["focus 150", "wb 4000"]


def test_a_controller_failure_is_a_refusal_not_a_crash(monkeypatch):
    monkeypatch.setattr(controller_proc.UvcBackend, "apply_controls", lambda self, values: ({"brightness": 3}, {}))
    b = HybridBackend(RecordingController(fail=OSError("not answering")), node_resolver=lambda: "/dev/video0")
    applied, refused = b.apply_controls({"brightness": 3, "focus": 140})
    assert applied == {"brightness": 3}
    assert "not answering" in refused["focus"]
