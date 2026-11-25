#!/usr/bin/env python3
"""BasaltLedger — a local ledger of weekly obsidian hydration readings.

One file, standard library only.  It records hydration rim depths sampled week
by week, converts them into surface ages with one ledger-wide baseline rate,
and publishes the result as an ASCII stratigraphic chart plus a CSV of the
plotted points.

Run `python3 basaltledger.py --help` for the command list.
"""

from __future__ import annotations

import argparse
import contextlib
import csv
import getpass
import hashlib
import hmac
import json
import math
import os
import sys
import tempfile
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

__version__ = "1.0.0"

# --- Baseline rate model -----------------------------------------------------

DEFAULT_RATE_UM_SQRTYR = 0.50
DEFAULT_RATE_NOTE = (
    "uncalibrated default — replace with a site-derived rate before relying on ages"
)
CUSTOM_RATE_NOTE = "custom baseline supplied with --rate (no calibration note given)"
DAYS_PER_YEAR = 365.25
WEEKS_PER_YEAR = 52.1775
SCHEMA_VERSION = 1

# --- Storage / gate ----------------------------------------------------------

DEFAULT_DATA_FILE = "basaltledger.json"
ENV_DATA = "BASALTLEDGER_DATA"
ENV_PASSPHRASE = "BASALTLEDGER_PASSPHRASE"
PBKDF2_ITERATIONS = 200_000
SALT_BYTES = 16

# --- Presentation ------------------------------------------------------------

DEFAULT_CHART_WIDTH = 48
MIN_CHART_WIDTH = 8
MAX_CHART_WIDTH = 200
RULE = "─"
PLOT = "*"

# Design tokens (see README "Design tokens"): accent oklch(0.58 0.12 266)
# resolved to hex for the terminal, which cannot parse oklch().
ACCENT_HEX = "#5977c1"
ACCENT_RGB = (89, 119, 193)

# --- Column contracts --------------------------------------------------------

EXPORT_HEADER = [
    "artifact_label",
    "site",
    "unit",
    "depth_cm",
    "measured_on",
    "hydration_um",
    "effective_rate_um_sqrtyr",
    "age_years",
]
ESTIMATE_HEADER = EXPORT_HEADER + ["age_weeks"]
IMPORT_HEADER = ["artifact", "site", "unit", "depth_cm", "hydration_um", "measured_on"]

# --- Exit codes --------------------------------------------------------------

EXIT_OK = 0
EXIT_IMPORT_REJECTED = 1
EXIT_USAGE = 2
EXIT_PASSPHRASE = 3

_USE_COLOR = False


class CliError(Exception):
    """A plain-language failure with a documented process exit code."""

    def __init__(
        self, message: str, code: int = EXIT_USAGE, details: Sequence[str] = ()
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.details: List[str] = list(details)


# --- Output helpers ----------------------------------------------------------


def eprint(*parts: str) -> None:
    print(*parts, file=sys.stderr)


def accent(text: str) -> str:
    if not _USE_COLOR:
        return text
    red, green, blue = ACCENT_RGB
    return "\033[38;2;{};{};{}m{}\033[0m".format(red, green, blue, text)


def color_enabled(no_color_flag: bool) -> bool:
    if no_color_flag or os.environ.get("NO_COLOR"):
        return False
    return sys.stdout.isatty()


def plural(count: int, singular: str, many: Optional[str] = None) -> str:
    word = singular if count == 1 else (many or singular + "s")
    return "{} {}".format(count, word)


def fmt_rate(value: float) -> str:
    """Two decimals for ordinary rates, four for values needing the detail."""
    if abs(value - round(value, 2)) < 1e-9:
        return "{:.2f}".format(value)
    return "{:.4f}".format(value)


def fmt_years(value: float) -> str:
    return "{:.2f}".format(value)


def fmt_weeks(value: float) -> str:
    return "{:.0f}".format(value)


def fmt_cm(value: float) -> str:
    return "{:.1f}".format(value)


def fmt_num(value: float) -> str:
    """Deterministic, round-trippable number text for CSV cells."""
    text = "{:.6f}".format(value).rstrip("0").rstrip(".")
    return text or "0"


def rate_week_equivalent(rate_um_sqrtyr: float) -> float:
    return rate_um_sqrtyr / math.sqrt(WEEKS_PER_YEAR)


def rate_line(rate_um_sqrtyr: float) -> str:
    return "{} µm/√yr ({} µm/√week)".format(
        fmt_rate(rate_um_sqrtyr), fmt_rate(rate_week_equivalent(rate_um_sqrtyr))
    )


def age_years_for(hydration_um: float, rate_um_sqrtyr: float) -> float:
    return (hydration_um / rate_um_sqrtyr) ** 2


def age_days(measured_on: str, today: date) -> int:
    return (today - date.fromisoformat(measured_on)).days


def implied_rate(hydration_um: float, measured_on: str, today: date) -> Optional[float]:
    """hydration_um / sqrt(age_years); None when the reading is dated today."""
    days = age_days(measured_on, today)
    if days <= 0:
        return None
    years = days / DAYS_PER_YEAR
    return hydration_um / math.sqrt(years)


@dataclass(frozen=True)
class Column:
    header: str
    align: str = "left"


def render_table(columns: Sequence[Column], rows: Sequence[Sequence[str]]) -> str:
    widths = [len(col.header) for col in columns]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))

    def format_row(cells: Sequence[str]) -> str:
        padded = [
            cell.rjust(widths[i]) if columns[i].align == "right" else cell.ljust(widths[i])
            for i, cell in enumerate(cells)
        ]
        return "  ".join(padded).rstrip()

    lines = [format_row([col.header for col in columns])]
    lines.append(RULE * (sum(widths) + 2 * (len(columns) - 1)))
    lines.extend(format_row(row) for row in rows)
    return "\n".join(lines)


def title_line(title: str, note: str = "") -> str:
    head = "BasaltLedger · {}".format(title)
    return "{}   {}".format(accent(head), note).rstrip()


# --- Validation --------------------------------------------------------------


def parse_required_text(raw: Optional[str], field_name: str) -> Tuple[Optional[str], List[str]]:
    text = (raw or "").strip()
    if not text:
        return None, ["{} must not be empty".format(field_name)]
    return text, []


def parse_number(
    raw: Optional[str],
    field_name: str,
    *,
    minimum: float,
    exclusive: bool,
) -> Tuple[Optional[float], List[str]]:
    bound = "greater than {}{}".format(
        "" if exclusive else "or equal to ", fmt_num(minimum)
    )
    text = (raw or "").strip()
    if not text:
        return None, ["{} must not be empty".format(field_name)]
    try:
        value = float(text)
    except ValueError:
        return None, [
            "{} must be a number {}, got {!r}".format(field_name, bound, text)
        ]
    if not math.isfinite(value):
        return None, [
            "{} must be a finite number {}, got {!r}".format(field_name, bound, text)
        ]
    if exclusive and value <= minimum:
        return None, [
            "{} must be a number {}, got {!r}".format(field_name, bound, text)
        ]
    if not exclusive and value < minimum:
        return None, [
            "{} must be a number {}, got {!r}".format(field_name, bound, text)
        ]
    return value, []


