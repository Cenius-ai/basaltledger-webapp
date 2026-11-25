# Using BasaltLedger

A task-by-task walkthrough of the commands, with the output each one produces.
All examples assume you are in the project directory and have run
`python3 basaltledger.py init` once.

## Keep a weekly series

```bash
python3 basaltledger.py add --artifact AR-7 --site Camas --unit EU-3 \
    --depth 40 --hydration 3.1 --date 2024-05-06
python3 basaltledger.py add --artifact AR-7 --site Camas --unit EU-3 \
    --depth 45 --hydration 3.4 --date 2024-06-13
```

The first reading echoes the effective rate and says a second reading is needed
before an implied rate exists. From the second reading on, `add` also prints the
implied rate of every reading in the series
(`hydration_um / √age_years`, with 365.25 days per year and 52.1775 weeks per
year).

* `--date` defaults to today; future dates are rejected.
* `--depth` is the artifact's depth, frozen when the artifact is created.
  `--depth-cm` records a different depth for one reading (a series measured
  down a single block).
* Reusing a label with a different `--site`, `--unit` or `--depth` still records
  the reading, and prints a warning naming each supplied value that was ignored
  for the artifact.
* Validation errors name the field, write nothing, and exit `2`.

## Inspect what you have

```bash
python3 basaltledger.py list                     # one line per artifact + totals
python3 basaltledger.py list --format csv        # same rows, pipeable
python3 basaltledger.py show --artifact AR-7     # that series, oldest first
python3 basaltledger.py show --artifact AR-7 --format csv
```

`list` shows the artifact's *single* stored `depth_cm` (there are no depth bands).
An unknown label in `show` exits `2` and lists the labels that do exist.

## Estimate surface ages

```bash
python3 basaltledger.py estimate
python3 basaltledger.py estimate --artifact AR-7
python3 basaltledger.py estimate --format csv
```

`age_years = (hydration_um / rate)²`, `age_weeks = age_years × 52.1775`. The
table is grouped by artifact, followed by a per-artifact summary
(count / min / mean / max), a ledger totals line, and a footer repeating the
rate, the date it was set and its calibration note.

`estimate --format csv` writes the export columns plus a final `age_weeks`
column, so `estimate --format csv` and `export --format csv` diff cleanly.

## Draw the stratigraphic chart

```bash
python3 basaltledger.py chart
python3 basaltledger.py chart --width 60
python3 basaltledger.py chart --site Camas --unit EU-3
python3 basaltledger.py chart --width 80 --out chart.txt
```

One block per site/unit; shallowest reading at the top; exactly one `*` per
reading at the column proportional to its age; a legend stating
`1 column = X years`, the width used and the maximum age. `--out PATH` writes
exactly the text printed to stdout and echoes the path on stderr. A ledger with
a single reading renders one row plus a note that a chart needs more points to
be stratigraphically meaningful.

## Export the plotted points

```bash
python3 basaltledger.py export --format csv --out points.csv
python3 basaltledger.py export --format csv > points.csv
python3 basaltledger.py export --format csv --site Camas | head -5
```

Header: `artifact_label,site,unit,depth_cm,measured_on,hydration_um,effective_rate_um_sqrtyr,age_years`.
One row per reading, in the same order the chart draws its symbols
(`site, unit, depth_cm, measured_on, id`), so the two agree point for point.
Labels, sites and units containing commas or quotes are quoted correctly by the
standard `csv` writer; output is UTF-8 with `\n` line endings.

## Bulk-load a CSV

```bash
python3 basaltledger.py import examples/readings.csv
```

Fixed header: `artifact,site,unit,depth_cm,hydration_um,measured_on`.

* Wrong header → one line naming the expected header, exit `2`, nothing written.
* Any bad row → every failing line number and field listed, **nothing** written,
  exit `1`. There is no partial import mode by design.
* Existing labels are reused; conflicting site/unit/depth values are reported as
  warnings and never overwrite the stored artifact.
* Every imported reading is stored with `source='import'`.
* Summary: rows read, readings added, artifacts created.

## Manage the baseline rate

```bash
python3 basaltledger.py rate show
python3 basaltledger.py rate set --rate 0.62 --note 'Camas obsidian curve, 2024 calibration'
python3 basaltledger.py init --rate 0.62 --note 'Camas obsidian curve'
```

`rate show` prints the rate in µm/√yr and µm/√week, the note and the date it was
set. `rate set` echoes the previous and the new rate, so a recalibration is
visible in the transcript. A rate that is not a finite number greater than 0
(`0`, `-1`, `abc`, `nan`, `inf`) is refused with
`rate must be a number greater than 0` and the stored rate is unchanged.

`init` on an existing ledger exits `2` and keeps every reading; `--force`
replaces the ledger and discards it.

## Protect the ledger with a passphrase

```bash
python3 basaltledger.py init --passphrase          # prompts twice, no echo
BASALTLEDGER_PASSPHRASE='…' python3 basaltledger.py list
```

Only a random 16-byte salt and a PBKDF2-HMAC-SHA256 hash (200,000 iterations)
are stored — never the passphrase. Every command is gated; a wrong passphrase
prints exactly one line (`wrong passphrase`) and exits `3`. Ledgers created
without `--passphrase` never prompt. The gate does **not** encrypt the JSON.

## Correct a mistake

There is no edit or delete command. Stop the tool, hand-edit
`basaltledger.json` (fix the `hydration_um`, `measured_on` or `depth_cm` of the
offending reading), then re-run `estimate`. Every write is atomic, so the file is
never half-written.

## Piping and scripting

* Results on stdout, diagnostics on stderr: `export --format csv | head` stays clean.
* Exit codes: `0` ok, `1` import rows rejected, `2` usage/validation, `3` wrong passphrase.
* `NO_COLOR=1` (or `--no-color`) disables ANSI colour; colour is emitted only on
  a TTY, so piped output is plain text.
