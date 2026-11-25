#!/usr/bin/env bash
# BasaltLedger end-to-end demo: records readings, estimates ages, draws the
# stratigraphic chart, exports the plotted points and bulk-loads a CSV.
#
# Non-interactive, no configuration, no network. Everything is written to a
# throwaway directory that is removed on exit.
#
# Run with: bash demo.sh

set -euo pipefail
cd "$(dirname "$0")"

PYTHON="${PYTHON:-python3}"
CLI="$PWD/basaltledger.py"
WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' EXIT
LEDGER="$WORKDIR/basaltledger.json"

days_ago() {
    "$PYTHON" - "$1" <<'PY'
import datetime
import sys

print((datetime.date.today() - datetime.timedelta(days=int(sys.argv[1]))).isoformat())
PY
}

run() {
    printf '\n$ basaltledger %s\n' "$*"
    "$PYTHON" "$CLI" --data "$LEDGER" "$@"
}

W70="$(days_ago 70)"
W56="$(days_ago 56)"
W42="$(days_ago 42)"
W21="$(days_ago 21)"

printf 'BasaltLedger demo — ledger: %s\n' "$LEDGER"

run init
run add --artifact AR-7 --site Camas --unit EU-3 --depth 40 --hydration 3.1 --date "$W70"
run add --artifact AR-7 --site Camas --unit EU-3 --depth 45 --hydration 3.4 --date "$W56"
run add --artifact AR-9 --site Camas --unit EU-3 --depth 52 --hydration 3.9 --date "$W42"
run add --artifact AR-12 --site Camas --unit EU-4 --depth 35 --hydration 2.6 --date "$W21"
run list
run show --artifact AR-7
run estimate
run estimate --format csv
run chart --width 60
run export --format csv --out "$WORKDIR/points.csv"

printf '\n--- %s ---\n' "$WORKDIR/points.csv"
cat "$WORKDIR/points.csv"

run import "$PWD/examples/readings.csv"
run list
run rate set --rate 0.62 --note 'Camas obsidian curve, 2024 calibration'
run estimate --artifact AR-9

printf '\nDemo complete. Nothing was written outside %s\n' "$WORKDIR"
