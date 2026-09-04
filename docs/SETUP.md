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
receives (and its timestamp) is recent. While a SEND flip is on and something is watching the
Normal camera, the engine renders that frame a second time without the flip,
so overlays and replacement backgrounds stay in place on both feeds.

The Normal camera is optional. When `/dev/video11` is missing, busy, or not a
loopback output, the daemon publishes the SEND camera alone and `decomposer
status` says so.

The module options pin both node numbers, name the devices, and set
`exclusive_caps=1` so camera clients negotiate them correctly.

```
sudo install -m 0644 packaging/v4l2loopback.conf /etc/modprobe.d/
sudo install -m 0644 packaging/v4l2loopback-load.conf /etc/modules-load.d/
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

## Arch packaging

`packaging/PKGBUILD` is a starting point for an AUR package that
installs all of the above in their system locations.
