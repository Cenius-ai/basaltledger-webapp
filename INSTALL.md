# Install and run BasaltLedger

BasaltLedger is a single Python file that uses only the standard library. There
is no build step, no service to configure, no database server and no network
access at any point.

## 1. Prerequisites (installed by you, not by this project)

| Requirement | Notes |
| --- | --- |
| **Python 3.9 or newer** | `python3 --version` must print 3.9+. Python 3.11 is what the project was developed and tested on. |
| A POSIX shell | Only needed for `install.sh` and `demo.sh`; the tool itself is plain `python3`. |

Nothing else is required: no compiler, no database, no `libsqlite3`, no system
packages. `install.sh` never uses `sudo`, `apt`, `brew` or any other system
package manager.

## 2. Install the project dependencies (one command, then it exits)

```bash
bash install.sh
```

`install.sh` does exactly three things and then returns:

1. refreshes `pip`/`setuptools`/`wheel` when the machine has network access
   (and continues with the installed tooling when it does not);
2. installs `requirements.txt`, which pins **only the test runner** (`pytest`) —
   the tool itself needs nothing;
3. runs a self-check: `python3 -c "import basaltledger"` and
   `python3 basaltledger.py --version`, so a broken environment fails loudly
   here instead of at first use.

It is idempotent: re-running it is harmless. It **never starts the program** and
never seeds anything — creating a ledger is an explicit `init` you run yourself.

If you would rather not install anything at all, skip this step:
`python3 basaltledger.py --help` works immediately.

## 3. Create a ledger (the project's "seed" step)

```bash
python3 basaltledger.py init
```

This writes `./basaltledger.json` holding the documented default baseline rate
of **0.50 µm/√yr** and the note
`uncalibrated default — replace with a site-derived rate before relying on ages`.
Nothing is downloaded and no readings are invented for you.

To start from the bundled sample data instead (4 readings across 3 artifacts):

```bash
python3 basaltledger.py init
python3 basaltledger.py import examples/readings.csv
```

## 4. Run

```bash
python3 basaltledger.py --help              # every command, its flags and the exit codes
python3 basaltledger.py add --artifact AR-7 --site Camas --unit EU-3 \
    --depth 42.5 --hydration 3.1 --date 2024-05-06
python3 basaltledger.py list
python3 basaltledger.py show --artifact AR-7
python3 basaltledger.py estimate
python3 basaltledger.py chart --width 60
python3 basaltledger.py export --format csv --out points.csv
```

Or run the whole tour non-interactively (it uses a throwaway directory and
cleans up after itself):

```bash
bash demo.sh
```

There is no server and no port: BasaltLedger is a terminal program.

## 5. Test

```bash
python3 -m pytest
```

The suite drives the real CLI end to end (no network, no external services, no
fixtures to download) and takes about a minute, most of which is the
10,000-reading performance check.

## Configuration

Every setting has a working default, so no `.env` file is needed. The two
environment variables the tool reads are documented in
[.env.example](.env.example):

| Variable | Default | Meaning |
| --- | --- | --- |
| `BASALTLEDGER_DATA` | `./basaltledger.json` | Ledger file to read and write (`--data PATH` overrides it). |
| `BASALTLEDGER_PASSPHRASE` | *(empty)* | Passphrase for a ledger created with `init --passphrase`; supplying it avoids the interactive prompt. |

`NO_COLOR` (any non-empty value) disables ANSI colour, as does `--no-color`.

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| `error: no ledger at … — run basaltledger init first` | Run `python3 basaltledger.py init`. |
| `error: ledger already exists … pass --force` | `init` refuses to wipe a ledger. Use `--force` only when you mean to discard every reading. |
| `error: no baseline rate — run basaltledger init --rate V` | The ledger's rate was set to 0 by hand; `rate show` to inspect, `rate set --rate V` to fix. |
| `error: this ledger is passphrase-protected …` | Set `BASALTLEDGER_PASSPHRASE`, or run the command in a terminal to get the prompt. |
| `error: ledger … is not valid JSON` | The file was hand-edited incorrectly; restore your backup or re-create the ledger and re-import from CSV. |
