#!/bin/bash
# Double-click me on macOS (or run me on Linux).
cd "$(dirname "$0")" || exit 1

PY="$(command -v python3 || command -v python)"
if [ -z "$PY" ]; then
  echo
  echo "  Python 3 is needed and was not found."
  echo "  macOS: install it from https://www.python.org/downloads/"
  echo "  Linux: sudo apt install python3 python3-tk"
  echo
  read -r -p "Press Enter to close "
  exit 1
fi

"$PY" ./gtnh-prism-update.py --setup "$@"
STATUS=$?
echo
read -r -p "Press Enter to close "
exit $STATUS
