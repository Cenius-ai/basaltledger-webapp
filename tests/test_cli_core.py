"""Core ledger behaviour: init, rate, add, list and show."""

import re

import pytest

from conftest import (
    LEDGER,
    add_reading,
    read_ledger,
    run_cli,
    write_ledger,
)

COMMANDS = (
    "init",
    "rate",
    "add",
    "list",
    "show",
    "estimate",
    "chart",
    "export",
    "import",
)


def test_help_lists_every_command(site):
    result = run_cli(site, "--help")

    assert result.returncode == 0
    for command in COMMANDS:
        assert command in result.stdout
    assert "exit codes" in result.stdout


def test_version_flag(site):
    result = run_cli(site, "--version")

    assert result.returncode == 0
    assert "BasaltLedger 1.0.0" in result.stdout


def test_bare_invocation_prints_usage(site):
    result = run_cli(site)

    assert result.returncode == 0
    assert "usage: basaltledger" in result.stdout


def test_init_seeds_documented_default_rate(site):
    result = run_cli(site, "init")

    assert result.returncode == 0
    assert "0.50 µm/√yr" in result.stdout
    assert "uncalibrated default" in result.stdout

    ledger = read_ledger(site)
    assert ledger["schema_version"] == 1
    assert ledger["meta"]["rate_um_sqrtyr"] == 0.5
    assert ledger["meta"]["rate_note"].startswith("uncalibrated default")
    assert ledger["artifacts"] == [] and ledger["readings"] == []


def test_rate_show_prints_rate_week_equivalent_and_note(ledger):
    result = run_cli(ledger, "rate", "show")

    assert result.returncode == 0
    assert "0.50 µm/√yr" in result.stdout
    assert "0.0692 µm/√week" in result.stdout
    assert "uncalibrated default" in result.stdout
    assert "rate set on" in result.stdout


def test_reinit_refuses_without_force_and_keeps_readings(ledger):
    add_reading(ledger)

    result = run_cli(ledger, "init")

    assert result.returncode == 2
    assert "already exists" in result.stderr
    assert len(read_ledger(ledger)["readings"]) == 1


def test_reinit_with_force_replaces_the_ledger(ledger):
    add_reading(ledger)

    result = run_cli(ledger, "init", "--force", "--rate", "0.62", "--note", "Ridge curve")

    assert result.returncode == 0
    stored = read_ledger(ledger)
    assert stored["readings"] == []
    assert stored["meta"]["rate_um_sqrtyr"] == 0.62


def test_rate_set_echoes_previous_and_new_rate(ledger):
    result = run_cli(ledger, "rate", "set", "--rate", "0.75", "--note", "Camas site curve")

    assert result.returncode == 0
    assert "previous rate" in result.stdout and "0.50 µm/√yr" in result.stdout
    assert "new rate" in result.stdout and "0.75 µm/√yr" in result.stdout
    assert "Camas site curve" in result.stdout

    stored = read_ledger(ledger)["meta"]
    assert stored["rate_um_sqrtyr"] == 0.75
    assert stored["rate_note"] == "Camas site curve"
    assert "0.75 µm/√yr" in run_cli(ledger, "rate", "show").stdout


@pytest.mark.parametrize("bad_rate", ["0", "-1", "abc", "nan", "inf"])
def test_rate_set_rejects_non_positive_or_unparsable_rates(ledger, bad_rate):
    result = run_cli(ledger, "rate", "set", "--rate", bad_rate)

    assert result.returncode == 2
    assert "rate must be a number greater than 0" in result.stderr
    assert read_ledger(ledger)["meta"]["rate_um_sqrtyr"] == 0.5


def test_missing_ledger_tells_the_user_to_run_init(site):
    result = run_cli(site, "rate", "show")

    assert result.returncode == 2
    assert "run basaltledger init first" in result.stderr


def test_init_rate_flag_sets_the_note_when_given(ledger):
    result = run_cli(ledger, "init", "--force", "--rate", "0.9", "--note", "Lab curve 2024")

    assert result.returncode == 0
    meta = read_ledger(ledger)["meta"]
    assert meta["rate_um_sqrtyr"] == 0.9
    assert meta["rate_note"] == "Lab curve 2024"


