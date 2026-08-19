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
  composer)
    echo "Capturing Opal Composer virtual cam as composer-${look}.png"
    echo "(Composer must be open; select the look in the UI first)"
    "$BIN" "Opal Composer" "$REF/composer-${look}.png"
    ;;
  *)
    echo "Usage: $0 raw | composer [look-name]"
    exit 1
    ;;
esac
