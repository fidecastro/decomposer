"""The optional normal camera: offered when the node takes frames, reported
only while the engine publishes it."""

import opal_c1.daemon as daemon_module
from opal_c1.core.model import EngineConfig
from opal_c1.daemon import Daemon


def test_normal_output_is_offered_only_when_the_node_takes_frames(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setattr(daemon_module, "camera_video_node", lambda: "/dev/video0")
    daemon = Daemon()

    monkeypatch.setattr(
        daemon_module, "output_ready", lambda path: path == "/dev/video11"
    )
    assert daemon._engine_config(False).normal_output == "/dev/video11"

    # A missing node, or a real webcam at that path, means SEND only.
    monkeypatch.setattr(daemon_module, "output_ready", lambda path: False)
    assert daemon._engine_config(False).normal_output is None
    assert daemon._engine_config(False).output == "/dev/video10"


class _Engine:
    def __init__(self, config):
        self.config = config

    @staticmethod
    def alive():
        return True

    @staticmethod
    def log_lines():
        return []

    @staticmethod
    def returncode():
        return None


def test_status_reports_the_normal_feed_only_while_it_is_published(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    daemon = Daemon()
    assert daemon.status()["normal_active"] is False

    daemon._engine = _Engine(EngineConfig(normal_output="/dev/video11"))
    assert daemon.status()["normal_active"] is True

    daemon._engine = _Engine(EngineConfig())
    assert daemon.status()["normal_active"] is False
