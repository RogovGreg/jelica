from __future__ import annotations

import os
import platform
import subprocess
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Callable

from jelica_core.reporting import (
    ReportExportError,
    ReportExportErrorCode,
    export_analysis_report_pdf,
)
from jelica_core.result_package import resolve_result_package_path
from jelica_core.system_config import CoreConfigService


class ReportOpenWarningCode(StrEnum):
    REPORT_OPEN_FAILED = "report_open_failed"


@dataclass(frozen=True, slots=True)
class ReportOpenResult:
    opened: bool
    warning_code: ReportOpenWarningCode | None = None


@dataclass(frozen=True, slots=True)
class ReportExportOutcome:
    output_path: Path
    open_result: ReportOpenResult | None


@dataclass(frozen=True, slots=True)
class _ResolvedPackageSource:
    path: Path


def export_results_pdf_report(
    *,
    source: str,
    output: str | None,
    open_after_export: bool,
    core_config_service: CoreConfigService,
    report_opener: Callable[[Path], ReportOpenResult] | None = None,
) -> ReportExportOutcome:
    resolved_source = _resolve_source_package(
        source=source,
        core_config_service=core_config_service,
    )
    output_path = _export_report_pdf(
        package_path=resolved_source.path,
        output=output,
    )

    open_result: ReportOpenResult | None = None
    if open_after_export:
        opener = open_report_file if report_opener is None else report_opener
        open_result = opener(output_path)
    return ReportExportOutcome(output_path=output_path, open_result=open_result)


def _export_report_pdf(*, package_path: Path, output: str | None) -> Path:
    outcome = export_analysis_report_pdf(source_package_path=package_path, output=output)
    return outcome.output_path


def open_report_file(path: Path) -> ReportOpenResult:
    normalized_path = path.resolve(strict=False)
    launch_command: list[str] | None
    system_name = platform.system()
    if system_name == "Darwin":
        launch_command = ["open", str(normalized_path)]
    elif system_name == "Windows":
        launch_command = None
    else:
        launch_command = ["xdg-open", str(normalized_path)]

    try:
        if launch_command is None:
            startfile = getattr(os, "startfile", None)
            if startfile is None:
                raise OSError("os.startfile is not available on this platform")
            startfile(str(normalized_path))
        else:
            subprocess.Popen(
                launch_command,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
            )
    except OSError:
        return ReportOpenResult(
            opened=False,
            warning_code=ReportOpenWarningCode.REPORT_OPEN_FAILED,
        )
    return ReportOpenResult(opened=True, warning_code=None)


def _resolve_source_package(
    *,
    source: str,
    core_config_service: CoreConfigService,
) -> _ResolvedPackageSource:
    normalized_source = source.strip()
    if normalized_source == "":
        raise ReportExportError(
            code=ReportExportErrorCode.INVALID_SOURCE_PACKAGE,
            message="Result package source must not be empty.",
        )

    if _looks_like_package_path(normalized_source):
        return _ResolvedPackageSource(
            path=Path(normalized_source).expanduser().resolve(strict=False)
        )

    resolved = resolve_result_package_path(
        task_or_content_ref=normalized_source,
        core_config_service=core_config_service,
    )
    return _ResolvedPackageSource(path=resolved.path.resolve(strict=False))


def _looks_like_package_path(value: str) -> bool:
    if value.lower().endswith(".jelica"):
        return True
    if value.startswith(".") or value.startswith("~"):
        return True
    if "/" in value or "\\" in value:
        return True
    return Path(value).is_absolute()


__all__ = [
    "ReportExportError",
    "ReportExportErrorCode",
    "ReportExportOutcome",
    "ReportOpenResult",
    "ReportOpenWarningCode",
    "export_results_pdf_report",
    "open_report_file",
]
