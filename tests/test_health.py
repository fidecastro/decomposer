"""Supervision policy: today's failure zoo, replayed in milliseconds.

Every scenario here was observed on real hardware on 2026-08-26. The point of
the pure policy is that we never again need the camera's cooperation to know
the supervisor handles them.
"""

from opal_c1.core.health import (
    GIVE_UP_AFTER,
    RETRY_FLOOR,
    SHORT_LIVES_LIMIT,
    VANISHED_LIMIT,
    EnginePolicy,
    Kind,
    StallDetector,
)
from opal_c1.core.model import Mode


def test_ordinary_death_retries_at_the_mode_floor():
    policy = EnginePolicy()
    action = policy.on_death(Mode.CALL, uptime=300.0, camera_on_bus=True)
    assert action.kind is Kind.RETRY
    assert action.delay == RETRY_FLOOR[Mode.CALL]


def test_studio_retry_floor_reflects_the_firmware_reboot():
    policy = EnginePolicy()
    action = policy.on_death(Mode.STUDIO, uptime=300.0, camera_on_bus=True)
    assert action.delay == RETRY_FLOOR[Mode.STUDIO]


def test_dies_young_declares_the_camera_sick():
    # The ~11s crash-reboot loop: bus presence looks fine at every check,
    # only the engine's lifetime gives it away.
    policy = EnginePolicy()
    for i in range(SHORT_LIVES_LIMIT - 1):
        action = policy.on_death(Mode.CALL, uptime=11.0, camera_on_bus=True)
        assert action.kind is Kind.RETRY, f"attempt {i} should still retry"
    action = policy.on_death(Mode.CALL, uptime=11.0, camera_on_bus=True)
    assert action.kind is Kind.HOLD_SICK
    assert "2-3 minutes" in action.message


def test_a_long_run_resets_the_dies_young_counter():
    policy = EnginePolicy()
    for _ in range(SHORT_LIVES_LIMIT - 1):
        policy.on_death(Mode.CALL, uptime=11.0, camera_on_bus=True)
    policy.on_death(Mode.CALL, uptime=300.0, camera_on_bus=True)
    action = policy.on_death(Mode.CALL, uptime=11.0, camera_on_bus=True)
    assert action.kind is Kind.RETRY


def test_sick_hold_stays_suspicious_afterwards():
    # After the hold, one more young death should re-trigger quickly, not
    # need four fresh ones - the camera did not get healthier by us waiting.
    policy = EnginePolicy()
    for _ in range(SHORT_LIVES_LIMIT):
        policy.on_death(Mode.CALL, uptime=11.0, camera_on_bus=True)
    kinds = [
        policy.on_death(Mode.CALL, uptime=11.0, camera_on_bus=True).kind
        for _ in range(2)
    ]
    assert Kind.HOLD_SICK in kinds


def test_vanished_camera_holds_after_the_limit():
    policy = EnginePolicy()
    for i in range(VANISHED_LIMIT - 1):
        action = policy.on_death(Mode.CALL, uptime=0.0, camera_on_bus=False)
        assert action.kind is Kind.RETRY, f"check {i} should still retry"
    action = policy.on_death(Mode.CALL, uptime=0.0, camera_on_bus=False)
    assert action.kind is Kind.HOLD_VANISHED
    assert "unplug" in action.message


def test_camera_reappearing_resets_the_vanish_counter():
    policy = EnginePolicy()
    for _ in range(VANISHED_LIMIT - 1):
        policy.on_death(Mode.CALL, uptime=0.0, camera_on_bus=False)
    policy.on_death(Mode.CALL, uptime=300.0, camera_on_bus=True)
    action = policy.on_death(Mode.CALL, uptime=0.0, camera_on_bus=False)
    assert action.kind is Kind.RETRY


def test_studio_falls_back_to_call_after_repeated_reentry_failures():
    policy = EnginePolicy()
    for i in range(GIVE_UP_AFTER - 1):
        action = policy.on_reentry_failed(Mode.STUDIO, "boot failed")
        assert action.kind is Kind.RECORD_FAILURE, f"failure {i}"
    action = policy.on_reentry_failed(Mode.STUDIO, "boot failed")
    assert action.kind is Kind.FALLBACK_TO_CALL
    assert "fell back" in action.message


def test_call_reentry_failures_never_fall_back():
    # There is nothing below Call mode to fall back to.
    policy = EnginePolicy()
    for _ in range(GIVE_UP_AFTER + 2):
        action = policy.on_reentry_failed(Mode.CALL, "node missing")
        assert action.kind is Kind.RECORD_FAILURE


def test_reentry_backoff_grows_and_is_capped():
    policy = EnginePolicy()
    delays = []
    for _ in range(8):
        policy.on_reentry_failed(Mode.CALL, "x")
        delays.append(policy.backoff)
    assert delays == sorted(delays)
    assert delays[-1] <= 120.0


def test_stall_detected_only_after_the_window():
    detector = StallDetector(window=10.0)
    assert not detector.update(frames=100, now=0.0)
    assert not detector.update(frames=100, now=5.0)
    assert detector.update(frames=100, now=10.0)


def test_advancing_frames_never_stall():
    detector = StallDetector(window=10.0)
    for tick in range(40):
        assert not detector.update(frames=tick, now=float(tick))


def test_stall_recovers_when_frames_resume():
    detector = StallDetector(window=10.0)
    detector.update(frames=7, now=0.0)
    assert detector.update(frames=7, now=12.0)
    assert not detector.update(frames=8, now=13.0)


def test_replug_clears_absence_and_backoff():
    # A reconnected camera deserves a prompt retry: absence history and
    # grown backoff described the old connection.
    policy = EnginePolicy()
    for _ in range(VANISHED_LIMIT - 1):
        policy.on_death(Mode.CALL, uptime=0.0, camera_on_bus=False)
    policy.on_reentry_failed(Mode.CALL, "x")
    policy.on_replug()
    assert policy.backoff == 0.0
    action = policy.on_death(Mode.CALL, uptime=0.0, camera_on_bus=False)
    assert action.kind is Kind.RETRY


def test_replug_does_not_launder_short_lives():
    # The crash-reboot cycle emits an add event per reboot, identical to a
    # human replugging the cable. If those events reset short_lives, the
    # sick-hold could never engage and the loop would churn forever.
    policy = EnginePolicy()
    for _ in range(SHORT_LIVES_LIMIT - 1):
        policy.on_death(Mode.CALL, uptime=11.0, camera_on_bus=True)
    policy.on_replug()
    action = policy.on_death(Mode.CALL, uptime=11.0, camera_on_bus=True)
    assert action.kind is Kind.HOLD_SICK


# The double-restart regression is daemon wiring, not policy - but the
# policy-side invariant it depends on is pinned here: note_alive after a
# hold must fully clear the retry pressure.
def test_note_alive_clears_retry_pressure():
    policy = EnginePolicy()
    policy.on_death(Mode.STUDIO, uptime=3000.0, camera_on_bus=True)
    policy.on_reentry_failed(Mode.STUDIO, "x")
    policy.note_alive()
    assert policy.backoff == 0.0
    assert policy.failures == 0
