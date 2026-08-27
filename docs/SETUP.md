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

## The virtual camera (v4l2loopback)

decomposer publishes to `/dev/video10`. The module options pin the node
number, name the device, and set `exclusive_caps=1` so apps only see the
camera while the engine is publishing.

```
sudo install -m 0644 packaging/v4l2loopback.conf /etc/modprobe.d/
sudo install -m 0644 packaging/v4l2loopback-load.conf /etc/modules-load.d/
sudo modprobe v4l2loopback
```

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
