"""The single control-routing table, and the sticky-replay rules."""

from opal_c1.core.model import (
    CALL_ONLY_CONTROLS,
    SHARED_CONTROLS,
    STICKY_CONTROLS,
    STUDIO_ONLY_CONTROLS,
    Mode,
    controls_for,
    merge_reported,
    refusal_reason,
    sticky_for_mode,
)
from opal_c1.ports import CameraBackend, FrameSource

from fake_camera import FakeCamera


def test_every_control_belongs_to_exactly_one_family():
    families = [CALL_ONLY_CONTROLS, SHARED_CONTROLS, STUDIO_ONLY_CONTROLS]
    for i, a in enumerate(families):
        for b in families[i + 1:]:
            assert not (a & b), f"overlap: {a & b}"


def test_shared_controls_are_reachable_in_both_modes():
    for control in SHARED_CONTROLS:
        assert refusal_reason(Mode.CALL, control) is None
        assert refusal_reason(Mode.STUDIO, control) is None


def test_studio_only_controls_are_refused_in_call_with_a_reason():
    for control in STUDIO_ONLY_CONTROLS:
        why = refusal_reason(Mode.CALL, control)
        assert why and "Studio" in why


def test_call_only_controls_are_refused_in_studio_with_a_reason():
    for control in CALL_ONLY_CONTROLS:
        why = refusal_reason(Mode.STUDIO, control)
        assert why and "Call" in why


def test_unknown_controls_are_named_in_the_refusal():
    assert "warp_drive" in refusal_reason(Mode.CALL, "warp_drive")


def test_regions_are_never_sticky():
    # A tap-to-focus was aimed at a moment, not a policy: replaying the last
    # tap after a firmware reboot would surprise.
    assert "af_region" not in STICKY_CONTROLS
    assert "ae_region" not in STICKY_CONTROLS


def test_sticky_replay_respects_the_mode():
    sticky = {"focus": 150, "brightness": 140, "effect": "sepia", "iso": 400}
    studio = sticky_for_mode(sticky, Mode.STUDIO)
    call = sticky_for_mode(sticky, Mode.CALL)
    assert studio == {"focus": 150, "effect": "sepia", "iso": 400}
    assert call == {"brightness": 140, "iso": 400}


def test_merge_reported_prefers_explicit_auto_over_hunting_isp():
    live = {"focus": 87, "iso": 1200}  # the lens mid-hunt
    sticky = {"focus": -1}
    merged = merge_reported(live, sticky)
    assert merged["focus"] == -1
    assert merged["iso"] == 1200


def test_merge_reported_supplies_effect_which_has_no_readback():
    assert merge_reported({}, {"effect": "sepia"})["effect"] == "sepia"


def test_fake_camera_satisfies_both_ports():
    fake = FakeCamera()
    assert isinstance(fake, CameraBackend)
    assert isinstance(fake, FrameSource)


def test_fake_camera_frames_and_silence():
    fake = FakeCamera(frames_before_silence=2)
    fake.attach()
    assert fake.try_read_frame() is not None
    assert fake.try_read_frame() is not None
    assert fake.try_read_frame() is None  # silent, not dead: the stall shape


def test_fake_camera_refusals_flow_through():
    fake = FakeCamera(refuse={"wb": "hardware said no"})
    fake.attach()
    applied, refused = fake.apply_controls({"focus": 10, "wb": 3000})
    assert applied == {"focus": 10}
    assert refused == {"wb": "hardware said no"}