def parse_rate(raw: Optional[str]) -> Tuple[Optional[float], List[str]]:
    text = (raw or "").strip()
    if not text:
        return None, ["rate must not be empty"]
    try:
        value = float(text)
    except ValueError:
        return None, ["rate must be a number greater than 0 (got {!r})".format(text)]
    if not math.isfinite(value) or value <= 0:
        return None, ["rate must be a number greater than 0 (got {!r})".format(text)]
    return value, []


def parse_measured_on(raw: Optional[str], today: date) -> Tuple[Optional[str], List[str]]:
    text = (raw or "").strip()
    if not text:
        return today.isoformat(), []
    try:
        parsed = date.fromisoformat(text)
    except ValueError:
        return None, [
            "measured_on must be an ISO-8601 date (YYYY-MM-DD), got {!r}".format(text)
        ]
    if parsed > today:
        return None, [
            "measured_on must not be in the future ({}), today is {}".format(
                parsed.isoformat(), today.isoformat()
            )
        ]
    return parsed.isoformat(), []


# --- Ledger store ------------------------------------------------------------


class LedgerStore:
    """One JSON document, read whole and written atomically."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def exists(self) -> bool:
        return self.path.exists()

    def check_target(self) -> None:
        """Refuse a directory where the ledger file should be."""
        if self.path.is_dir():
            raise CliError(
                "ledger path {} is a directory — give --data a file path".format(self.path)
            )

    def load(self) -> Dict[str, Any]:
        self.check_target()
        if not self.exists():
            raise CliError(
                "no ledger at {} — run basaltledger init first".format(self.path)
            )
        try:
            raw = self.path.read_text(encoding="utf-8")
        except OSError as exc:
            raise CliError(
                "cannot read ledger {}: {}".format(self.path, exc.strerror or exc)
            ) from exc
        try:
            ledger = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise CliError(
                "ledger {} is not valid JSON ({} at line {}) — restore it from a "
                "backup or hand-edit the file".format(self.path, exc.msg, exc.lineno)
            ) from exc
        validate_ledger_shape(ledger, self.path)
        return ledger

    def save(self, ledger: Dict[str, Any]) -> None:
        self.check_target()
        directory = self.path.parent if str(self.path.parent) else Path(".")
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise CliError(
                "cannot create directory {} for ledger {}: {}".format(
                    directory, self.path, exc.strerror or exc
                )
            ) from exc
        payload = json.dumps(ledger, indent=2, ensure_ascii=False) + "\n"
        temp_name: Optional[str] = None
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                newline="\n",
                dir=str(directory),
                prefix=".basaltledger-",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temp_name = handle.name
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, str(self.path))
        except OSError as exc:
            if temp_name is not None:
                with contextlib.suppress(OSError):
                    os.unlink(temp_name)
            raise CliError(
                "cannot write ledger {}: {}".format(self.path, exc.strerror or exc)
            ) from exc


def validate_ledger_shape(ledger: Any, path: Path) -> None:
    broken = "ledger {} is not a BasaltLedger document — expected schema_version, meta, artifacts and readings".format(path)
    if not isinstance(ledger, dict):
        raise CliError(broken)
    if not isinstance(ledger.get("meta"), dict):
        raise CliError(broken)
    if not isinstance(ledger.get("artifacts"), list):
        raise CliError(broken)
    if not isinstance(ledger.get("readings"), list):
        raise CliError(broken)


def resolve_store(args: argparse.Namespace) -> LedgerStore:
    raw = getattr(args, "data", None) or os.environ.get(ENV_DATA) or DEFAULT_DATA_FILE
    return LedgerStore(Path(raw).expanduser())


def next_id(items: Sequence[Dict[str, Any]]) -> int:
    return max((int(item.get("id", 0)) for item in items), default=0) + 1


def now_timestamp() -> str:
    return datetime.now().isoformat(timespec="seconds")


# --- Passphrase gate ---------------------------------------------------------


def hash_passphrase(passphrase: str, salt: bytes) -> str:
    return hashlib.pbkdf2_hmac(
        "sha256", passphrase.encode("utf-8"), salt, PBKDF2_ITERATIONS
    ).hex()


def ledger_is_gated(ledger: Dict[str, Any]) -> bool:
    meta = ledger.get("meta", {})
    return bool(meta.get("passphrase_salt_hex") and meta.get("passphrase_hash_hex"))


def prompt_secret(label: str) -> str:
    if not sys.stdin.isatty():
        raise CliError(
            "this ledger is passphrase-protected — set BASALTLEDGER_PASSPHRASE to run without a prompt",
            EXIT_PASSPHRASE,
        )
    try:
        return getpass.getpass(label)
    except (EOFError, KeyboardInterrupt) as exc:
        raise CliError("passphrase entry cancelled", EXIT_PASSPHRASE) from exc


def verify_gate(ledger: Dict[str, Any]) -> None:
    if not ledger_is_gated(ledger):
        return
    passphrase = os.environ.get(ENV_PASSPHRASE) or prompt_secret("passphrase: ")
    salt = bytes.fromhex(ledger["meta"]["passphrase_salt_hex"])
    expected = ledger["meta"]["passphrase_hash_hex"]
    if not hmac.compare_digest(hash_passphrase(passphrase, salt), expected):
        raise CliError("wrong passphrase", EXIT_PASSPHRASE)


def read_new_passphrase() -> str:
    from_env = os.environ.get(ENV_PASSPHRASE)
    if from_env:
        return from_env
    if not sys.stdin.isatty():
        raise CliError(
            "init --passphrase needs a passphrase — set BASALTLEDGER_PASSPHRASE or run it in a terminal",
            EXIT_USAGE,
        )
    first = prompt_secret("choose a passphrase: ")
    if not first:
        raise CliError("passphrase must not be empty", EXIT_USAGE)
    second = prompt_secret("confirm passphrase: ")
    if first != second:
        raise CliError("passphrases do not match — nothing written", EXIT_USAGE)
    return first


def gate_ledger(store: LedgerStore) -> Dict[str, Any]:
    ledger = store.load()
    verify_gate(ledger)
    return ledger


# --- Derived data ------------------------------------------------------------


@dataclass(frozen=True)
class PlottedPoint:
    artifact_id: int
    reading_id: int
    artifact_label: str
    site: str
    unit: str
    depth_cm: float
    measured_on: str
    hydration_um: float
    effective_rate_um_sqrtyr: float
    age_years: float

    def export_row(self) -> List[str]:
        return [
            self.artifact_label,
            self.site,
            self.unit,
            fmt_num(self.depth_cm),
            self.measured_on,
            fmt_num(self.hydration_um),
            fmt_num(self.effective_rate_um_sqrtyr),
            fmt_num(self.age_years),
        ]

    def estimate_row(self) -> List[str]:
        return self.export_row() + [fmt_num(self.age_years * WEEKS_PER_YEAR)]


def effective_rate(ledger: Dict[str, Any]) -> float:
    raw = ledger.get("meta", {}).get("rate_um_sqrtyr")
    try:
        rate = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        rate = 0.0
    if not math.isfinite(rate) or rate <= 0:
        raise CliError("no baseline rate — run basaltledger init --rate V")
    return rate


def artifact_by_label(ledger: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    return {str(item["label"]): item for item in ledger["artifacts"]}


def known_labels(ledger: Dict[str, Any]) -> List[str]:
    return sorted(str(item["label"]) for item in ledger["artifacts"])


def require_label(ledger: Dict[str, Any], label: str) -> Dict[str, Any]:
    found = artifact_by_label(ledger).get(label)
    if found is None:
        labels = known_labels(ledger)
        hint = (
            "hint: known labels: {}".format(", ".join(labels))
            if labels
            else "hint: no artifacts recorded yet — add one with basaltledger add"
        )
        raise CliError("unknown artifact label {!r}".format(label), EXIT_USAGE, [hint])
    return found


def build_points(
    ledger: Dict[str, Any],
    *,
    label: Optional[str] = None,
    site: Optional[str] = None,
    unit: Optional[str] = None,
) -> List[PlottedPoint]:
    """The single ordered point list shared by `chart` and `export`."""
    rate = effective_rate(ledger)
    artifacts = {int(item["id"]): item for item in ledger["artifacts"]}
    points: List[PlottedPoint] = []
    for reading in ledger["readings"]:
        artifact = artifacts.get(int(reading["artifact_id"]))
        if artifact is None:
            continue  # orphaned row from a hand edit; never crash the report
        if label is not None and str(artifact["label"]) != label:
            continue
        if site is not None and str(artifact["site"]) != site:
            continue
        if unit is not None and str(artifact["unit"]) != unit:
            continue
        hydration = float(reading["hydration_um"])
        points.append(
            PlottedPoint(
                artifact_id=int(artifact["id"]),
                reading_id=int(reading["id"]),
                artifact_label=str(artifact["label"]),
                site=str(artifact["site"]),
                unit=str(artifact["unit"]),
                depth_cm=float(reading["depth_cm"]),
                measured_on=str(reading["measured_on"]),
                hydration_um=hydration,
                effective_rate_um_sqrtyr=rate,
                age_years=age_years_for(hydration, rate),
            )
        )
    points.sort(
        key=lambda p: (p.site, p.unit, p.depth_cm, p.measured_on, p.reading_id)
    )
    return points


def series_readings(ledger: Dict[str, Any], artifact_id: int) -> List[Dict[str, Any]]:
    rows = [r for r in ledger["readings"] if int(r["artifact_id"]) == artifact_id]
    rows.sort(key=lambda r: (str(r["measured_on"]), int(r["id"])))
    return rows


def latest_reading(readings: Sequence[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not readings:
        return None
    return max(readings, key=lambda r: (str(r["measured_on"]), int(r["id"])))


def artifact_depth(artifact: Dict[str, Any]) -> float:
    return float(artifact["depth_cm"])


# --- Handlers ----------------------------------------------------------------


def cmd_init(args: argparse.Namespace) -> int:
    store = resolve_store(args)
    store.check_target()
    if store.exists() and not args.force:
        raise CliError(
            "ledger already exists at {} — pass --force to replace it (every stored "
            "reading would be lost)".format(store.path)
        )
    if store.exists():
        verify_gate(store.load())

    today = date.today()
    rate, problems = (
        parse_rate(args.rate) if args.rate is not None else (DEFAULT_RATE_UM_SQRTYR, [])
    )
    if problems:
        raise CliError(problems[0])
    assert rate is not None

    if args.note is not None and args.note.strip():
        note = args.note.strip()
    elif args.rate is None:
        note = DEFAULT_RATE_NOTE
    else:
        note = CUSTOM_RATE_NOTE

    meta: Dict[str, Any] = {
        "rate_um_sqrtyr": rate,
        "rate_note": note,
        "rate_set_at": today.isoformat(),
        "passphrase_salt_hex": None,
        "passphrase_hash_hex": None,
        "created_at": now_timestamp(),
    }
    if args.passphrase:
        passphrase = read_new_passphrase()
        salt = os.urandom(SALT_BYTES)
        meta["passphrase_salt_hex"] = salt.hex()
        meta["passphrase_hash_hex"] = hash_passphrase(passphrase, salt)

    ledger: Dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "meta": meta,
        "artifacts": [],
        "readings": [],
    }
    store.save(ledger)

    if args.force:
        print("ledger replaced at {}".format(store.path))
    else:
        print("ledger created at {}".format(store.path))
    print("baseline rate   {}".format(rate_line(rate)))
    print("note            {}".format(note))
    print("rate set on     {}".format(meta["rate_set_at"]))
    print(
        "passphrase      {}".format(
            "set (PBKDF2-HMAC-SHA256, {:,} iterations)".format(PBKDF2_ITERATIONS)
            if args.passphrase
            else "not set — every command runs without a prompt"
        )
    )
    if rate == DEFAULT_RATE_UM_SQRTYR and note == DEFAULT_RATE_NOTE:
        eprint(
            "warn: {} µm/√yr is the uncalibrated default — every age depends on it".format(
                fmt_rate(rate)
            )
        )
    print("next            basaltledger add --artifact AR-7 --site Camas --unit EU-3 --depth 42.5 --hydration 3.1")
    return EXIT_OK


def cmd_rate(args: argparse.Namespace) -> int:
    store = resolve_store(args)
    ledger = gate_ledger(store)
    meta = ledger["meta"]
    if args.rate_command == "show":
        return rate_show(store, ledger)
    return rate_set(args, store, ledger)


def rate_show(store: LedgerStore, ledger: Dict[str, Any]) -> int:
    meta = ledger["meta"]
    rate = meta.get("rate_um_sqrtyr")
    try:
        rate_value = float(rate)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise CliError("no baseline rate — run basaltledger init --rate V")
    print(title_line("baseline rate"))
    print("rate            {}".format(rate_line(rate_value)))
    print("note            {}".format(meta.get("rate_note", "")))
    print("rate set on     {}".format(meta.get("rate_set_at", "")))
    print("ledger          {}".format(store.path))
    print(
        "readings        {} across {}".format(
            plural(len(ledger["readings"]), "reading"),
            plural(len(ledger["artifacts"]), "artifact"),
        )
    )
    if meta.get("rate_note") == DEFAULT_RATE_NOTE:
        eprint(
            "warn: this ledger still uses the uncalibrated default — recalibrate with "
            "basaltledger rate set --rate V --note TEXT"
        )
    return EXIT_OK


def rate_set(args: argparse.Namespace, store: LedgerStore, ledger: Dict[str, Any]) -> int:
    new_rate, problems = parse_rate(args.rate)
    if problems:
        raise CliError(problems[0])
    assert new_rate is not None
    meta = ledger["meta"]
    previous, _ = parse_rate(str(meta.get("rate_um_sqrtyr")))
    meta["rate_um_sqrtyr"] = new_rate
    if args.note is not None and args.note.strip():
        meta["rate_note"] = args.note.strip()
    elif meta.get("rate_note") == DEFAULT_RATE_NOTE:
        meta["rate_note"] = CUSTOM_RATE_NOTE
    meta["rate_set_at"] = date.today().isoformat()
    store.save(ledger)

    print(title_line("baseline rate updated"))
    if previous is not None:
        print("previous rate   {}".format(rate_line(previous)))
    print("new rate        {}".format(rate_line(new_rate)))
    print("note            {}".format(meta.get("rate_note", "")))
    print("rate set on     {}".format(meta["rate_set_at"]))
    print(
        "affected        every age in the ledger ({}) is recomputed with {}".format(
            plural(len(ledger["readings"]), "reading"), fmt_rate(new_rate)
        )
    )
    return EXIT_OK


def collect_add_fields(args: argparse.Namespace, today: date) -> Dict[str, Any]:
    errors: List[str] = []
    label, problems = parse_required_text(args.artifact, "artifact")
    errors.extend(problems)
    site, problems = parse_required_text(args.site, "site")
    errors.extend(problems)
    unit, problems = parse_required_text(args.unit, "unit")
    errors.extend(problems)
    depth, problems = parse_number(args.depth, "depth_cm", minimum=0.0, exclusive=False)
    errors.extend(problems)
    hydration, problems = parse_number(
        args.hydration, "hydration_um", minimum=0.0, exclusive=True
    )
    errors.extend(problems)
    measured_on, problems = parse_measured_on(args.date, today)
    errors.extend(problems)
    if args.depth_cm is not None:
        reading_depth, problems = parse_number(
            args.depth_cm, "depth_cm (reading)", minimum=0.0, exclusive=False
        )
        errors.extend(problems)
    else:
        reading_depth = None
    if errors:
        raise CliError(
            "{} to fix — nothing was written".format(plural(len(errors), "field")),
            EXIT_USAGE,
            errors,
        )
    return {
        "label": label,
        "site": site,
        "unit": unit,
        "depth": depth,
        "hydration": hydration,
        "measured_on": measured_on,
        "reading_depth": reading_depth,
    }


def artifact_conflicts(artifact: Dict[str, Any], fields: Dict[str, Any]) -> List[str]:
    """Site, unit and depth are frozen at creation; name every supplied value we drop."""
    warnings: List[str] = []
    if str(artifact["site"]) != fields["site"]:
        warnings.append(
            "artifact {!r} keeps site {!r} — supplied site {!r} ignored".format(
                artifact["label"], artifact["site"], fields["site"]
            )
        )
    if str(artifact["unit"]) != fields["unit"]:
        warnings.append(
            "artifact {!r} keeps unit {!r} — supplied unit {!r} ignored".format(
                artifact["label"], artifact["unit"], fields["unit"]
            )
        )
    if abs(float(artifact["depth_cm"]) - float(fields["depth"])) > 1e-9:
        warnings.append(
            "artifact {!r} keeps depth_cm {} — supplied depth {} ignored for the "
            "artifact (this reading records its own depth)".format(
                artifact["label"],
                fmt_cm(float(artifact["depth_cm"])),
                fmt_cm(float(fields["depth"])),
            )
        )
    return warnings


def upsert_artifact(
    ledger: Dict[str, Any], fields: Dict[str, Any]
) -> Tuple[Dict[str, Any], bool, List[str]]:
    """Reuse an artifact by label, or create it.  Site/unit/depth are frozen."""
    existing = artifact_by_label(ledger).get(fields["label"])
    if existing is not None:
        return existing, False, artifact_conflicts(existing, fields)
    artifact = {
        "id": next_id(ledger["artifacts"]),
        "label": fields["label"],
        "site": fields["site"],
        "unit": fields["unit"],
        "depth_cm": fields["depth"],
        "created_at": now_timestamp(),
    }
    ledger["artifacts"].append(artifact)
    return artifact, True, []


def append_reading(
    ledger: Dict[str, Any],
    artifact: Dict[str, Any],
    *,
    hydration_um: float,
    depth_cm: float,
    measured_on: str,
    source: str,
    reading_id: Optional[int] = None,
) -> Dict[str, Any]:
    reading = {
        "id": reading_id if reading_id is not None else next_id(ledger["readings"]),
        "artifact_id": int(artifact["id"]),
        "hydration_um": hydration_um,
        "depth_cm": depth_cm,
        "measured_on": measured_on,
        "source": source,
        "created_at": now_timestamp(),
    }
    ledger["readings"].append(reading)
    return reading


def echo_implied_rates(
    ledger: Dict[str, Any], artifact: Dict[str, Any], today: date
) -> None:
    readings = series_readings(ledger, int(artifact["id"]))
    if len(readings) < 2:
        print(
            "implied rate    —  (a second reading is needed before an implied rate exists)"
        )
        return
    print("implied rate    computed from hydration_um / sqrt(age_years), one line per reading")
    for reading in readings:
        value = implied_rate(float(reading["hydration_um"]), str(reading["measured_on"]), today)
        if value is None:
            print(
                "  {}  {} µm  dated today — age_years is 0, so no implied rate".format(
                    reading["measured_on"], fmt_num(float(reading["hydration_um"]))
                )
            )
            continue
        days = age_days(str(reading["measured_on"]), today)
        print(
            "  {}  {} µm  age {:.3f} yr ({:,} d)  implied {} µm/√yr ({} µm/√week)".format(
                reading["measured_on"],
                fmt_num(float(reading["hydration_um"])),
                days / DAYS_PER_YEAR,
                days,
                fmt_rate(value),
                fmt_rate(rate_week_equivalent(value)),
            )
        )


def cmd_add(args: argparse.Namespace) -> int:
    today = date.today()
    fields = collect_add_fields(args, today)
    store = resolve_store(args)
    ledger = gate_ledger(store)
    rate = effective_rate(ledger)

    artifact, created, warnings = upsert_artifact(ledger, fields)
    reading_depth = (
        fields["reading_depth"]
        if fields["reading_depth"] is not None
        else float(fields["depth"])
    )
    reading = append_reading(
        ledger,
        artifact,
        hydration_um=float(fields["hydration"]),
        depth_cm=reading_depth,
        measured_on=str(fields["measured_on"]),
        source="add",
    )
    store.save(ledger)

    for warning in warnings:
        eprint("warn: {}".format(warning))
    count = len(series_readings(ledger, int(artifact["id"])))
    print(title_line("reading recorded"))
    print("artifact        {} · {} · {} · {} cm{}".format(
        artifact["label"],
        artifact["site"],
        artifact["unit"],
        fmt_cm(float(artifact["depth_cm"])),
        "  (new artifact)" if created else "",
    ))
    print(
        "reading #{}      {} µm at {} cm, measured {}, source {}".format(
            reading["id"],
            fmt_num(reading["hydration_um"]),
            fmt_cm(reading["depth_cm"]),
            reading["measured_on"],
            reading["source"],
        )
    )
    print("series          {}".format(plural(count, "reading")))
    print("effective rate  {}".format(rate_line(rate)))
    echo_implied_rates(ledger, artifact, today)
    return EXIT_OK


def latest_age_text(ledger: Dict[str, Any], reading: Optional[Dict[str, Any]]) -> str:
    if reading is None:
        return "—"
    try:
        rate = effective_rate(ledger)
    except CliError:
        return "n/a"
    return fmt_years(age_years_for(float(reading["hydration_um"]), rate))


def cmd_list(args: argparse.Namespace) -> int:
    store = resolve_store(args)
    ledger = gate_ledger(store)
    artifacts = sorted(
        ledger["artifacts"], key=lambda a: (str(a["site"]), str(a["unit"]), str(a["label"]))
    )
    total_readings = len(ledger["readings"])

    if not artifacts:
        print(title_line("artifacts"))
        print("no artifacts yet — add one with:")
        print(
            "  basaltledger add --artifact AR-7 --site Camas --unit EU-3 --depth 42.5 "
            "--hydration 3.1 --date {}".format(date.today().isoformat())
        )
        print("totals          0 artifacts · {} readings".format(total_readings))
        return EXIT_OK

    rows: List[List[str]] = []
    for artifact in artifacts:
        readings = series_readings(ledger, int(artifact["id"]))
        latest = latest_reading(readings)
        rows.append(
            [
                str(artifact["label"]),
                str(artifact["site"]),
                str(artifact["unit"]),
                fmt_cm(artifact_depth(artifact)),
                str(len(readings)),
                fmt_num(float(latest["hydration_um"])) if latest else "—",
                latest_age_text(ledger, latest),
            ]
        )

    if args.format == "csv":
        writer = csv.writer(sys.stdout, lineterminator="\n")
        writer.writerow(
            [
                "label",
                "site",
                "unit",
                "depth_cm",
                "readings",
                "latest_hydration_um",
                "latest_age_years",
            ]
        )
        writer.writerows(rows)
        eprint(
            "totals          {} · {}".format(
                plural(len(artifacts), "artifact"),
                plural(total_readings, "reading"),
            )
        )
        return EXIT_OK

    try:
        rate_text = rate_line(effective_rate(ledger))
    except CliError:
        rate_text = "no baseline rate (run basaltledger init --rate V)"
    print(title_line("artifacts", "baseline {}".format(rate_text)))
    print(
        render_table(
            [
                Column("label"),
                Column("site"),
                Column("unit"),
                Column("depth_cm", "right"),
                Column("readings", "right"),
                Column("latest_µm", "right"),
                Column("latest_age_yr", "right"),
            ],
            rows,
        )
    )
    print(
        "totals          {} · {}".format(
            plural(len(artifacts), "artifact"), plural(total_readings, "reading")
        )
    )
    return EXIT_OK


def cmd_show(args: argparse.Namespace) -> int:
    store = resolve_store(args)
    ledger = gate_ledger(store)
    artifact = require_label(ledger, args.artifact)
    readings = series_readings(ledger, int(artifact["id"]))
    rate: Optional[float]
    try:
        rate = effective_rate(ledger)
    except CliError:
        rate = None

    rows: List[List[str]] = []
    for reading in readings:
        age_text = (
            fmt_years(age_years_for(float(reading["hydration_um"]), rate))
            if rate is not None
            else "n/a"
        )
        rows.append(
            [
                str(reading["measured_on"]),
                fmt_num(float(reading["hydration_um"])),
                fmt_cm(float(reading["depth_cm"])),
                str(reading["source"]),
                age_text,
            ]
        )

    if args.format == "csv":
        writer = csv.writer(sys.stdout, lineterminator="\n")
        writer.writerow(["measured_on", "hydration_um", "depth_cm", "source", "age_years"])
        writer.writerows(rows)
        eprint("{} readings for {}".format(len(readings), artifact["label"]))
        return EXIT_OK

    header = "{} · {} · {} · {} cm".format(
        artifact["label"],
        artifact["site"],
        artifact["unit"],
        fmt_cm(artifact_depth(artifact)),
    )
    print(title_line("reading series", header))
    print(
        render_table(
            [
                Column("measured_on"),
                Column("hydration_µm", "right"),
                Column("depth_cm", "right"),
                Column("source"),
                Column("age_years", "right"),
            ],
            rows,
        )
    )
    if readings:
        print(
            "series          {} · first {} · latest {}".format(
                plural(len(readings), "reading"),
                readings[0]["measured_on"],
                readings[-1]["measured_on"],
            )
        )
    else:
        print("series          0 readings — add one with:")
        print(
            "  basaltledger add --artifact {label} --site {site} --unit {unit} --depth "
            "{depth} --hydration 3.1".format(
                label=artifact["label"],
                site=artifact["site"],
                unit=artifact["unit"],
                depth=fmt_cm(artifact_depth(artifact)),
            )
        )
    return EXIT_OK


def summary_rows(points: Sequence[PlottedPoint]) -> List[List[str]]:
    grouped: Dict[str, List[float]] = {}
    for point in points:
        grouped.setdefault(point.artifact_label, []).append(point.age_years)
    rows: List[List[str]] = []
    for label in sorted(grouped):
        ages = grouped[label]
        rows.append(
            [
                label,
                str(len(ages)),
                fmt_years(min(ages)),
                fmt_years(sum(ages) / len(ages)),
                fmt_years(max(ages)),
            ]
        )
    return rows


def cmd_estimate(args: argparse.Namespace) -> int:
    store = resolve_store(args)
    ledger = gate_ledger(store)
    if args.artifact is not None:
        require_label(ledger, args.artifact)
    rate = effective_rate(ledger)
    points = build_points(ledger, label=args.artifact)
    if not points:
        raise CliError(
            "no readings to estimate — add one with basaltledger add or bulk-load "
            "with basaltledger import PATH"
        )

    if args.format == "csv":
        writer = csv.writer(sys.stdout, lineterminator="\n")
        writer.writerow(ESTIMATE_HEADER)
        writer.writerows(point.estimate_row() for point in points)
        eprint(
            "{} · baseline {} µm/√yr · note: {}".format(
                plural(len(points), "reading"),
                fmt_rate(rate),
                ledger["meta"].get("rate_note", ""),
            )
        )
        return EXIT_OK

    print(
        title_line(
            "surface ages",
            "baseline {} · {}".format(
                rate_line(rate), plural(len(points), "reading")
            ),
        )
    )
    print(
        render_table(
            [
                Column("label"),
                Column("site"),
                Column("unit"),
                Column("depth_cm", "right"),
                Column("measured_on"),
                Column("hydration_µm", "right"),
                Column("rate", "right"),
                Column("age_years", "right"),
                Column("age_weeks", "right"),
            ],
            [
                [
                    point.artifact_label,
                    point.site,
                    point.unit,
                    fmt_cm(point.depth_cm),
                    point.measured_on,
                    fmt_num(point.hydration_um),
                    fmt_rate(point.effective_rate_um_sqrtyr),
                    fmt_years(point.age_years),
                    fmt_weeks(point.age_years * WEEKS_PER_YEAR),
                ]
                for point in points
            ],
        )
    )
    print("")
    print("summary per artifact")
    summaries = summary_rows(points)
    print(
        render_table(
            [
                Column("label"),
                Column("count", "right"),
                Column("min_age_yr", "right"),
                Column("mean_age_yr", "right"),
                Column("max_age_yr", "right"),
            ],
            summaries,
        )
    )
    mean_age = sum(p.age_years for p in points) / len(points)
    print(
        "totals          {} · {} · mean age {} yr".format(
            plural(len(points), "reading"),
            plural(len(summaries), "artifact"),
            fmt_years(mean_age),
        )
    )
    print(
        "rate            {} · set {} · {}".format(
            rate_line(rate),
            ledger["meta"].get("rate_set_at", ""),
            ledger["meta"].get("rate_note", ""),
        )
    )
    return EXIT_OK


def chart_column(age: float, max_age: float, width: int) -> int:
    if max_age <= 0 or width <= 1:
        return 0
    index = int(round((age / max_age) * (width - 1)))
    return max(0, min(width - 1, index))


def group_points(points: Sequence[PlottedPoint]) -> List[Tuple[Tuple[str, str], List[PlottedPoint]]]:
    grouped: Dict[Tuple[str, str], List[PlottedPoint]] = {}
    for point in points:
        grouped.setdefault((point.site, point.unit), []).append(point)
    return [(key, grouped[key]) for key in sorted(grouped)]


def render_chart(points: Sequence[PlottedPoint], width: int) -> str:
    groups = group_points(points)
    max_age = max(point.age_years for point in points)
    column_years = max_age / (width - 1) if width > 1 else max_age
    lines: List[str] = [
        title_line(
            "stratigraphic chart",
            "{} columns · 1 column = {} years · max age {} years · {}".format(
                width,
                fmt_years(column_years),
                fmt_years(max_age),
                plural(len(points), "reading"),
            ),
        ),
        "shallowest reading at the top of each block · one {} per reading · "
        "age increases left to right".format(PLOT),
    ]
    for (site, unit), group in groups:
        lines.append("")
        lines.append(
            "{} · {}   ({}, {} cm → {} cm)".format(
                site,
                unit,
                plural(len(group), "reading"),
                fmt_cm(min(p.depth_cm for p in group)),
                fmt_cm(max(p.depth_cm for p in group)),
            )
        )
        lines.append("depth_cm │ age 0 → {} yr".format(fmt_years(max_age)))
        lines.append(RULE * 9 + "┼" + RULE * width)
        for point in group:
            bar = [" "] * width
            bar[chart_column(point.age_years, max_age, width)] = PLOT
            lines.append(
                "{} │{}│ {} yr  {}  {}".format(
                    "{:>8}".format(fmt_cm(point.depth_cm)),
                    accent("".join(bar)),
                    "{:>7}".format(fmt_years(point.age_years)),
                    point.measured_on,
                    point.artifact_label,
                )
            )
        lines.append(RULE * 9 + "┴" + RULE * width)
        right_label = "max {} yr".format(fmt_years(max_age))
        lines.append(
            "{}0 yr{}{}   (1 column = {} years, {} columns)".format(
                " " * 10,
                " " * max(width - 4 - len(right_label), 1),
                right_label,
                fmt_years(column_years),
                width,
            )
        )
    if len(points) == 1:
        lines.append("")
        lines.append(
            "note: a single point is not stratigraphically meaningful — add more "
            "readings at different depths to compare hydration against depth"
        )
    return "\n".join(lines)


def cmd_chart(args: argparse.Namespace) -> int:
    try:
        width = int(args.width)
    except (TypeError, ValueError):
        raise CliError("width must be a whole number between {} and {}".format(MIN_CHART_WIDTH, MAX_CHART_WIDTH))
    if width < MIN_CHART_WIDTH or width > MAX_CHART_WIDTH:
        raise CliError(
            "width must be a whole number between {} and {} (got {})".format(
                MIN_CHART_WIDTH, MAX_CHART_WIDTH, width
            )
        )
    store = resolve_store(args)
    ledger = gate_ledger(store)
    effective_rate(ledger)  # a chart without a baseline rate cannot plot ages
    if not ledger["readings"]:
        raise CliError(
            "no readings to chart — add one with basaltledger add or bulk-load with "
            "basaltledger import PATH first"
        )
    points = build_points(ledger, site=args.site, unit=args.unit)
    if not points:
        raise CliError(
            "no readings match {} — clear --site/--unit or add a reading for that "
            "unit".format(
                " ".join(
                    part
                    for part in (
                        "--site {!r}".format(args.site) if args.site else "",
                        "--unit {!r}".format(args.unit) if args.unit else "",
                    )
                    if part
                )
            )
        )
    text = render_chart(points, width)
    print(text)
    if args.out:
        out_path = Path(args.out).expanduser()
        try:
            out_path.write_text(text + "\n", encoding="utf-8")
        except OSError as exc:
            raise CliError(
                "cannot write chart to {}: {}".format(out_path, exc.strerror or exc)
            ) from exc
        eprint("chart written to {}".format(out_path))
    return EXIT_OK


def write_csv(rows: Sequence[Sequence[str]], header: Sequence[str], out: Optional[str]) -> None:
    buffer = []
    writer = csv.writer(_ListSink(buffer), lineterminator="\n")
    writer.writerow(header)
    writer.writerows(rows)
    text = "".join(buffer)
    if out is None or out == "-":
        sys.stdout.write(text)
        return
    path = Path(out).expanduser()
    try:
        path.write_text(text, encoding="utf-8", newline="")
    except OSError as exc:
        raise CliError("cannot write {}: {}".format(path, exc.strerror or exc)) from exc
    eprint("wrote {} to {}".format(plural(len(rows), "row"), path))


class _ListSink:
    """Minimal text sink so csv.writer can build the payload before we place it."""

    def __init__(self, buffer: List[str]) -> None:
        self._buffer = buffer

    def write(self, text: str) -> None:
        self._buffer.append(text)


def cmd_export(args: argparse.Namespace) -> int:
    store = resolve_store(args)
    ledger = gate_ledger(store)
    effective_rate(ledger)
    points = build_points(ledger, site=args.site, unit=args.unit)
    if not points:
        raise CliError(
            "nothing to export — add a reading with basaltledger add or bulk-load with "
            "basaltledger import PATH first"
        )
    write_csv([point.export_row() for point in points], EXPORT_HEADER, args.out)
    return EXIT_OK


def read_import_rows(path_text: str) -> List[Tuple[int, List[str]]]:
    path = Path(path_text).expanduser()
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise CliError(
            "cannot read {}: {}".format(path, exc.strerror or exc)
        ) from exc
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise CliError("{} is not UTF-8 text: {}".format(path, exc)) from exc
    rows: List[Tuple[int, List[str]]] = []
    reader = csv.reader(text.splitlines())
    for row in reader:
        if not row or all(not cell.strip() for cell in row):
            continue  # tolerate blank trailing lines
        rows.append((reader.line_num, row))
    if not rows:
        raise CliError(
            "{} is empty — expected header {}".format(path, ",".join(IMPORT_HEADER))
        )
    return rows


def check_import_header(row: Sequence[str]) -> None:
    normalized = [cell.strip().lower() for cell in row]
    if normalized != IMPORT_HEADER:
        raise CliError(
            "expected header {}".format(",".join(IMPORT_HEADER)),
            EXIT_USAGE,
            ["got header {}".format(",".join(cell.strip() for cell in row))],
        )


def validate_import_row(
    row: Sequence[str], line_no: int, today: date
) -> Tuple[Optional[Dict[str, Any]], List[str]]:
    if len(row) != len(IMPORT_HEADER):
        return None, [
            "line {}: expected {} fields, found {}".format(line_no, len(IMPORT_HEADER), len(row))
        ]
    artifact_raw, site_raw, unit_raw, depth_raw, hydration_raw, date_raw = [
        cell.strip() for cell in row
    ]
    errors: List[str] = []
    label, problems = parse_required_text(artifact_raw, "artifact")
    errors.extend("line {}: {}".format(line_no, problem) for problem in problems)
    site, problems = parse_required_text(site_raw, "site")
    errors.extend("line {}: {}".format(line_no, problem) for problem in problems)
    unit, problems = parse_required_text(unit_raw, "unit")
    errors.extend("line {}: {}".format(line_no, problem) for problem in problems)
    depth, problems = parse_number(depth_raw, "depth_cm", minimum=0.0, exclusive=False)
    errors.extend("line {}: {}".format(line_no, problem) for problem in problems)
    hydration, problems = parse_number(
        hydration_raw, "hydration_um", minimum=0.0, exclusive=True
    )
    errors.extend("line {}: {}".format(line_no, problem) for problem in problems)
    measured_on, problems = parse_measured_on(date_raw, today)
    errors.extend("line {}: {}".format(line_no, problem) for problem in problems)
    if errors:
        return None, errors
    return (
        {
            "label": label,
            "site": site,
            "unit": unit,
            "depth": depth,
            "hydration": hydration,
            "measured_on": measured_on,
        },
        [],
    )


def cmd_import(args: argparse.Namespace) -> int:
    """Validate every row first, then commit all of them or none."""
    today = date.today()
    store = resolve_store(args)
    ledger = gate_ledger(store)
    rows = read_import_rows(args.path)
    check_import_header(rows[0][1])
    data_rows = rows[1:]

    known: Dict[str, Dict[str, Any]] = {
        str(item["label"]): item for item in ledger["artifacts"]
    }
    next_artifact_id = next_id(ledger["artifacts"])
    errors: List[str] = []
    warnings: List[str] = []
    failing_lines: set = set()
    created_artifacts: List[Dict[str, Any]] = []
    plan: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []

    for line_no, row in data_rows:
        fields, problems = validate_import_row(row, line_no, today)
        if problems:
            errors.extend(problems)
            failing_lines.add(line_no)
            continue
        assert fields is not None
        artifact = known.get(str(fields["label"]))
        if artifact is None:
            artifact = {
                "id": next_artifact_id,
                "label": fields["label"],
                "site": fields["site"],
                "unit": fields["unit"],
                "depth_cm": fields["depth"],
                "created_at": now_timestamp(),
            }
            next_artifact_id += 1
            created_artifacts.append(artifact)
            known[str(fields["label"])] = artifact
        else:
            for conflict in artifact_conflicts(artifact, fields):
                warnings.append("line {}: {}".format(line_no, conflict))
        plan.append((artifact, fields))

    if errors:
        raise CliError(
            "import rejected — {} failed validation, nothing was written".format(
                plural(len(failing_lines), "row")
            ),
            EXIT_IMPORT_REJECTED,
            errors,
        )

    ledger["artifacts"].extend(created_artifacts)
    next_reading_id = next_id(ledger["readings"])
    for artifact, fields in plan:
        append_reading(
            ledger,
            artifact,
            hydration_um=float(fields["hydration"]),
            depth_cm=float(fields["depth"]),
            measured_on=str(fields["measured_on"]),
            source="import",
            reading_id=next_reading_id,
        )
        next_reading_id += 1
    store.save(ledger)

    for warning in warnings:
        eprint("warn: {}".format(warning))
    print(title_line("import complete", args.path))
    print(
        "rows read {} · readings added {} · artifacts created {}".format(
            len(data_rows), len(plan), len(created_artifacts)
        )
    )
    if not data_rows:
        print("note: the file held only a header, so nothing was written")
    return EXIT_OK


# --- Argument parser ---------------------------------------------------------


EXAMPLES = """\
examples:
  python3 basaltledger.py init
  python3 basaltledger.py add --artifact AR-7 --site Camas --unit EU-3 \\
      --depth 42.5 --hydration 3.1 --date 2024-05-06
  python3 basaltledger.py list
  python3 basaltledger.py show --artifact AR-7
  python3 basaltledger.py estimate
  python3 basaltledger.py chart --width 60
  python3 basaltledger.py export --format csv --out points.csv
  python3 basaltledger.py import rows.csv

