from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Callable

from jelica_core.result_package import (
    ResultPackageLibraryError,
    content_digest_from_content_id,
    parse_result_package_filename,
    resolve_result_package_path,
)
from jelica_core.system_config import CoreConfigService

from .builder import AnalysisReportBuildError, build_analysis_report_model
from .models import AnalysisReportModel
from .pdf import PdfReportRenderer
from .store import (
    ReportFormat,
    ReportStoreError,
    publish_report_temporary_file,
    report_filename_for_stem,
    resolve_reports_directory,
    validate_report_filename,
    validate_report_stem,
)

_TEMPLATE_VARIABLE_PATTERN = re.compile(r"\{([^{}]+)\}")
_SUPPORTED_TEMPLATE_VARIABLES = frozenset(
    {"name", "filename", "hash", "index", "i", "task_id"}
)


class ReportGenerationErrorCode(StrEnum):
    INVALID_SOURCE = "invalid_source"
    INVALID_FORMAT = "invalid_format"
    UNSUPPORTED_FORMAT = "unsupported_format"
    INVALID_TEMPLATE = "invalid_template"
    INVALID_OUTPUT = "invalid_output"
    OUTPUT_DIRECTORY_NOT_FOUND = "output_directory_not_found"
    BATCH_OUTPUT_CONFLICT = "batch_output_conflict"
    RENDER_FAILED = "render_failed"
    PUBLICATION_FAILED = "publication_failed"


class ReportGenerationError(RuntimeError):
    def __init__(self, *, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class ReportGenerationSource:
    reference: str
    path: Path
    filename_stem: str
    result_name: str | None
    content_id: str
    task_id: str
    model: AnalysisReportModel


@dataclass(frozen=True, slots=True)
class ReportGenerationPlan:
    sources: tuple[ReportGenerationSource, ...]
    report_format: ReportFormat
    output_directory: Path
    exact_filename: str | None
    managed_output: bool
    name_template: str | None
    force: bool
    rendered_stems: tuple[str | None, ...]


@dataclass(frozen=True, slots=True)
class ReportGenerationItemOutcome:
    reference: str
    content_id: str
    status: str
    path: Path | None = None
    error: str | None = None
    opened: bool | None = None
    open_warning: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "reference": self.reference,
            "content_id": self.content_id,
            "status": self.status,
            "path": str(self.path.resolve(strict=False)) if self.path is not None else None,
            "error": self.error,
            "opened": self.opened,
            "open_warning": self.open_warning,
        }


@dataclass(frozen=True, slots=True)
class ReportGenerationBatchOutcome:
    items: tuple[ReportGenerationItemOutcome, ...]

    @property
    def generated_count(self) -> int:
        return sum(item.status == "generated" for item in self.items)

    @property
    def failed_count(self) -> int:
        return sum(item.status == "failed" for item in self.items)

    def to_dict(self) -> dict[str, object]:
        return {
            "generated": self.generated_count,
            "failed": self.failed_count,
            "reports": [item.to_dict() for item in self.items],
        }


def plan_report_generation(
    *,
    references: tuple[str, ...],
    format_name: str = "pdf",
    name_template: str | None = None,
    output: str | None = None,
    force: bool = False,
    core_config_service: CoreConfigService,
) -> ReportGenerationPlan:
    if not references:
        raise ReportGenerationError(
            code=ReportGenerationErrorCode.INVALID_SOURCE,
            message="At least one result reference is required.",
        )
    report_format = _parse_generation_format(format_name)
    template = _normalize_template(name_template)
    sources = _resolve_and_validate_sources(
        references=references,
        core_config_service=core_config_service,
    )
    rendered_stems = tuple(
        _render_report_stem(
            source=source,
            template=template,
            index=index,
        )
        for index, source in enumerate(sources, start=1)
    )
    output_directory, exact_filename, managed_output = _resolve_output_mode(
        output=output,
        report_format=report_format,
        source_count=len(sources),
        name_template=template,
        core_config_service=core_config_service,
    )

    if (
        force
        and exact_filename is None
        and len({stem for stem in rendered_stems}) != len(rendered_stems)
    ):
        raise ReportGenerationError(
            code=ReportGenerationErrorCode.BATCH_OUTPUT_CONFLICT,
            message=(
                "--force with a batch requires unique report filenames; use "
                "{index}, {hash}, or {name}."
            ),
        )
    if exact_filename is not None:
        try:
            validate_report_filename(
                filename=exact_filename,
                report_format=report_format,
            )
        except ValueError as error:
            raise ReportGenerationError(
                code=ReportGenerationErrorCode.INVALID_OUTPUT,
                message=str(error),
            ) from error
    else:
        for stem in rendered_stems:
            if stem is not None:
                try:
                    report_filename_for_stem(stem=stem, report_format=report_format)
                except ValueError as error:
                    error_code = (
                        ReportGenerationErrorCode.INVALID_TEMPLATE
                        if template is not None
                        else ReportGenerationErrorCode.INVALID_SOURCE
                    )
                    raise ReportGenerationError(
                        code=error_code,
                        message=str(error),
                    ) from error

    return ReportGenerationPlan(
        sources=sources,
        report_format=report_format,
        output_directory=output_directory,
        exact_filename=exact_filename,
        managed_output=managed_output,
        name_template=template,
        force=force,
        rendered_stems=rendered_stems,
    )


