"""Bulk CSV import (all-or-nothing) and the optional passphrase gate."""

import pytest

from conftest import (
    CSV_HEADER,
    LEDGER,
    add_reading,
    read_ledger,
    run_cli,
    write_csv,
)


def test_import_appends_rows_and_reports_a_summary(ledger):
    write_csv(
        ledger,
        "rows.csv",
        [
            CSV_HEADER,
            "AR-7,Camas,EU-3,42.5,3.1,2024-05-06",
            "AR-7,Camas,EU-3,42.5,3.4,2024-06-10",
            "AR-9,Camas,EU-3,52.0,3.9,2024-06-17",
            "AR-12,Camas,EU-4,35.0,2.6,2024-04-15",
        ],
    )

    result = run_cli(ledger, "import", "rows.csv")

    assert result.returncode == 0, result.stderr
    assert "rows read 4" in result.stdout
    assert "readings added 4" in result.stdout
    assert "artifacts created 3" in result.stdout

    stored = read_ledger(ledger)
    assert len(stored["readings"]) == 4
    assert len(stored["artifacts"]) == 3
    assert {reading["source"] for reading in stored["readings"]} == {"import"}


def test_import_is_all_or_nothing(ledger):
    write_csv(
        ledger,
        "rows.csv",
        [
            CSV_HEADER,
            "AR-7,Camas,EU-3,42.5,3.1,2024-05-06",
            "AR-8,Camas,EU-3,44.0,-2,2024-05-13",
            "AR-9,Camas,EU-3,52.0,3.9,2024-06-17",
        ],
    )

    result = run_cli(ledger, "import", "rows.csv")

    assert result.returncode == 1
    assert "import rejected" in result.stderr
    assert "line 3" in result.stderr
    assert "hydration_um" in result.stderr
    stored = read_ledger(ledger)
    assert stored["readings"] == []
    assert stored["artifacts"] == []


def test_import_rejects_a_wrong_header(ledger):
    write_csv(
        ledger,
        "rows.csv",
        [CSV_HEADER.replace(",hydration_um", ""), "AR-7,Camas,EU-3,42.5,2024-05-06"],
    )

    result = run_cli(ledger, "import", "rows.csv")

    assert result.returncode == 2
    assert "expected header artifact,site,unit,depth_cm,hydration_um,measured_on" in result.stderr
    assert read_ledger(ledger)["readings"] == []


def test_import_warns_when_a_stored_artifact_field_is_frozen(ledger):
    assert add_reading(ledger).returncode == 0

    write_csv(
        ledger,
        "rows.csv",
        [CSV_HEADER, "AR-7,Other,EU-9,10,3.2,2024-05-13"],
    )

    result = run_cli(ledger, "import", "rows.csv")

    assert result.returncode == 0, result.stderr
    assert "ignored" in result.stderr
    assert "site" in result.stderr
    stored = read_ledger(ledger)
    artifact = stored["artifacts"][0]
    assert (artifact["site"], artifact["unit"], artifact["depth_cm"]) == (
        "Camas",
        "EU-3",
        42.5,
    )
    assert len(stored["readings"]) == 2


def test_import_reuses_an_artifact_across_rows(ledger):
    write_csv(
        ledger,
        "rows.csv",
        [
            CSV_HEADER,
            "AR-7,Camas,EU-3,42.5,3.1,2024-05-06",
            "AR-7,Camas,EU-3,42.5,3.4,2024-06-10",
        ],
    )

    result = run_cli(ledger, "import", "rows.csv")

    assert result.returncode == 0
    assert "artifacts created 1" in result.stdout
    stored = read_ledger(ledger)
    assert len(stored["artifacts"]) == 1
    assert len(stored["readings"]) == 2


def test_import_of_a_header_only_file_writes_nothing(ledger):
    write_csv(ledger, "rows.csv", [CSV_HEADER])

    result = run_cli(ledger, "import", "rows.csv")

    assert result.returncode == 0
    assert "rows read 0" in result.stdout
    assert read_ledger(ledger)["readings"] == []


def test_import_rejects_a_future_measurement_date(ledger):
    write_csv(ledger, "rows.csv", [CSV_HEADER, "AR-7,Camas,EU-3,42.5,3.1,2999-01-01"])

    result = run_cli(ledger, "import", "rows.csv")

    assert result.returncode == 1
    assert "measured_on must not be in the future" in result.stderr


def test_import_reports_a_missing_file(ledger):
    result = run_cli(ledger, "import", "nope.csv")

    assert result.returncode == 2
    assert "cannot read" in result.stderr


def test_passphrase_gate_protects_every_command(site):
    created = run_cli(
        site, "init", "--passphrase", env={"BASALTLEDGER_PASSPHRASE": "swordfish"}
    )

    assert created.returncode == 0, created.stderr
    raw = (site / LEDGER).read_text(encoding="utf-8")
    assert "swordfish" not in raw
    meta = read_ledger(site)["meta"]
    assert len(meta["passphrase_salt_hex"]) == 32
    assert len(meta["passphrase_hash_hex"]) == 64
    assert meta["passphrase_salt_hex"] != meta["passphrase_hash_hex"][:32]

    for argv in (
        ["list"],
        ["rate", "show"],
        ["estimate"],
        ["chart"],
        ["export", "--format", "csv"],
    ):
        denied = run_cli(site, *argv, env={"BASALTLEDGER_PASSPHRASE": "wrong"})
        assert denied.returncode == 3, argv
        assert denied.stderr.strip() == "wrong passphrase"

    allowed = run_cli(site, "list", env={"BASALTLEDGER_PASSPHRASE": "swordfish"})
    assert allowed.returncode == 0
    assert "artifacts" in allowed.stdout


def test_passphrase_gate_blocks_writes_too(site):
    assert run_cli(
        site, "init", "--passphrase", env={"BASALTLEDGER_PASSPHRASE": "swordfish"}
    ).returncode == 0

    denied = run_cli(
        site,
        "add",
        "--artifact",
        "AR-7",
        "--site",
        "Camas",
        "--unit",
        "EU-3",
        "--depth",
        "42.5",
        "--hydration",
        "3.1",
        env={"BASALTLEDGER_PASSPHRASE": "wrong"},
    )

    assert denied.returncode == 3
    assert read_ledger(site)["readings"] == []


def test_open_ledger_never_prompts(ledger):
    result = run_cli(ledger, "list", stdin="")

    assert result.returncode == 0
    assert "passphrase" not in result.stderr.lower()


def test_passphrase_less_ledger_works_without_any_environment_variable(site):
    assert run_cli(site, "init").returncode == 0
    assert add_reading(site).returncode == 0

    result = run_cli(site, "estimate")

    assert result.returncode == 0
    assert "38.44" in result.stdout


@pytest.mark.parametrize("command", [["rate", "set", "--rate", "0.7"], ["import", "rows.csv"]])
def test_passphrase_gate_covers_rate_set_and_import(site, command):
    assert run_cli(
        site, "init", "--passphrase", env={"BASALTLEDGER_PASSPHRASE": "swordfish"}
    ).returncode == 0
    write_csv(site, "rows.csv", [CSV_HEADER, "AR-7,Camas,EU-3,42.5,3.1,2024-05-06"])

    denied = run_cli(site, *command, env={"BASALTLEDGER_PASSPHRASE": "nope"})

    assert denied.returncode == 3
    assert read_ledger(site)["meta"]["rate_um_sqrtyr"] == 0.5
    assert read_ledger(site)["readings"] == []
