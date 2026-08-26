#!/usr/bin/env bash
# Capture Mac reference stills for look matching.
# Usage:
#   ./scripts/capture_mac_refs.sh raw          # quit Composer first
#   ./scripts/capture_mac_refs.sh composer     # Composer open with look selected
#   ./scripts/capture_mac_refs.sh composer noir
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
REF="$ROOT/references"
mkdir -p "$REF"
BIN="$ROOT/scripts/capture_mac_frame"
SWIFT="$ROOT/scripts/capture_mac_frame.swift"

if [[ ! -x "$BIN" ]] || [[ "$SWIFT" -nt "$BIN" ]]; then
  echo "Compiling Mac frame grabber…"
  swiftc -O "$SWIFT" -o "$BIN"
fi

mode="${1:-}"
look="${2:-default}"

case "$mode" in
  raw)
    echo "Capturing raw Opal C1 (Composer should be quit)…"
    "$BIN" "Opal C1" "$REF/raw-c1.png"
    ;;
  look)
    # Everything, including the baseline, comes through the virtual camera so
    # the only difference between captures is the look itself. Comparing
    # against the raw C1 instead would fold the virtual camera's own scaling
    # and sharpening into the measurement.
    if [[ -z "${2:-}" ]]; then
      echo "Usage: $0 look off|G1|D1|Q1|S1|X1|chrome|..." >&2
      exit 1
    fi
    echo "Capturing Opal Composer virtual cam as look-${look}.png"
    echo "(select '${look}' in the Composer UI first; change nothing else)"
    "$BIN" "Opal Composer" "$REF/look-${look}.png"
    ;;
  composer)
    echo "Capturing Opal Composer virtual cam as composer-${look}.png"
    "$BIN" "Opal Composer" "$REF/composer-${look}.png"
    ;;
  *)
    echo "Usage: $0 look <name> | raw | composer [look-name]"
    echo "See docs/reference-capture.md - the baseline 'look off' is required."
    exit 1
    ;;
esac