def generate_reports(
    *,
    plan: ReportGenerationPlan,
    report_opener: Callable[[Path], object] | None = None,
) -> ReportGenerationBatchOutcome:
    outcomes: list[ReportGenerationItemOutcome] = []
    for source, rendered_stem in zip(plan.sources, plan.rendered_stems, strict=True):
        try:
            payload = _render_source(source=source, report_format=plan.report_format)
            temporary_path = _write_temporary_report(
                payload=payload,
                directory=plan.output_directory,
            )
            try:
                published_path = publish_report_temporary_file(
                    temporary_path=temporary_path,
                    directory=plan.output_directory,
                    base_stem=rendered_stem or source.filename_stem,
                    report_format=plan.report_format,
                    force=plan.force,
                    exact_filename=plan.exact_filename,
                    managed=plan.managed_output,
                )
            finally:
                temporary_path.unlink(missing_ok=True)
        except (ReportGenerationError, ReportStoreError, OSError, ValueError) as error:
            outcomes.append(
                ReportGenerationItemOutcome(
                    reference=source.reference,
                    content_id=source.content_id,
                    status="failed",
                    error=str(error),
                )
            )
            continue

        opened: bool | None = None
        open_warning: str | None = None
        if report_opener is not None:
            try:
                opener_result = report_opener(published_path)
                opened = bool(getattr(opener_result, "opened", opener_result))
                if not opened:
                    open_warning = "Report could not be opened automatically."
            except Exception as error:  # opener failures must not undo generation
                opened = False
                open_warning = str(error)
        outcomes.append(
            ReportGenerationItemOutcome(
                reference=source.reference,
                content_id=source.content_id,
                status="generated",
                path=published_path,
                opened=opened,
                open_warning=open_warning,
            )
        )
    return ReportGenerationBatchOutcome(items=tuple(outcomes))


def _resolve_and_validate_sources(
    *,
    references: tuple[str, ...],
    core_config_service: CoreConfigService,
) -> tuple[ReportGenerationSource, ...]:
    sources: list[ReportGenerationSource] = []
    seen_content_ids: set[str] = set()
    for reference in references:
        path = _resolve_source_path(
            reference=reference,
            core_config_service=core_config_service,
        )
        try:
            model = build_analysis_report_model(package_path=path)
        except AnalysisReportBuildError as error:
            raise ReportGenerationError(
                code=ReportGenerationErrorCode.INVALID_SOURCE,
                message=f"Result package '{reference}' is invalid: {error}",
            ) from error
        content_id = model.metadata.content_id
        if content_id in seen_content_ids:
            continue
        seen_content_ids.add(content_id)
        filename_stem = _source_filename_stem(path=path)
        result_name = _source_result_name(path=path, content_id=content_id)
        try:
            safe_filename_stem = validate_report_stem(
                filename_stem,
                field_name="Source filename stem",
            )
        except ValueError as error:
            raise ReportGenerationError(
                code=ReportGenerationErrorCode.INVALID_SOURCE,
                message=f"Source '{reference}' has an unsafe filename stem; use --name.",
            ) from error
        sources.append(
            ReportGenerationSource(
                reference=reference,
                path=path,
                filename_stem=safe_filename_stem,
                result_name=result_name,
                content_id=content_id,
                task_id=model.metadata.task_id,
                model=model,
            )
        )
    return tuple(sources)


def _resolve_source_path(*, reference: str, core_config_service: CoreConfigService) -> Path:
    normalized = reference.strip()
    if normalized == "":
        raise ReportGenerationError(
            code=ReportGenerationErrorCode.INVALID_SOURCE,
            message="Result package source must not be empty.",
        )
    if _looks_like_package_path(normalized):
        candidate = Path(normalized).expanduser().resolve(strict=False)
        if candidate.is_file() or "/" in normalized or "\\" in normalized:
            return candidate
        try:
            return resolve_result_package_path(
                task_or_content_ref=normalized,
                core_config_service=core_config_service,
            ).path.resolve(strict=False)
        except ResultPackageLibraryError:
            return candidate
    try:
        return resolve_result_package_path(
            task_or_content_ref=normalized,
            core_config_service=core_config_service,
        ).path.resolve(strict=False)
    except ResultPackageLibraryError as error:
        raise ReportGenerationError(
            code=ReportGenerationErrorCode.INVALID_SOURCE,
            message=f"Cannot resolve result reference '{reference}': {error}",
        ) from error


