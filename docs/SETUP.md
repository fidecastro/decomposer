# System setup

Every command that touches your system lives on this page, and only on
this page — the application itself never runs a privileged command and
never tells you to from inside the code. `decomposer doctor` diagnoses
what is missing and points here; you decide what to run.

## GUI dependencies

The panel needs PyGObject with GTK4 and libadwaita, plus
gtk4-layer-shell for the overlay surface.

```
# Arch
sudo pacman -S python-gobject gtk4 libadwaita gtk4-layer-shell

# or, inside the virtualenv
pip install 'decomposer[gui]'
```

## The virtual cameras (v4l2loopback)

decomposer publishes the processed frame to two devices:

- `/dev/video10`, **decomposer Send Flip**, follows the panel's SEND flips.
- `/dev/video11`, **decomposer Normal**, always removes SEND flips. Select
  this once in Meet or any other app that should be excluded.

Both outputs subscribe to v4l2loopback's client-usage event. After one priming
frame makes a device discoverable, decomposer converts and writes frames only
while an application is actually streaming from that device, plus a
keep-warm frame every 100 ms so that the first frame a newly connected viewer
receives (and its timestamp) is recent. With nothing streaming from either
output and the panel closed for five seconds, Call mode stops the camera
altogether and both outputs keep publishing the last frame; the camera starts
again the moment a viewer does. While a SEND flip is on and something is watching the
Normal camera, the engine renders that frame a second time without the flip,
so overlays and replacement backgrounds stay in place on both feeds.

The Normal camera is optional. When `/dev/video11` is missing, busy, or not a
loopback output, the daemon publishes the SEND camera alone and `decomposer
status` says so.

The module options pin both node numbers, name the devices, and set
`exclusive_caps=1` so camera clients negotiate them correctly.

The udev rule makes both devices report as cameras from the moment they are
created, by setting v4l2loopback's `keep_format` control. Without it they
report as output-only until decomposer publishes its first frame. PipeWire
inspects a device once, when it appears, so it would never offer them, and
applications that take their cameras from PipeWire (Chrome and Zoom's web
client on Omarchy) would not list them. `decomposer doctor` checks that
PipeWire offers both. The rule needs `v4l2-ctl` (package `v4l-utils`).

```
sudo install -m 0644 packaging/v4l2loopback.conf /etc/modprobe.d/
sudo install -m 0644 packaging/v4l2loopback-load.conf /etc/modules-load.d/
sudo install -m 0644 packaging/59-decomposer-loopback.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules
sudo modprobe v4l2loopback
```

### Upgrading

The original modprobe file created `/dev/video10` only. To add the Normal
camera to an existing install, reinstall the file and reload the module while
no application holds either camera:

```
sudo install -m 0644 packaging/v4l2loopback.conf /etc/modprobe.d/
sudo modprobe -r v4l2loopback && sudo modprobe v4l2loopback
```

Until then everything keeps working on `/dev/video10`; `decomposer doctor`
lists the second node as absent rather than as a fault.

## udev rules

Grants your seat access to the C1 in both firmware personalities. The
`60-` prefix matters: it must run before `73-seat-late.rules` or the
uaccess tag never applies.

```
sudo install -m 0644 packaging/60-opal-c1.rules /etc/udev/rules.d/
sudo udevadm control --reload
```

## USB quirk (optional, recommended)

Disables USB3 link power management for the C1's Opal personality.
Myriad X devices are documented to misbehave under U1/U2 transitions;
the quirk is harmless when not needed.

```
sudo install -m 0644 packaging/decomposer-usb.conf /etc/tmpfiles.d/
sudo systemd-tmpfiles --create
```

## Run the daemon as a service (optional)

```
decomposer install-service       # writes a user unit, no privileges needed
systemctl --user enable --now decomposer
```

## Studio's controls in Call mode (opal-controller)

Call mode normally runs the camera's stored firmware, which offers video and
the microphone but keeps focus and white balance automatic. With
[opal-controller](https://github.com/fidecastro/opal-controller) installed,
decomposer instead has it boot the camera's own, unmodified firmware into
RAM. The camera then presents the same video and microphone, plus a control
channel, and Call mode gains everything Studio has: manual focus and white
balance (each with an auto button), tap-to-focus, exposure metering regions,
effects and scenes. Nothing is written to the camera's flash; unplugging it
returns it to the stored firmware.

What stays Studio-only: frame rates above 30, the 4:3 12 MP mode and the
on-camera person mask.

It is opt-in, because of one limit. Under the controller the camera's
video stream does not reliably start again once it has been stopped: every
restart one second after a stop failed, and about one in fifteen after
longer pauses, where the stored firmware survived 42 of 42. So in this mode
decomposer never pauses the camera. **It stays on, light included, for as
long as Call mode runs.** Streaming continuously it ran for an hour without
a fault. If you want the camera to switch off when no application is
using it, stay on the default.

To turn it on, set `DECOMPOSER_HYBRID=1` for the daemon (for the user
service: `Environment=DECOMPOSER_HYBRID=1` under `[Service]`) and have:

- `opalctl` on `PATH` or at `~/.local/bin/opalctl` (or `DECOMPOSER_OPALCTL`);
- `~/.local/share/opal-controller/firmware.mvcmd`, `camTuning.bin` and
  `detection.blob`, taken from your own camera (the controller's
  documentation describes how). All three are needed.

Run this way the camera's microphone carries a generic USB name, and
applications list it as "Myriad VPU [Movidius Neural Compute Stick]". To
have it called "Opal C1 Microphone":

```
install -Dm644 packaging/51-opal-c1-microphone.conf \
  ~/.config/wireplumber/wireplumber.conf.d/51-opal-c1-microphone.conf
systemctl --user restart wireplumber
```

If the controller cannot start, or its session keeps ending, decomposer says
so in `decomposer status` and runs Call mode on the stored firmware.
`decomposer status` shows `hybrid` when it is in use.

Entering Call mode this way takes about 15 seconds longer the first time:
the camera reboots into RAM and the firmware is given time to settle before
anything asks it to stream. An engine restart does not repeat that.

Autofocus in this mode is done by opal-controller, not by the firmware. It
sweeps the lens once (about three seconds, when the camera first streams),
holds the sharpest position, and every 15 seconds tries one lens step either
side, too small to see, following it only if the picture gets clearly
sharper. The Focus row's auto button starts a new sweep; a tap on the
preview focuses on that spot, and auto undoes the tap.

## USB 2 recovery

If the C1 repeatedly enumerates as `03e7:f63c` and fails to start over
USB 3, an explicit USB 2 Studio connection may work:

```
DECOMPOSER_USB_SPEED=high decomposer daemon --mode studio --width 1280 --height 720 --fps 15
```

This waits for three seconds of stable bootloader enumeration, then boots
DepthAI into RAM without changing the stored Opal firmware. The lower
resolution and frame rate fit USB 2 bandwidth. Studio mode has no camera
microphone. For a user service, add `Environment=DECOMPOSER_USB_SPEED=high`
under `[Service]` and use the same mode, dimensions, and frame rate in
`ExecStart`. Preserve any custom virtual-camera output paths.

## Arch packaging

`packaging/PKGBUILD` is a starting point for an AUR package that
installs all of the above in their system locations.
