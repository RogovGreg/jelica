from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from jelica_cli.main import app
from jelica_core.system_config import CoreConfigService

runner = CliRunner()


def _invoke(args: list[str], home: Path):
    service = CoreConfigService(jelica_home=home)
    service.initialize_system_config()
    return runner.invoke(app, args, env={"JELICA_HOME": str(home)})


def test_reports_list_short_is_dense_and_sorted(tmp_path: Path) -> None:
    home = tmp_path / "home"
    service = CoreConfigService(jelica_home=home)
    service.initialize_system_config()
    reports = home / "reports"
    reports.mkdir()
    for index in range(11, 0, -1):
        (reports / f"Report_{index:02d}.pdf").write_bytes(b"pdf")

    result = _invoke(["reports", "list", "--short"], home)

    assert result.exit_code == 0
    lines = result.stdout.splitlines()
    assert len(lines) == 11
    assert all(line and not line.isspace() for line in lines)
    assert lines[0].startswith("1. Report_01.pdf")
    assert lines[8].startswith("9. Report_09.pdf")
    assert lines[9].startswith("10. Report_10.pdf")
    assert lines[10].startswith("11. Report_11.pdf")


def test_reports_list_standard_and_verbose_align_two_digit_numbers(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    service = CoreConfigService(jelica_home=home)
    service.initialize_system_config()
    reports = home / "reports"
    reports.mkdir()
    for index in range(1, 12):
        (reports / f"Report_{index:02d}.pdf").write_bytes(b"pdf")

    standard = _invoke(["reports", "list", "--standard"], home)
    verbose = _invoke(["reports", "list", "--verbose"], home)

    assert standard.exit_code == 0
    standard_lines = standard.stdout.splitlines()
    assert "9. Report: Report_09.pdf" in standard_lines
    assert "   Format: PDF - 3 B" in standard_lines
    assert "10. Report: Report_10.pdf" in standard_lines
    assert "    Format: PDF - 3 B" in standard_lines
    assert "11. Report: Report_11.pdf" in standard_lines
    assert standard_lines.count("") == 10

    assert verbose.exit_code == 0
    verbose_lines = verbose.stdout.splitlines()
    for label in ("Format:", "Size:", "Modified:", "Path:"):
        assert any(line.startswith(f"    {label}") for line in verbose_lines)
    assert verbose_lines.count("") == 10


def test_reports_list_modes_are_mutually_exclusive(tmp_path: Path) -> None:
    result = _invoke(["reports", "list", "--short", "--verbose"], tmp_path / "home")

    assert result.exit_code != 0
    assert "mutually exclusive" in result.output.lower()


def test_reports_list_machine_output_is_structured(tmp_path: Path) -> None:
    home = tmp_path / "home"
    service = CoreConfigService(jelica_home=home)
    service.initialize_system_config()
    reports = home / "reports"
    reports.mkdir()
    (reports / "example.pdf").write_bytes(b"pdf")

    result = _invoke(["reports", "list", "--machine"], home)

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["data"]["reports"][0]["filename"] == "example.pdf"


def test_reports_delete_requires_confirmation_but_yes_deletes_selected_report(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    service = CoreConfigService(jelica_home=home)
    service.initialize_system_config()
    reports = home / "reports"
    reports.mkdir()
    report = reports / "example.pdf"
    report.write_bytes(b"pdf")

    result = _invoke(["reports", "delete", "example.pdf", "--yes"], home)

    assert result.exit_code == 0
    assert not report.exists()
    assert "Reports deleted: 1" in result.stdout


def test_storage_usage_reports_category_excludes_unknown_files(tmp_path: Path) -> None:
    home = tmp_path / "home"
    env = {"JELICA_HOME": str(home)}
    initialized = runner.invoke(
        app,
        ["config", "init", "--non-interactive"],
        env=env,
    )
    assert initialized.exit_code == 0
    reports = home / "reports"
    reports.mkdir()
    (reports / "one.pdf").write_bytes(b"pdf")
    (reports / "notes.bin").write_bytes(b"unknown")

    result = runner.invoke(
        app,
        ["storage", "usage", "--reports", "--verbose"],
        env=env,
    )

    assert result.exit_code == 0
    assert "Reports" in result.stdout
    assert "Reports: 1" in result.stdout
    assert "PDF: 1" in result.stdout
    assert "Other files: 1" in result.stdout