def _render_source(*, source: ReportGenerationSource, report_format: ReportFormat) -> bytes:
    if report_format is not ReportFormat.PDF:
        raise ReportGenerationError(
            code=ReportGenerationErrorCode.UNSUPPORTED_FORMAT,
            message=(
                f"Report format '{report_format.value}' is not supported yet; "
                "only pdf is available."
            ),
        )
    try:
        payload = PdfReportRenderer().render(model=source.model)
    except Exception as error:
        raise ReportGenerationError(
            code=ReportGenerationErrorCode.RENDER_FAILED,
            message="PDF report could not be rendered.",
        ) from error
    if len(payload) < 64 or not payload.startswith(b"%PDF-") or b"%%EOF" not in payload[-4096:]:
        raise ReportGenerationError(
            code=ReportGenerationErrorCode.RENDER_FAILED,
            message="Rendered output does not have a valid PDF structure.",
        )
    return payload


def _write_temporary_report(*, payload: bytes, directory: Path) -> Path:
    try:
        directory.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="wb",
            delete=False,
            dir=directory,
            prefix="jelica-report.",
            suffix=".tmp",
        ) as file:
            file.write(payload)
            file.flush()
            os.fsync(file.fileno())
            return Path(file.name)
    except OSError as error:
        raise ReportGenerationError(
            code=ReportGenerationErrorCode.PUBLICATION_FAILED,
            message="Temporary report file could not be created.",
        ) from error


def _parse_generation_format(value: str) -> ReportFormat:
    normalized = value.strip().casefold()
    try:
        report_format = ReportFormat(normalized)
    except ValueError as error:
        raise ReportGenerationError(
            code=ReportGenerationErrorCode.INVALID_FORMAT,
            message=f"Unknown report format '{value}'. Allowed values: pdf, html, xml, txt.",
        ) from error
    if report_format is not ReportFormat.PDF:
        raise ReportGenerationError(
            code=ReportGenerationErrorCode.UNSUPPORTED_FORMAT,
            message=(
                f"Report format '{report_format.value}' is not supported yet; "
                "only pdf is available."
            ),
        )
    return report_format


