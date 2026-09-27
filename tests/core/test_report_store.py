from __future__ import annotations

from pathlib import Path

import pytest

from jelica_core.reporting import (
    ReportFormat,
    ReportStoreError,
    ReportStoreErrorCode,
    delete_stored_reports,
    list_stored_reports,
    resolve_reports_directory,
    resolve_stored_report,
    validate_report_stem,
)
from jelica_core.system_config import CoreConfigService


def _service(home: Path) -> CoreConfigService:
    service = CoreConfigService(jelica_home=home)
    service.initialize_system_config()
    return service


def test_catalog_lists_recognized_reports_and_skips_unknown_files(tmp_path: Path) -> None:
    service = _service(tmp_path / "home")
    root = resolve_reports_directory(core_config_service=service)
    root.mkdir(parents=True)
    (root / "zeta.txt").write_text("z", encoding="utf-8")
    (root / "Alpha.pdf").write_bytes(b"pdf")
    (root / "notes.bin").write_bytes(b"unknown")
    (root / "temporary.tmp").write_bytes(b"temporary")

    reports = list_stored_reports(core_config_service=service)

    assert [report.filename for report in reports] == ["Alpha.pdf", "zeta.txt"]
    assert reports[0].stem == "Alpha"
    assert reports[0].format is ReportFormat.PDF
    assert reports[0].size_bytes == 3


def test_resolver_supports_filename_stem_and_hashless_canonical_alias(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path / "home")
    root = resolve_reports_directory(core_config_service=service)
    root.mkdir(parents=True)
    digest = "a" * 64
    report = root / f"Experiment_A__{digest}.pdf"
    report.write_bytes(b"pdf")

    assert resolve_stored_report(
        reference=report.name,
        core_config_service=service,
    ).path == report
    assert resolve_stored_report(
        reference=report.stem,
        core_config_service=service,
    ).path == report
    assert resolve_stored_report(
        reference="Experiment_A",
        core_config_service=service,
    ).path == report


def test_resolver_reports_ambiguity_instead_of_picking_first(tmp_path: Path) -> None:
    service = _service(tmp_path / "home")
    root = resolve_reports_directory(core_config_service=service)
    root.mkdir(parents=True)
    (root / f"Experiment_A__{'a' * 64}.pdf").write_bytes(b"one")
    (root / f"Experiment_A__{'b' * 64}.pdf").write_bytes(b"two")

    with pytest.raises(ReportStoreError) as error:
        resolve_stored_report(reference="Experiment_A", core_config_service=service)

    assert error.value.code is ReportStoreErrorCode.AMBIGUOUS_REPORT
    assert "Experiment_A__" in str(error.value)


def test_resolver_rejects_paths_outside_managed_reports_store(tmp_path: Path) -> None:
    service = _service(tmp_path / "home")
    outside = tmp_path / "outside.pdf"
    outside.write_bytes(b"pdf")

    with pytest.raises(ReportStoreError) as error:
        resolve_stored_report(
            reference=str(outside),
            core_config_service=service,
        )

    assert error.value.code is ReportStoreErrorCode.REPORT_OUTSIDE_MANAGED_STORE


def test_delete_deduplicates_references_and_all_requires_force(tmp_path: Path) -> None:
    service = _service(tmp_path / "home")
    root = resolve_reports_directory(core_config_service=service)
    root.mkdir(parents=True)
    (root / "one.pdf").write_bytes(b"one")
    (root / "two.pdf").write_bytes(b"two")
    (root / "notes.bin").write_bytes(b"keep")

    deleted = delete_stored_reports(
        references=("one.pdf", "one", "two.pdf"),
        core_config_service=service,
    )
    assert [item.filename for item in deleted] == ["one.pdf", "two.pdf"]
    assert (root / "notes.bin").is_file()

    (root / "three.pdf").write_bytes(b"three")
    with pytest.raises(ReportStoreError) as error:
        delete_stored_reports(
            delete_all=True,
            force=False,
            core_config_service=service,
        )
    assert error.value.code is ReportStoreErrorCode.INVALID_REPORT


def test_report_stem_normalization_reuses_task_result_name_rules() -> None:
    assert validate_report_stem("  My   report  ") == "My_report"
    assert validate_report_stem("My__report") == "My__report"
    with pytest.raises(ValueError):
        validate_report_stem("My/report")
