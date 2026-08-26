# Roadmap

Two lists: hardening first, features second. The hardening list is the output of
a skeptical review (2026-08-26) of the whole codebase after a day of real-world
failures; the features list is what remains from the Composer gap analysis.
**Hardening outranks features** — the day proved the failure paths get exercised
constantly, because the camera's own firmware is fragile and every mode switch
reboots it.

## What the failures taught us (facts, not guesses)

- The C1's degraded state is **specific to Opal's UVC firmware path**: in that
  state, Call mode dies ~11s into streaming (`cannot submit urb, err -19`,
  device falls to bootloader) while **Studio mode — stock DepthAI firmware —
  streams indefinitely**. The hardware is fine; one of the two firmwares isn't.
- The state correlates with heavy mode-switch churn (each switch is a firmware
  reboot). A 30s unplug cleared it once and failed to clear it later the same
  day; a longer powerless rest was needed.
- Switching modes while the camera is mid-reboot can attach depthai to the
  **bootloader** ("Couldn't read data from stream: `_bootloader`
  (X_LINK_ERROR)"), wedging the session.
- `/dev/videoN` numbers are not stable across re-enumerations.
- v4l2loopback pins its negotiated format while any consumer holds the node,
  and OBS keeps running (and holding) in the tray after its window closes.

## Hardening (P0 — do before any feature)

1. **Serialize mode transitions.** `enter_call`/`enter_studio` run on client
   handler threads *and* the supervisor thread with no transition lock; two
   concurrent switches can interleave teardown/startup, which matches the
   observed "switching back and forth got stuck". Add a transition mutex, a
   `transitioning` field in status (so the panel can say what is happening),
   and reject a switch while one is in flight. Also stop holding the big state
   lock across `OpalDevice.open()` and engine start retries — today a switch
   blocks every `status()` call for up to ~20s, which is why the panel freezes.

2. **Gate depthai attach on device state.** `find_device()` takes `devices[0]`
   in any state. It must wait (with deadline) for `X_LINK_FLASH_BOOTED`, skip
   error-status devices, and treat "only a bootloader present" as *mid-
   transition, wait* — never attach to it. This is the `_bootloader
   X_LINK_ERROR` guardrail.

3. **Stall watchdog.** The supervisor only checks `engine.poll()`. A camera
   that stops delivering leaves everything "alive" with zero frames flowing —
   the pump spins on `try_read() -> None`, the engine blocks in `read_exact`,
   status looks healthy. Detect no-frames-for-N-seconds (a daemon-side preview
   client is the mode-agnostic observable) and treat it as an engine death so
   the existing backoff machinery applies.

4. **Rate-limit firmware transitions.** Nothing stops clients, presets, the
   supervisor fallback chain, or a misbehaving script from cycling modes
   rapidly — and rapid cycling is exactly what degrades the camera. Enforce a
   minimum interval between firmware reboots with a clear refusal message.

5. **Fix teardown ordering for a blocked pump.** `_teardown` joins the pump
   *before* closing the engine's stdin; a pump blocked in `stdin.write` cannot
   exit until stdin closes, so the join times out and the device is left open.
   Close/signal stdin first, then join.

6. **Accept clients before the initial mode entry.** `run()` performs
   `set_mode(initial_mode)` (up to ~30s of retries) before the accept loop, so
   `decomposer status` during startup hits a connection refusal or timeout.
   Start serving immediately; enter the initial mode on a thread.

## Hardening (P1)

7. **Replay Studio camera state after re-entry.** Effect, scene, manual
   focus/WB and regions die with the firmware on every Studio exit or engine
   restart, but `state.controls` still reports them — status lies. Either
   replay them on `enter_studio` or clear them; replaying matches user intent.
8. **`status()` race:** `self.engine is not None and self.engine.poll()` can
   AttributeError if `_stop_engine` nulls the field between the operands; take
   a local reference.
9. **`probe-xlink` while the daemon holds Studio** claims interface 0 out from
   under the live depthai session. Refuse when the device is in use.
10. **Panel refresh flood:** a blocked daemon plus the 2s tick piles up worker
    threads each with a 60s socket timeout. Single in-flight refresh guard.
11. **Preset `--with-mode` partial application:** a failed switch aborts the
    whole load; apply the non-mode parts first, then attempt the switch, and
    report precisely what happened.

## Hardening (P1, found during refactor phase 2)

- **Replaced panels linger.** `gui --replace` takes the D-Bus name but the
  displaced GTK process does not exit — it stays alive, windowless, polling
  the daemon on its old code. Two were found running side by side. Handle
  name-loss by quitting.
- **`status.error` mixes history with the present.** After a successful
  recovery it still carries "engine restarted after: <old log>", which reads
  as a live fault. Split into `error` (current) and `last_event` (history).
- **Unattributed `look mono` on the engine control socket** during the
  phase-2 smoke, while daemon state stayed `none`. Observed once, with a
  stale pre-refactor panel alive, so attribution is impossible — but the
  engine control socket accepts lines from any same-user process with no
  provenance. Worth logging accepted commands engine-side.

## Hardening (P2)

12. Cache `_live_controls` for ~1s (currently 6 open/query/close ioctl rounds
    per status poll, GUI polls every 2s).
13. `LOOKS` is computed at import and served by the `looks` command while
    `status` recomputes fresh — one source of truth.
14. `lut::find` look names come via the engine control socket; the daemon
    validates but the socket itself would accept `../` names. Sanitize in the
    engine too (defence in depth, local-only surface).
15. Engine LUT/overlay loads do file IO inline between frames — a slow disk is
    a frame hitch. Load off-thread, swap atomically.

## Features (deferred until P0 is done)

- **Digital zoom / crop** — the sensor is 8000x6000 and we publish at most 4K;
  crop+pan with no quality loss. Studio mode via ISP; Call mode via shader crop.
- **CLAHE** — local contrast, a real GPU pass (Composer shipped it).
- **Background blur / replacement** — segmentation on the idle 4090. The
  largest remaining Composer gap. Model choice + licensing needs its own
  research pass.
- **AE region re-verification** — the internal 2x scaling was verified end to
  end once, then a retest was confounded by a changed scene; re-verify next
  Studio session with fresh tiles.
- **Composer default-intensity check** — on the Mac: does `Default/data.opaldata`
  set `filters.intensity` explicitly, or omit it (schema default 0.5)? Decides
  nothing for us (we chose 0.5 deliberately) but worth recording.
- **Windows** — README promises "Windows later". The engine and looks port;
  Call mode does not (no v4l2); needs a DirectShow/MediaFoundation virtual
  camera story. Genuinely large.
- **Packaging** — a systemd user service for the daemon (autostart, restart on
  failure — would have absorbed several of today's manual restarts), an AUR
  package, `decomposer doctor` (udev present? loopback loaded? engine built?
  LUTs installed?).
- **Mic note in the panel** — Call mode could show which mic to pick (the C1)
  next to "mic on", since the whole point of Call mode is keeping it.