def _normalize_template(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    if normalized == "":
        raise ReportGenerationError(
            code=ReportGenerationErrorCode.INVALID_TEMPLATE,
            message="--name must not be empty.",
        )
    if "\x00" in normalized or "/" in normalized or "\\" in normalized:
        raise ReportGenerationError(
            code=ReportGenerationErrorCode.INVALID_TEMPLATE,
            message="--name must be a safe filename stem/template.",
        )
    if normalized.casefold().endswith((".pdf", ".html", ".xml", ".txt")):
        raise ReportGenerationError(
            code=ReportGenerationErrorCode.INVALID_TEMPLATE,
            message="--name must specify a filename stem without extension.",
        )
    variables = set(_TEMPLATE_VARIABLE_PATTERN.findall(normalized))
    unsupported = sorted(variables.difference(_SUPPORTED_TEMPLATE_VARIABLES))
    if unsupported:
        raise ReportGenerationError(
            code=ReportGenerationErrorCode.INVALID_TEMPLATE,
            message="Unknown report name template variable(s): " + ", ".join(unsupported),
        )
    return normalized


def _render_report_stem(
    *,
    source: ReportGenerationSource,
    template: str | None,
    index: int,
) -> str | None:
    if template is None:
        return source.filename_stem
    if "{name}" in template and source.result_name is None:
        raise ReportGenerationError(
            code=ReportGenerationErrorCode.INVALID_TEMPLATE,
            message=(
                f"Result name is unavailable for source '{source.reference}'; "
                "{name} cannot be used."
            ),
        )
    replacements = {
        "name": source.result_name or "",
        "filename": source.filename_stem,
        "hash": content_digest_from_content_id(source.content_id),
        "index": str(index),
        "i": str(index),
        "task_id": source.task_id,
    }
    rendered = _TEMPLATE_VARIABLE_PATTERN.sub(
        lambda match: replacements[match.group(1)],
        template,
    )
    try:
        return validate_report_stem(rendered, field_name="Rendered report name")
    except ValueError as error:
        raise ReportGenerationError(
            code=ReportGenerationErrorCode.INVALID_TEMPLATE,
            message=str(error),
        ) from error


def _resolve_output_mode(
    *,
    output: str | None,
    report_format: ReportFormat,
    source_count: int,
    name_template: str | None,
    core_config_service: CoreConfigService,
) -> tuple[Path, str | None, bool]:
    if output is None:
        return (
            resolve_reports_directory(core_config_service=core_config_service),
            None,
            True,
        )
    normalized = output.strip()
    if normalized == "":
        raise ReportGenerationError(
            code=ReportGenerationErrorCode.INVALID_OUTPUT,
            message="--output must not be empty.",
        )
    candidate = Path(normalized).expanduser()
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    candidate = candidate.resolve(strict=False)
    if candidate.exists():
        if candidate.is_dir():
            return candidate, None, _is_managed_reports_directory(
                candidate=candidate,
                core_config_service=core_config_service,
            )
        if candidate.is_file():
            if source_count != 1:
                raise ReportGenerationError(
                    code=ReportGenerationErrorCode.BATCH_OUTPUT_CONFLICT,
                    message="Multiple sources require --output to be a directory.",
                )
            if name_template is not None:
                raise ReportGenerationError(
                    code=ReportGenerationErrorCode.BATCH_OUTPUT_CONFLICT,
                    message="--output exact file and --name cannot be used together.",
                )
            _validate_exact_output(candidate=candidate, report_format=report_format)
            return candidate.parent, candidate.name, _is_managed_reports_directory(
                candidate=candidate.parent,
                core_config_service=core_config_service,
            )
        raise ReportGenerationError(
            code=ReportGenerationErrorCode.INVALID_OUTPUT,
            message="--output must identify a regular file or directory.",
        )

    if candidate.suffix.casefold() == report_format.extension:
        if source_count != 1:
            raise ReportGenerationError(
                code=ReportGenerationErrorCode.BATCH_OUTPUT_CONFLICT,
                message="Multiple sources require --output to be a directory.",
            )
        if name_template is not None:
            raise ReportGenerationError(
                code=ReportGenerationErrorCode.BATCH_OUTPUT_CONFLICT,
                message="--output exact file and --name cannot be used together.",
            )
        _validate_exact_output(candidate=candidate, report_format=report_format)
        if not candidate.parent.is_dir():
            raise ReportGenerationError(
                code=ReportGenerationErrorCode.OUTPUT_DIRECTORY_NOT_FOUND,
                message="Output directory does not exist.",
            )
        return candidate.parent, candidate.name, _is_managed_reports_directory(
            candidate=candidate.parent,
            core_config_service=core_config_service,
        )
    raise ReportGenerationError(
        code=ReportGenerationErrorCode.INVALID_OUTPUT,
        message="Nonexistent --output paths must end with .pdf or name an existing directory.",
    )


def _validate_exact_output(*, candidate: Path, report_format: ReportFormat) -> None:
    if candidate.suffix.casefold() != report_format.extension:
        raise ReportGenerationError(
            code=ReportGenerationErrorCode.INVALID_OUTPUT,
            message=f"Output filename must end with {report_format.extension}.",
        )
    try:
        validate_report_filename(
            filename=candidate.name,
            report_format=report_format,
        )
    except ValueError as error:
        raise ReportGenerationError(
            code=ReportGenerationErrorCode.INVALID_OUTPUT,
            message=str(error),
        ) from error


def _is_managed_reports_directory(
    *,
    candidate: Path,
    core_config_service: CoreConfigService,
) -> bool:
    return candidate.resolve(strict=False) == resolve_reports_directory(
        core_config_service=core_config_service
    ).resolve(strict=False)


def _source_filename_stem(*, path: Path) -> str:
    if path.suffix.casefold() != ".jelica":
        raise ReportGenerationError(
            code=ReportGenerationErrorCode.INVALID_SOURCE,
            message="Result package source must have the .jelica extension.",
        )
    return path.name[: -len(path.suffix)]


def _source_result_name(*, path: Path, content_id: str) -> str | None:
    parsed = parse_result_package_filename(path.name)
    digest = content_digest_from_content_id(content_id)
    if parsed is not None and parsed.content_digest == digest:
        return parsed.result_name
    stem = path.name[: -len(path.suffix)]
    if stem == digest:
        return None
    try:
        return validate_report_stem(stem, field_name="Result name")
    except ValueError:
        return None


def _looks_like_package_path(value: str) -> bool:
    return (
        value.lower().endswith(".jelica")
        or value.startswith((".", "~", "/"))
        or "/" in value
        or "\\" in value
        or Path(value).is_absolute()
    )


__all__ = [
    "ReportGenerationBatchOutcome",
    "ReportGenerationError",
    "ReportGenerationErrorCode",
    "ReportGenerationItemOutcome",
    "ReportGenerationPlan",
    "ReportGenerationSource",
    "generate_reports",
    "plan_report_generation",
]