exit codes: 0 ok · 1 import rows rejected · 2 usage or validation · 3 wrong passphrase
data file: ./basaltledger.json, or $BASALTLEDGER_DATA, or --data PATH
"""


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--data",
        metavar="PATH",
        default=argparse.SUPPRESS,
        help="ledger file to read or write (default: ./{}, $BASALTLEDGER_DATA)".format(
            DEFAULT_DATA_FILE
        ),
    )
    common.add_argument(
        "--no-color",
        action="store_true",
        default=argparse.SUPPRESS,
        help="disable ANSI colour (also honours NO_COLOR)",
    )

    parser = argparse.ArgumentParser(
        prog="basaltledger",
        description=(
            "Record weekly obsidian hydration readings, estimate surface ages from "
            "one ledger-wide baseline rate, and export ASCII stratigraphic charts."
        ),
        epilog=EXAMPLES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        parents=[common],
    )
    parser.add_argument("--version", action="version", version="BasaltLedger {}".format(__version__))
    subparsers = parser.add_subparsers(dest="command", metavar="<command>")

    init = subparsers.add_parser(
        "init",
        parents=[common],
        help="create the ledger and seed the baseline rate",
        description=(
            "Create the ledger and seed the single ledger-wide baseline rate "
            "(default {} µm/√yr).".format(fmt_rate(DEFAULT_RATE_UM_SQRTYR))
        ),
    )
    init.add_argument("--rate", metavar="V", help="baseline hydration rate in µm/√yr")
    init.add_argument("--note", metavar="TEXT", help="calibration note for the rate")
    init.add_argument(
        "--passphrase",
        action="store_true",
        help="gate every later command behind a local passphrase",
    )
    init.add_argument(
        "--force",
        action="store_true",
        help="replace an existing ledger (destroys every stored reading)",
    )
    init.set_defaults(handler=cmd_init)

    rate = subparsers.add_parser(
        "rate",
        parents=[common],
        help="show or replace the baseline rate",
        description="Show or replace the ledger-wide baseline hydration rate.",
    )
    rate_sub = rate.add_subparsers(dest="rate_command", metavar="<action>")
    rate_show_parser = rate_sub.add_parser(
        "show", parents=[common], help="print the stored rate, note and date"
    )
    rate_show_parser.set_defaults(handler=cmd_rate)
    rate_set_parser = rate_sub.add_parser(
        "set", parents=[common], help="replace the stored rate"
    )
    rate_set_parser.add_argument("--rate", metavar="V", required=True, help="new rate in µm/√yr")
    rate_set_parser.add_argument("--note", metavar="TEXT", help="calibration note")
    rate_set_parser.set_defaults(handler=cmd_rate)

    add = subparsers.add_parser(
        "add",
        parents=[common],
        help="append one hydration reading",
        description="Append one dated hydration reading, creating the artifact on first use.",
    )
    add.add_argument("--artifact", metavar="LABEL", required=True, help="artifact label, e.g. AR-7")
    add.add_argument("--site", metavar="S", required=True, help="site name")
    add.add_argument("--unit", metavar="U", required=True, help="stratigraphic unit")
    add.add_argument(
        "--depth", metavar="CM", required=True, help="artifact depth in cm (frozen at creation)"
    )
    add.add_argument("--hydration", metavar="UM", required=True, help="hydration rim depth in µm")
    add.add_argument("--date", metavar="YYYY-MM-DD", help="measurement date (default: today)")
    add.add_argument(
        "--depth-cm",
        metavar="CM",
        dest="depth_cm",
        help="depth of this reading in cm (default: the artifact's depth)",
    )
    add.set_defaults(handler=cmd_add)

    list_parser = subparsers.add_parser(
        "list",
        parents=[common],
        help="list artifacts with their latest reading",
        description="Print one line per artifact.",
    )
    list_parser.add_argument("--format", choices=["table", "csv"], default="table")
    list_parser.set_defaults(handler=cmd_list)

    show = subparsers.add_parser(
        "show",
        parents=[common],
        help="print one artifact's reading series",
        description="Print one artifact's readings, oldest first.",
    )
    show.add_argument("--artifact", metavar="LABEL", required=True)
    show.add_argument("--format", choices=["table", "csv"], default="table")
    show.set_defaults(handler=cmd_show)

    estimate = subparsers.add_parser(
        "estimate",
        parents=[common],
        help="compute surface ages from the baseline rate",
        description="Compute age_years = (hydration_um / rate)² for every reading.",
    )
    estimate.add_argument("--artifact", metavar="LABEL", help="narrow to one artifact")
    estimate.add_argument("--format", choices=["table", "csv"], default="table")
    estimate.set_defaults(handler=cmd_estimate)

    chart = subparsers.add_parser(
        "chart",
        parents=[common],
        help="print the ASCII stratigraphic chart",
        description="Render depth-versus-age as text, one symbol per reading.",
    )
    chart.add_argument("--site", metavar="S", help="only this site")
    chart.add_argument("--unit", metavar="U", help="only this unit")
    chart.add_argument(
        "--width",
        metavar="N",
        default=str(DEFAULT_CHART_WIDTH),
        help="chart columns (default: {})".format(DEFAULT_CHART_WIDTH),
    )
    chart.add_argument("--out", metavar="PATH", help="also write the chart text to PATH")
    chart.set_defaults(handler=cmd_chart)

    export = subparsers.add_parser(
        "export",
        parents=[common],
        help="export the plotted points as CSV",
        description="Write one CSV row per reading, in exactly the order the chart draws them.",
    )
    export.add_argument("--format", choices=["csv"], default="csv")
    export.add_argument("--out", metavar="PATH", help="write to PATH ('-' for stdout)")
    export.add_argument("--site", metavar="S", help="only this site")
    export.add_argument("--unit", metavar="U", help="only this unit")
    export.set_defaults(handler=cmd_export)

    importer = subparsers.add_parser(
        "import",
        parents=[common],
        help="append readings from a CSV file (all-or-nothing)",
        description=(
            "Read a CSV with header {} and append one reading per row.  "
            "If any row fails, nothing is written.".format(",".join(IMPORT_HEADER))
        ),
    )
    importer.add_argument("path", metavar="PATH", help="CSV file to read")
    importer.set_defaults(handler=cmd_import)

    return parser


def dispatch(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    if getattr(args, "command", None) is None:
        parser.print_help()
        return EXIT_OK
    if args.command == "rate" and getattr(args, "rate_command", None) is None:
        raise CliError("rate needs an action — use basaltledger rate show or basaltledger rate set --rate V")
    handler = getattr(args, "handler", None)
    if handler is None:
        raise CliError(
            "unknown command {!r} — run basaltledger --help".format(args.command)
        )
    return handler(args)


def main(argv: Optional[Sequence[str]] = None) -> int:
    global _USE_COLOR
    parser = build_parser()
    args = parser.parse_args(argv)
    _USE_COLOR = color_enabled(bool(getattr(args, "no_color", False)))
    try:
        return dispatch(args, parser)
    except CliError as exc:
        if exc.code == EXIT_PASSPHRASE:
            eprint(exc.message)
        else:
            eprint("error: {}".format(exc.message))
        for detail in exc.details:
            eprint(detail)
        return exc.code
    except KeyboardInterrupt:
        eprint("interrupted")
        return 130


if __name__ == "__main__":
    sys.exit(main())
