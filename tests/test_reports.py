"""Report surfaces: estimate, export and the ASCII stratigraphic chart."""

import csv
import io
import re

import pytest

from conftest import (
    add_reading,
    read_ledger,
    run_cli,
    seed_example_series,
    write_ledger,
)

CHART_ROW = re.compile(
    r"^\s*(?P<depth>[\d.]+) │(?P<bar>[^│]*)│\s*(?P<age>[\d.]+) yr  "
    r"(?P<measured_on>\d{4}-\d{2}-\d{2})  (?P<label>.+?)\s*$"
)


def chart_rows(stdout: str):
    rows = []
    for line in stdout.splitlines():
        match = CHART_ROW.match(line)
        if match:
            rows.append(match)
    return rows


def csv_text(result) -> str:
    return result.stdout


def test_estimate_uses_the_documented_age_formula(ledger):
    add_reading(ledger)

    result = run_cli(ledger, "estimate")

    assert result.returncode == 0, result.stderr
    row = [line for line in result.stdout.splitlines() if line.startswith("AR-7")][0]
    cells = row.split()
    assert cells[0] == "AR-7"
    assert cells[3] == "42.5"
    assert cells[4] == "2024-05-06"
    assert cells[5] == "3.1"
    assert cells[6] == "0.50"
    assert float(cells[7]) == pytest.approx(38.44, abs=0.01)
    assert int(cells[8]) == 2006
    assert "uncalibrated default" in result.stdout


def test_estimate_summarises_per_artifact_and_ledger_wide(ledger):
    seed_example_series(ledger)

    result = run_cli(ledger, "estimate")

    assert result.returncode == 0, result.stderr
    assert "summary per artifact" in result.stdout
    assert "min_age_yr" in result.stdout and "mean_age_yr" in result.stdout
    assert "max_age_yr" in result.stdout
    assert "5 readings · 3 artifacts" in result.stdout
    assert "rate            0.50 µm/√yr" in result.stdout
    assert "uncalibrated default" in result.stdout
    summary = [line for line in result.stdout.splitlines() if line.startswith("AR-12")]
    assert summary and summary[-1].split()[1] == "1"


def test_estimate_without_a_baseline_rate_fails(ledger):
    add_reading(ledger)
    stored = read_ledger(ledger)
    stored["meta"]["rate_um_sqrtyr"] = 0
    write_ledger(ledger, stored)

    result = run_cli(ledger, "estimate")

    assert result.returncode == 2
    assert "no baseline rate — run basaltledger init --rate V" in result.stderr
    assert "age_years" not in result.stdout


def test_estimate_on_an_empty_ledger_points_at_add_and_import(ledger):
    result = run_cli(ledger, "estimate")

    assert result.returncode == 2
    assert "basaltledger add" in result.stderr and "basaltledger import" in result.stderr


def test_estimate_narrows_to_one_artifact(ledger):
    seed_example_series(ledger)

    result = run_cli(ledger, "estimate", "--artifact", "AR-9")

    assert result.returncode == 0
    assert "AR-7" not in result.stdout
    assert "AR-9" in result.stdout

    unknown = run_cli(ledger, "estimate", "--artifact", "NOPE")
    assert unknown.returncode == 2
    assert "unknown artifact label" in unknown.stderr
    assert "AR-12" in unknown.stderr


def test_estimate_csv_matches_the_export_columns_plus_age_weeks(ledger):
    add_reading(ledger)

    export_row = run_cli(ledger, "export", "--format", "csv")
    estimate = run_cli(ledger, "estimate", "--format", "csv")

    export_header = next(csv.reader(io.StringIO(export_row.stdout)))
    estimate_rows = list(csv.reader(io.StringIO(estimate.stdout)))
    assert estimate_rows[0] == export_header + ["age_weeks"]
    assert len(estimate_rows) == 2
    assert float(estimate_rows[1][7]) == pytest.approx(38.44, abs=0.01)
    assert float(estimate_rows[1][8]) == pytest.approx(38.44 * 52.1775, rel=1e-6)