def test_add_first_reading_echoes_effective_rate_only(ledger):
    result = add_reading(ledger)

    assert result.returncode == 0, result.stderr
    assert "0.50 µm/√yr" in result.stdout
    assert "0.0692 µm/√week" in result.stdout
    assert "a second reading is needed" in result.stdout
    assert "AR-7 · Camas · EU-3 · 42.5 cm" in result.stdout

    stored = read_ledger(ledger)
    assert len(stored["artifacts"]) == 1
    assert len(stored["readings"]) == 1
    reading = stored["readings"][0]
    assert reading["hydration_um"] == 3.1
    assert reading["depth_cm"] == 42.5
    assert reading["measured_on"] == "2024-05-06"
    assert reading["source"] == "add"
    assert reading["created_at"]


def test_add_second_reading_prints_an_implied_rate(ledger):
    assert add_reading(ledger).returncode == 0

    result = add_reading(ledger, **{"--hydration": "3.4", "--date": "2024-06-10"})

    assert result.returncode == 0, result.stderr
    assert "implied rate" in result.stdout
    assert "hydration_um / sqrt(age_years)" in result.stdout
    printed = [float(value) for value in re.findall(r"implied ([\d.]+) µm/√yr", result.stdout)]
    assert len(printed) == 2
    assert all(value > 0 for value in printed)


@pytest.mark.parametrize(
    "overrides,field",
    [
        ({"--hydration": "0"}, "hydration_um"),
        ({"--hydration": "abc"}, "hydration_um"),
        ({"--depth": "-1"}, "depth_cm"),
        ({"--artifact": "   "}, "artifact"),
        ({"--site": ""}, "site"),
        ({"--unit": ""}, "unit"),
    ],
)
def test_add_validation_errors_write_nothing(ledger, overrides, field):
    result = add_reading(ledger, **overrides)

    assert result.returncode == 2
    assert field in result.stderr
    assert read_ledger(ledger)["readings"] == []


def test_add_rejects_a_future_date(ledger):
    result = add_reading(ledger, **{"--date": "2999-01-01"})

    assert result.returncode == 2
    assert "measured_on must not be in the future" in result.stderr
    assert read_ledger(ledger)["readings"] == []


def test_add_rejects_a_malformed_date(ledger):
    result = add_reading(ledger, **{"--date": "06/05/2024"})

    assert result.returncode == 2
    assert "measured_on must be an ISO-8601 date" in result.stderr


def test_add_reuses_the_artifact_and_warns_about_ignored_fields(ledger):
    assert add_reading(ledger).returncode == 0

    result = add_reading(
        ledger,
        **{"--site": "Other", "--unit": "EU-9", "--depth": "10", "--hydration": "3.2",
           "--date": "2024-05-13"},
    )

    assert result.returncode == 0, result.stderr
    assert "ignored" in result.stderr

    stored = read_ledger(ledger)
    assert len(stored["artifacts"]) == 1
    artifact = stored["artifacts"][0]
    assert artifact["site"] == "Camas"
    assert artifact["unit"] == "EU-3"
    assert artifact["depth_cm"] == 42.5
    assert len(stored["readings"]) == 2

    shown = run_cli(ledger, "show", "--artifact", "AR-7")
    assert "Camas" in shown.stdout and "EU-3" in shown.stdout and "42.5 cm" in shown.stdout


def test_add_accepts_a_per_reading_depth(ledger):
    result = add_reading(ledger, **{"--depth-cm": "45"})

    assert result.returncode == 0, result.stderr
    stored = read_ledger(ledger)
    assert stored["artifacts"][0]["depth_cm"] == 42.5
    assert stored["readings"][0]["depth_cm"] == 45.0


def test_list_on_an_empty_ledger_names_the_add_command(ledger):
    result = run_cli(ledger, "list")

    assert result.returncode == 0
    assert "no artifacts yet" in result.stdout
    assert "basaltledger add --artifact" in result.stdout
    assert "0 artifacts" in result.stdout


