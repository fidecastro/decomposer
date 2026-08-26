"""The pure core: domain model, state machine, and policies.

Nothing in this package performs IO — no filesystem, no sockets, no
subprocesses, no hardware, no clocks. Time arrives as a parameter, device
facts arrive as plain values, and decisions leave as plain values. That is
what makes this the part of decomposer that can be tested in milliseconds
against every failure the camera has ever shown us, instead of against the
camera.

The rule is enforced by tests/test_core_purity.py: a module here that
imports os, socket, subprocess, threading, time, pathlib, json, depthai or
gi is a test failure, not a style complaint.
"""
