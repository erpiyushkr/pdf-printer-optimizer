#!/bin/bash
# ==========================================================
#  PDF Printer Optimizer - macOS / Linux launcher
#  Double-click in Finder (or run ./run.command in Terminal).
#  Creates a private Python environment (.venv), installs
#  missing packages, processes every PDF in "input" and keeps
#  the window open so the summary can be read.
# ==========================================================
cd "$(dirname "$0")" || exit 1

pause_exit() { echo; read -r -p "Press Enter to close this window..." _; exit "${1:-0}"; }

PY=""
for cand in python3.13 python3.12 python3.11 python3.10 python3; do
    if command -v "$cand" >/dev/null 2>&1; then PY="$cand"; break; fi
done
if [ -z "$PY" ]; then
    echo "Python 3 was not found. Install it from https://www.python.org/downloads/"
    pause_exit 1
fi

if [ ! -x ".venv/bin/python" ]; then
    echo "Creating the Python environment (first run only)..."
    "$PY" -m venv --clear .venv || { echo "Could not create the Python environment."; pause_exit 1; }
fi
# shellcheck disable=SC1091
source ".venv/bin/activate"

if ! python -c "import fitz, cv2, numpy, PIL, pypdf" >/dev/null 2>&1; then
    echo "Installing required packages (first run only, needs internet)..."
    python -m pip install --upgrade pip && python -m pip install -r requirements.txt \
        || { echo "Package installation failed. Check your internet connection."; pause_exit 1; }
fi

python main.py "$@"
pause_exit $?