def test_list_prints_one_line_per_artifact(ledger):
    add_reading(ledger)
    add_reading(ledger, **{"--hydration": "3.4", "--date": "2024-06-10"})
    add_reading(
        ledger,
        **{"--artifact": "AR-9", "--unit": "EU-4", "--depth": "60", "--hydration": "4.1",
           "--date": "2024-06-17"},
    )

    result = run_cli(ledger, "list")

    assert result.returncode == 0
    rows = [line for line in result.stdout.splitlines() if line.startswith(("AR-7", "AR-9"))]
    assert len(rows) == 2
    ar7 = rows[0].split()
    assert ar7[0] == "AR-7" and ar7[1] == "Camas" and ar7[2] == "EU-3"
    assert ar7[3] == "42.5" and ar7[4] == "2" and ar7[5] == "3.4"
    assert float(ar7[6]) == pytest.approx((3.4 / 0.5) ** 2, rel=1e-3)
    assert "2 artifacts · 3 readings" in result.stdout


def test_list_csv_format_is_pipeable(ledger):
    add_reading(ledger)

    result = run_cli(ledger, "list", "--format", "csv")

    assert result.returncode == 0
    lines = result.stdout.splitlines()
    assert lines[0] == (
        "label,site,unit,depth_cm,readings,latest_hydration_um,latest_age_years"
    )
    assert lines[1].startswith("AR-7,Camas,EU-3,42.5,1,3.1,")
    assert "totals" in result.stderr


def test_show_unknown_label_lists_the_known_labels(ledger):
    add_reading(ledger)
    add_reading(
        ledger,
        **{"--artifact": "AR-9", "--depth": "60", "--hydration": "4.1", "--date": "2024-06-17"},
    )

    result = run_cli(ledger, "show", "--artifact", "NOPE")

    assert result.returncode == 2
    assert "unknown artifact label" in result.stderr
    assert "AR-7" in result.stderr and "AR-9" in result.stderr


def test_show_prints_readings_oldest_first(ledger):
    for measured_on in ("2024-06-17", "2024-05-06", "2024-06-10"):
        assert add_reading(ledger, **{"--date": measured_on}).returncode == 0

    result = run_cli(ledger, "show", "--artifact", "AR-7")

    assert result.returncode == 0
    rows = [line for line in result.stdout.splitlines() if line.startswith("2024-")]
    assert [row.split()[0] for row in rows] == ["2024-05-06", "2024-06-10", "2024-06-17"]
    assert all("add" in row for row in rows)
    assert "42.5" in rows[0]
    assert "3 readings" in result.stdout


def test_show_csv_format_lists_the_same_rows(ledger):
    add_reading(ledger)

    result = run_cli(ledger, "show", "--artifact", "AR-7", "--format", "csv")

    assert result.returncode == 0
    lines = result.stdout.splitlines()
    assert lines[0] == "measured_on,hydration_um,depth_cm,source,age_years"
    assert lines[1].startswith("2024-05-06,3.1,42.5,add,")


def test_writes_are_atomic_and_leave_no_temp_files(ledger):
    add_reading(ledger)

    leftovers = [path.name for path in ledger.iterdir() if path.name.startswith(".basaltledger-")]
    assert leftovers == []
    assert read_ledger(ledger)["readings"][0]["hydration_um"] == 3.1


def test_unwritable_target_exits_two(site):
    blocker = site / "blocked"
    blocker.write_text("not a directory", encoding="utf-8")

    result = run_cli(site, "--data", str(blocker / "ledger.json"), "init")

    assert result.returncode == 2
    assert "cannot create directory" in result.stderr


def test_directory_target_is_rejected(site):
    (site / "adir").mkdir()

    result = run_cli(site, "--data", "adir", "init")

    assert result.returncode == 2
    assert "is a directory" in result.stderr


def test_data_env_var_selects_the_ledger(site):
    result = run_cli(site, "init", env={"BASALTLEDGER_DATA": "nested/ledger.json"})

    assert result.returncode == 0
    assert (site / "nested" / "ledger.json").exists()


def test_corrupt_ledger_reports_one_plain_line(site):
    (site / LEDGER).write_text("{not json", encoding="utf-8")

    result = run_cli(site, "list")

    assert result.returncode == 2
    assert "is not valid JSON" in result.stderr


def test_missing_rate_blocks_reports_but_not_listing(ledger):
    add_reading(ledger)
    stored = read_ledger(ledger)
    stored["meta"]["rate_um_sqrtyr"] = 0
    write_ledger(ledger, stored)

    listing = run_cli(ledger, "list")
    estimate = run_cli(ledger, "estimate")

    assert listing.returncode == 0
    assert "no baseline rate" in listing.stdout
    assert estimate.returncode == 2
    assert "no baseline rate — run basaltledger init --rate V" in estimate.stderr