def test_export_writes_one_row_per_reading(ledger):
    seed_example_series(ledger)

    result = run_cli(ledger, "export", "--format", "csv", "--out", "points.csv")

    assert result.returncode == 0, result.stderr
    assert "wrote 5 rows to points.csv" in result.stderr
    text = (ledger / "points.csv").read_text(encoding="utf-8")
    assert text.endswith("\n") and "\r" not in text
    rows = list(csv.reader(io.StringIO(text)))
    assert rows[0] == [
        "artifact_label",
        "site",
        "unit",
        "depth_cm",
        "measured_on",
        "hydration_um",
        "effective_rate_um_sqrtyr",
        "age_years",
    ]
    assert len(rows) == 6
    assert {row[0] for row in rows[1:]} == {"AR-7", "AR-9", "AR-12"}
    for row in rows[1:]:
        assert float(row[5]) > 0
        assert float(row[7]) == pytest.approx((float(row[5]) / float(row[6])) ** 2, rel=1e-6)


def test_export_keeps_every_reading_of_a_multi_reading_artifact(ledger):
    for measured_on, hydration in (
        ("2024-05-06", "3.1"),
        ("2024-06-10", "3.4"),
        ("2024-06-17", "3.9"),
    ):
        assert add_reading(
            ledger, **{"--date": measured_on, "--hydration": hydration}
        ).returncode == 0

    result = run_cli(ledger, "export", "--format", "csv")

    rows = list(csv.DictReader(io.StringIO(csv_text(result))))
    assert len(rows) == 3
    assert [row["measured_on"] for row in rows] == [
        "2024-05-06",
        "2024-06-10",
        "2024-06-17",
    ]
    assert [row["hydration_um"] for row in rows] == ["3.1", "3.4", "3.9"]


def test_export_quotes_labels_and_sites_with_commas(ledger):
    assert add_reading(
        ledger, **{"--artifact": 'AR-7, north', "--site": 'Ridge, east'}
    ).returncode == 0

    result = run_cli(ledger, "export", "--format", "csv")

    assert '"AR-7, north"' in result.stdout
    row = list(csv.DictReader(io.StringIO(result.stdout)))[0]
    assert row["artifact_label"] == "AR-7, north"
    assert row["site"] == "Ridge, east"


def test_export_to_stdout_streams_the_same_bytes(ledger):
    add_reading(ledger)

    piped = run_cli(ledger, "export", "--format", "csv", "--out", "-")
    streamed = run_cli(ledger, "export", "--format", "csv")

    assert piped.returncode == 0 and streamed.returncode == 0
    assert piped.stdout == streamed.stdout


def test_export_on_an_empty_ledger_exits_two(ledger):
    result = run_cli(ledger, "export", "--format", "csv", "--out", "points.csv")

    assert result.returncode == 2
    assert "nothing to export" in result.stderr
    assert not (ledger / "points.csv").exists()


def test_export_rejects_an_unsupported_format(ledger):
    add_reading(ledger)

    result = run_cli(ledger, "export", "--format", "xlsx")

    assert result.returncode == 2


def test_chart_plots_one_symbol_per_reading_shallowest_first(ledger):
    for depth, hydration, measured_on in (
        ("40", "3.1", "2024-05-06"),
        ("45", "3.4", "2024-06-10"),
        ("52", "3.9", "2024-06-17"),
    ):
        assert add_reading(
            ledger,
            **{"--depth": depth, "--hydration": hydration, "--date": measured_on},
        ).returncode == 0

    result = run_cli(ledger, "chart", "--width", "40")

    assert result.returncode == 0, result.stderr
    rows = chart_rows(result.stdout)
    assert [float(row.group("depth")) for row in rows] == [40.0, 45.0, 52.0]
    for row in rows:
        assert row.group("bar").count("*") == 1
        assert len(row.group("bar")) == 40
    columns = [row.group("bar").index("*") for row in rows]
    assert columns == sorted(columns)
    assert columns[0] >= 0 and columns[-1] <= 39


