"""Shared fixtures: every test drives the real CLI in a throwaway directory."""

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Optional

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CLI = PROJECT_ROOT / "basaltledger.py"
LEDGER = "basaltledger.json"
CSV_HEADER = "artifact,site,unit,depth_cm,hydration_um,measured_on"

DEFAULT_ADD_FIELDS = {
    "--artifact": "AR-7",
    "--site": "Camas",
    "--unit": "EU-3",
    "--depth": "42.5",
    "--hydration": "3.1",
    "--date": "2024-05-06",
}


@pytest.fixture()
def site(tmp_path, monkeypatch):
    """An empty working directory with a clean environment."""
    monkeypatch.chdir(tmp_path)
    for name in ("BASALTLEDGER_DATA", "BASALTLEDGER_PASSPHRASE", "NO_COLOR"):
        monkeypatch.delenv(name, raising=False)
    return tmp_path


@pytest.fixture()
def ledger(site):
    """An initialised ledger in the working directory."""
    result = run_cli(site, "init")
    assert result.returncode == 0, result.stderr
    return site


def run_cli(
    site: Path,
    *argv: str,
    env: Optional[Dict[str, str]] = None,
    stdin: str = "",
) -> subprocess.CompletedProcess:
    environ = {
        key: value
        for key, value in os.environ.items()
        if key not in ("BASALTLEDGER_DATA", "BASALTLEDGER_PASSPHRASE")
    }
    environ["NO_COLOR"] = "1"
    if env:
        environ.update(env)
    return subprocess.run(
        [sys.executable, str(CLI), *argv],
        cwd=str(site),
        env=environ,
        input=stdin,
        capture_output=True,
        text=True,
    )


def add_reading(site: Path, **overrides: str) -> subprocess.CompletedProcess:
    fields = dict(DEFAULT_ADD_FIELDS)
    fields.update(overrides)
    argv = ["add"]
    for key, value in fields.items():
        argv.extend([key, value])
    return run_cli(site, *argv)


def seed_example_series(site: Path) -> None:
    """Three artifacts, five readings across two units — the demo ledger."""
    for overrides in (
        {"--artifact": "AR-7", "--depth": "40", "--hydration": "3.1", "--date": "2024-05-06"},
        {"--artifact": "AR-7", "--depth": "45", "--hydration": "3.4", "--date": "2024-06-10"},
        {"--artifact": "AR-7", "--depth": "52", "--hydration": "3.9", "--date": "2024-06-17"},
        {"--artifact": "AR-9", "--unit": "EU-4", "--depth": "60", "--hydration": "4.4",
         "--date": "2024-06-24"},
        {"--artifact": "AR-12", "--unit": "EU-4", "--depth": "35", "--hydration": "2.6",
         "--date": "2024-04-15"},
    ):
        result = add_reading(site, **overrides)
        assert result.returncode == 0, result.stderr


def read_ledger(site: Path) -> Dict[str, Any]:
    return json.loads((site / LEDGER).read_text(encoding="utf-8"))


def write_ledger(site: Path, ledger: Dict[str, Any]) -> None:
    (site / LEDGER).write_text(json.dumps(ledger, indent=2), encoding="utf-8")


def write_csv(site: Path, name: str, lines) -> Path:
    path = site / name
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path
