//! Camera rest: when the camera may stop streaming, and how long it settles
//! after starting again. Kept free of device state so it can be tested; the
//! render loop does the stopping and the holding.

use std::time::{Duration, Instant};

/// Unwatched for this long, the camera stops streaming and its light goes
/// off. Long enough that an app probing the device does not flap it.
pub const PAUSE_AFTER: Duration = Duration::from_secs(5);

/// Streaming again, the Opal hands over about half a second of stale frames,
/// ~270 ms of black and an exposure overshoot before it settles, around
/// 900 ms in. Viewers keep the held frame until then.
pub const RESUME_SETTLE: Duration = Duration::from_millis(1200);

/// What the render loop does with the frame in hand.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Step {
    /// Render and publish it as usual.
    Run,
    /// The camera is still waking: viewers keep the held frame.
    Settle,
    /// Render it in full, hold it, and stop the camera.
    Pause,
}

pub struct Rest {
    can_pause: bool,
    idle_since: Option<Instant>,
    settle_until: Option<Instant>,
}

impl Rest {
    pub fn new() -> Self {
        Self {
            can_pause: true,
            idle_since: None,
            settle_until: None,
        }
    }

    /// Decide for a frame that arrived at `now`. The grace period counts
    /// from the first unwatched frame after the camera has settled, so a
    /// viewer who leaves during the settle still gets the full grace.
    pub fn step(&mut self, watched: bool, now: Instant) -> Step {
        let settling = self.settle_until.is_some_and(|t| now < t);
        if !settling {
            self.settle_until = None;
        }
        if watched || settling {
            self.idle_since = None;
        } else if self.idle_since.is_none() {
            self.idle_since = Some(now);
        }
        if settling {
            Step::Settle
        } else if self.can_pause && self.idle_since.is_some_and(|t| now - t >= PAUSE_AFTER) {
            Step::Pause
        } else {
            Step::Run
        }
    }

    /// The camera started streaming again at `now`.
    pub fn resumed(&mut self, now: Instant) {
        self.settle_until = Some(now + RESUME_SETTLE);
    }

    /// The source has no device to stop: never ask again.
    pub fn cannot_pause(&mut self) {
        self.can_pause = false;
    }
}

#[cfg(test)]
mod tests {
    use super::{Rest, Step, PAUSE_AFTER, RESUME_SETTLE};
    use std::time::{Duration, Instant};

    fn ms(n: u64) -> Duration {
        Duration::from_millis(n)
    }

    #[test]
    fn a_watched_camera_never_pauses() {
        let (mut rest, t0) = (Rest::new(), Instant::now());
        for s in 0..60 {
            assert_eq!(rest.step(true, t0 + Duration::from_secs(s)), Step::Run);
        }
    }

    #[test]
    fn pauses_only_after_the_grace_period() {
        let (mut rest, t0) = (Rest::new(), Instant::now());
        assert_eq!(rest.step(false, t0), Step::Run);
        assert_eq!(rest.step(false, t0 + PAUSE_AFTER - ms(1)), Step::Run);
        assert_eq!(rest.step(false, t0 + PAUSE_AFTER), Step::Pause);
    }

    #[test]
    fn a_viewer_restarts_the_grace_period() {
        let (mut rest, t0) = (Rest::new(), Instant::now());
        rest.step(false, t0);
        rest.step(true, t0 + ms(4000));
        let left = t0 + ms(6000);
        assert_eq!(rest.step(false, left), Step::Run);
        assert_eq!(rest.step(false, left + PAUSE_AFTER - ms(1)), Step::Run);
        assert_eq!(rest.step(false, left + PAUSE_AFTER), Step::Pause);
    }

    #[test]
    fn frames_settle_after_resuming() {
        let (mut rest, t0) = (Rest::new(), Instant::now());
        rest.resumed(t0);
        assert_eq!(rest.step(true, t0 + ms(300)), Step::Settle);
        assert_eq!(rest.step(true, t0 + RESUME_SETTLE - ms(1)), Step::Settle);
        assert_eq!(rest.step(true, t0 + RESUME_SETTLE), Step::Run);
    }

    #[test]
    fn a_viewer_who_leaves_while_settling_still_gets_the_grace() {
        let (mut rest, t0) = (Rest::new(), Instant::now());
        rest.resumed(t0);
        assert_eq!(rest.step(false, t0 + ms(500)), Step::Settle);
        let settled = t0 + RESUME_SETTLE;
        assert_eq!(rest.step(false, settled), Step::Run);
        assert_eq!(rest.step(false, settled + PAUSE_AFTER - ms(1)), Step::Run);
        assert_eq!(rest.step(false, settled + PAUSE_AFTER), Step::Pause);
    }

    #[test]
    fn a_source_that_cannot_pause_just_runs() {
        let (mut rest, t0) = (Rest::new(), Instant::now());
        rest.cannot_pause();
        rest.step(false, t0);
        assert_eq!(rest.step(false, t0 + PAUSE_AFTER * 10), Step::Run);
    }
}
