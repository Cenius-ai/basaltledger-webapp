#!/usr/bin/env bash
# Install BasaltLedger's project-level dependencies, then exit.
#
# BasaltLedger itself is standard-library only: this script installs nothing the
# tool needs to run. It pins the test runner used by the suite.
#
# System prerequisites (a Python 3.9+ interpreter) are listed in INSTALL.md and
# are never installed here. This script does not start the program.

set -euo pipefail
cd "$(dirname "$0")"

PYTHON="${PYTHON:-python3}"

"$PYTHON" --version

if "$PYTHON" -c "import pytest" >/dev/null 2>&1; then
    echo "pytest already available — no project dependency to install"
else
    # Refresh the packaging tooling first so an install cannot fail silently.
    # A sandbox without network access simply keeps the installed tooling.
    if ! "$PYTHON" -m pip install --quiet --upgrade pip setuptools wheel; then
        echo "note: could not refresh pip/setuptools/wheel (offline?) — using installed tooling"
    fi
    "$PYTHON" -m pip install --quiet -r requirements.txt
fi

# Self-check: an import error must fail here, not at first use.
"$PYTHON" -c "import basaltledger"
"$PYTHON" basaltledger.py --version

echo
echo "install complete"
echo "  run    : python3 basaltledger.py --help"
echo "  demo   : bash demo.sh"
echo "  tests  : python3 -m pytest"