def test_chart_legend_states_scale_width_and_max_age(ledger):
    seed_example_series(ledger)

    result = run_cli(ledger, "chart", "--width", "60")

    assert result.returncode == 0
    assert "1 column = " in result.stdout
    assert "60 columns" in result.stdout
    assert "max age" in result.stdout
    assert "age 0 →" in result.stdout


def test_chart_groups_by_site_and_unit(ledger):
    seed_example_series(ledger)

    result = run_cli(ledger, "chart")

    assert "Camas · EU-3" in result.stdout
    assert "Camas · EU-4" in result.stdout
    assert len(chart_rows(result.stdout)) == 5


def test_chart_and_export_agree_point_for_point(ledger):
    seed_example_series(ledger)

    chart = run_cli(ledger, "chart", "--width", "70")
    export = run_cli(ledger, "export", "--format", "csv")

    chart_points = [
        (row.group("label"), row.group("measured_on"), float(row.group("depth")))
        for row in chart_rows(chart.stdout)
    ]
    csv_points = [
        (row["artifact_label"], row["measured_on"], float(row["depth_cm"]))
        for row in csv.DictReader(io.StringIO(export.stdout))
    ]
    assert chart_points == csv_points


def test_chart_out_writes_the_same_text_as_stdout(ledger):
    seed_example_series(ledger)

    printed = run_cli(ledger, "chart", "--width", "80")
    written = run_cli(ledger, "chart", "--width", "80", "--out", "chart.txt")

    assert written.returncode == 0
    assert "chart written to chart.txt" in written.stderr
    assert (ledger / "chart.txt").read_text(encoding="utf-8") == printed.stdout


def test_chart_notes_a_single_reading(ledger):
    add_reading(ledger)

    result = run_cli(ledger, "chart")

    assert result.returncode == 0
    assert len(chart_rows(result.stdout)) == 1
    assert "single point is not stratigraphically meaningful" in result.stdout


def test_chart_on_an_empty_ledger_exits_two(ledger):
    result = run_cli(ledger, "chart")

    assert result.returncode == 2
    assert "no readings to chart" in result.stderr


@pytest.mark.parametrize("width", ["4", "abc", "1000"])
def test_chart_rejects_an_invalid_width(ledger, width):
    add_reading(ledger)

    result = run_cli(ledger, "chart", "--width", width)

    assert result.returncode == 2
    assert "width must be a whole number" in result.stderr


def test_chart_filters_by_site_and_unit(ledger):
    seed_example_series(ledger)

    result = run_cli(ledger, "chart", "--unit", "EU-4")

    assert result.returncode == 0
    assert "EU-3" not in result.stdout
    assert len(chart_rows(result.stdout)) == 2

    unmatched = run_cli(ledger, "chart", "--unit", "NOPE")
    assert unmatched.returncode == 2
    assert "no readings match" in unmatched.stderr


def test_ten_thousand_readings_stay_fast(ledger):
    lines = ["artifact,site,unit,depth_cm,hydration_um,measured_on"]
    for index in range(10_000):
        lines.append(
            "AR-{n},Camas,EU-{u},{d}.0,{h}.{f},2020-01-{day:02d}".format(
                n=index % 400 + 1,
                u=index % 5 + 1,
                d=index % 90,
                h=index % 5 + 1,
                f=index % 10,
                day=index % 28 + 1,
            )
        )
    (ledger / "big.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")

    imported = run_cli(ledger, "import", "big.csv")
    estimate = run_cli(ledger, "estimate", "--format", "csv")

    assert imported.returncode == 0, imported.stderr
    assert "readings added 10000" in imported.stdout
    assert estimate.returncode == 0
    assert len(estimate.stdout.splitlines()) == 10_001
