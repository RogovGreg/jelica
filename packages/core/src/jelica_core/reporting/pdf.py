from __future__ import annotations

import os
import tempfile
import textwrap
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Callable

from .builder import (
    AnalysisReportBuildError,
    AnalysisReportBuildErrorCode,
    build_analysis_report_model,
)
from .models import AnalysisReportModel, PairwiseReportItem, ReportMessage, ReportStage


class ReportExportErrorCode(StrEnum):
    INVALID_SOURCE_PACKAGE = "invalid_source_package"
    INVALID_OUTPUT_PATH = "invalid_output_path"
    INVALID_OPEN_VALUE = "invalid_open_value"
    OUTPUT_DIRECTORY_NOT_FOUND = "output_directory_not_found"
    REPORT_MODEL_BUILD_FAILED = "report_model_build_failed"
    PDF_RENDER_FAILED = "pdf_render_failed"
    PDF_VALIDATION_FAILED = "pdf_validation_failed"
    PDF_PUBLICATION_FAILED = "pdf_publication_failed"
    OUTPUT_PUBLICATION_FAILED = "output_publication_failed"


class ReportExportError(RuntimeError):
    def __init__(self, *, code: ReportExportErrorCode, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class AnalysisReportPdfExportOutcome:
    output_path: Path
    report_model: AnalysisReportModel


@dataclass(frozen=True, slots=True)
class _TextCommand:
    x: float
    y: float
    font: str
    size: int
    text: str


@dataclass(frozen=True, slots=True)
class _LineCommand:
    x1: float
    y1: float
    x2: float
    y2: float


@dataclass(frozen=True, slots=True)
class _FontStyle:
    font: str
    size: int
    line_height: float


class PdfReportRenderer:
    PAGE_WIDTH = 595.0
    PAGE_HEIGHT = 842.0
    LEFT_MARGIN = 52.0
    RIGHT_MARGIN = 52.0
    TOP_MARGIN = 56.0
    BOTTOM_MARGIN = 44.0
    FOOTER_HEIGHT = 24.0
    INDENT_WIDTH = 14.0

    TITLE_STYLE = _FontStyle(font="F2", size=20, line_height=28.0)
    SECTION_STYLE = _FontStyle(font="F2", size=14, line_height=20.0)
    SUBSECTION_STYLE = _FontStyle(font="F2", size=12, line_height=18.0)
    BODY_STYLE = _FontStyle(font="F1", size=11, line_height=14.5)
    METRIC_STYLE = _FontStyle(font="F2", size=11, line_height=14.5)
    FOOTER_STYLE = _FontStyle(font="F1", size=9, line_height=11.0)

    def render(self, *, model: AnalysisReportModel) -> bytes:
        pages = self._layout_pages(model=model)
        return _build_pdf_document(pages=pages)

    def _layout_pages(
        self, *, model: AnalysisReportModel
    ) -> list[list[_TextCommand | _LineCommand]]:
        pages: list[list[_TextCommand | _LineCommand]] = [[]]
        cursor_y = self.PAGE_HEIGHT - self.TOP_MARGIN

        def ensure_room(*, required_height: float) -> None:
            nonlocal cursor_y
            threshold = self.BOTTOM_MARGIN + self.FOOTER_HEIGHT
            if cursor_y - required_height >= threshold:
                return
            pages.append([])
            cursor_y = self.PAGE_HEIGHT - self.TOP_MARGIN

        def add_vertical_space(amount: float) -> None:
            nonlocal cursor_y
            ensure_room(required_height=amount)
            cursor_y -= amount

        def add_line(
            *,
            text: str,
            style: _FontStyle,
            indent: int = 0,
            gap_before: float = 0.0,
            gap_after: float = 0.0,
        ) -> None:
            nonlocal cursor_y
            if gap_before > 0:
                add_vertical_space(gap_before)

            wrapped = _wrap_text(
                text=text,
                style=style,
                indent=indent,
                page_width=self.PAGE_WIDTH,
                left_margin=self.LEFT_MARGIN,
                right_margin=self.RIGHT_MARGIN,
                indent_width=self.INDENT_WIDTH,
            )
            x = self.LEFT_MARGIN + (indent * self.INDENT_WIDTH)
            for chunk in wrapped:
                ensure_room(required_height=style.line_height)
                pages[-1].append(
                    _TextCommand(
                        x=x,
                        y=cursor_y,
                        font=style.font,
                        size=style.size,
                        text=chunk,
                    )
                )
                cursor_y -= style.line_height

            if gap_after > 0:
                add_vertical_space(gap_after)

        def add_separator() -> None:
            nonlocal cursor_y
            ensure_room(required_height=8.0)
            y = cursor_y + 6.0
            pages[-1].append(
                _LineCommand(
                    x1=self.LEFT_MARGIN,
                    y1=y,
                    x2=self.PAGE_WIDTH - self.RIGHT_MARGIN,
                    y2=y,
                )
            )

        add_line(text="JELICA Analysis Report", style=self.TITLE_STYLE)
        add_line(text="Package metadata", style=self.SECTION_STYLE, gap_before=2.0)
        add_separator()
        add_line(text=f"Task ID: {model.metadata.task_id}", style=self.METRIC_STYLE)
        add_line(text=f"Task status: {model.metadata.task_status}", style=self.METRIC_STYLE)
        add_line(
            text=f"Created at: {_format_timestamp(model.metadata.created_at)}",
            style=self.METRIC_STYLE,
        )
        add_line(
            text=f"Completed at: {_format_timestamp(model.metadata.completed_at)}",
            style=self.METRIC_STYLE,
        )
        add_line(text=f"Content ID: {model.metadata.content_id}", style=self.METRIC_STYLE)
        add_line(
            text=f"Package format version: {model.metadata.format_version}", style=self.METRIC_STYLE
        )
        add_line(
            text=f"Producer version: {model.metadata.producer_version}", style=self.METRIC_STYLE
        )
        add_line(
            text=f"Package created at: {_format_timestamp(model.metadata.package_created_at)}",
            style=self.METRIC_STYLE,
        )
        add_line(text=f"Number of stages: {model.metadata.stage_count}", style=self.METRIC_STYLE)
        add_line(
            text=f"Number of protected artifacts: {model.metadata.protected_artifact_count}",
            style=self.METRIC_STYLE,
            gap_after=6.0,
        )

        summary = _compute_summary(model.stages)
        add_line(text="Analysis summary", style=self.SECTION_STYLE, gap_before=4.0)
        add_separator()
        add_line(text=f"Completed stages: {summary.completed_stages}", style=self.METRIC_STYLE)
        add_line(
            text=f"Stages with warnings: {summary.stages_with_warnings}", style=self.METRIC_STYLE
        )
        add_line(
            text=f"Failed or unavailable stages: {summary.failed_or_unavailable_stages}",
            style=self.METRIC_STYLE,
        )
        add_line(text=f"Total warnings: {summary.total_warnings}", style=self.METRIC_STYLE)
        add_line(
            text=f"Total errors: {summary.total_errors}", style=self.METRIC_STYLE, gap_after=6.0
        )

        for index, stage in enumerate(model.stages, start=1):
            add_line(
                text=f"{index}. {stage.display_name}",
                style=self.SECTION_STYLE,
                gap_before=5.0,
            )
            add_separator()
            add_line(text=f"Status: {stage.status}", style=self.BODY_STYLE)

            if len(stage.metrics) > 0:
                add_line(text="Metrics", style=self.SUBSECTION_STYLE, gap_before=2.0)
                for metric in stage.metrics:
                    add_line(
                        text=f"{metric.label}: {metric.value}", style=self.METRIC_STYLE, indent=1
                    )

            add_line(text="Warnings", style=self.SUBSECTION_STYLE, gap_before=2.0)
            if len(stage.warnings) == 0:
                add_line(text="Warnings: None", style=self.BODY_STYLE, indent=1)
            else:
                for warning in stage.warnings:
                    add_line(text=_format_message(message=warning), style=self.BODY_STYLE, indent=1)

            add_line(text="Errors", style=self.SUBSECTION_STYLE, gap_before=2.0)
            if len(stage.errors) == 0:
                add_line(text="Errors: None", style=self.BODY_STYLE, indent=1)
            else:
                for error in stage.errors:
                    add_line(text=_format_message(message=error), style=self.BODY_STYLE, indent=1)

            if len(stage.details) > 0:
                add_line(text="Details", style=self.SUBSECTION_STYLE, gap_before=2.0)
                for detail in stage.details:
                    add_line(text=detail, style=self.BODY_STYLE, indent=1)

            add_vertical_space(4.0)

        add_line(text="Pairwise comparisons", style=self.SECTION_STYLE, gap_before=4.0)
        add_separator()
        if len(model.pairwise_comparisons) == 0:
            add_line(text="No pairwise comparisons are available.", style=self.BODY_STYLE)
        else:
            for item in model.pairwise_comparisons:
                self._render_pairwise_item(
                    item=item, add_line=add_line, add_vertical_space=add_vertical_space
                )

        total_pages = len(pages)
        for page_index, commands in enumerate(pages, start=1):
            footer_text = f"Page {page_index} of {total_pages}"
            commands.append(
                _TextCommand(
                    x=self.PAGE_WIDTH / 2.0 - 28.0,
                    y=self.BOTTOM_MARGIN / 2.0,
                    font=self.FOOTER_STYLE.font,
                    size=self.FOOTER_STYLE.size,
                    text=footer_text,
                )
            )
        return pages

    def _render_pairwise_item(
        self,
        *,
        item: PairwiseReportItem,
        add_line: Callable[..., None],
        add_vertical_space: Callable[[float], None],
    ) -> None:
        add_line(
            text=f"{item.left_sequence_id} <-> {item.right_sequence_id}",
            style=self.SUBSECTION_STYLE,
            gap_before=3.0,
        )
        add_line(text=f"Status: {item.status}", style=self.BODY_STYLE, indent=1)
        add_line(
            text=f"Compared length: {_format_optional_int(item.compared_length)}",
            style=self.BODY_STYLE,
            indent=1,
        )
        add_line(
            text=f"Matches: {_format_optional_int(item.matches)}", style=self.BODY_STYLE, indent=1
        )
        add_line(
            text=f"Identity: {_format_optional_float(item.identity)}",
            style=self.BODY_STYLE,
            indent=1,
        )
        add_line(
            text=f"Substitutions: {_format_optional_int(item.substitutions)}",
            style=self.BODY_STYLE,
            indent=1,
        )
        add_line(
            text=f"Insertions: {_format_optional_int(item.insertions)}",
            style=self.BODY_STYLE,
            indent=1,
        )
        add_line(
            text=f"Deletions: {_format_optional_int(item.deletions)}",
            style=self.BODY_STYLE,
            indent=1,
        )
        add_line(
            text=f"Distance: {_format_optional_float(item.distance)}",
            style=self.BODY_STYLE,
            indent=1,
        )

        if len(item.warnings) == 0:
            add_line(text="Warnings: None", style=self.BODY_STYLE, indent=1)
        else:
            for warning in item.warnings:
                add_line(text=_format_message(message=warning), style=self.BODY_STYLE, indent=1)
        if len(item.errors) == 0:
            add_line(text="Errors: None", style=self.BODY_STYLE, indent=1)
        else:
            for error in item.errors:
                add_line(text=_format_message(message=error), style=self.BODY_STYLE, indent=1)
        add_vertical_space(5.0)


@dataclass(frozen=True, slots=True)
class _StageSummary:
    completed_stages: int
    stages_with_warnings: int
    failed_or_unavailable_stages: int
    total_warnings: int
    total_errors: int


def export_analysis_report_pdf(
    *,
    source_package_path: Path | str,
    output: str | None,
) -> AnalysisReportPdfExportOutcome:
    try:
        report_model = build_analysis_report_model(package_path=source_package_path)
    except AnalysisReportBuildError as error:
        if error.code is AnalysisReportBuildErrorCode.INVALID_SOURCE_PACKAGE:
            raise ReportExportError(
                code=ReportExportErrorCode.INVALID_SOURCE_PACKAGE,
                message=str(error),
            ) from error
        raise ReportExportError(
            code=ReportExportErrorCode.REPORT_MODEL_BUILD_FAILED,
            message=str(error),
        ) from error

    output_path = _resolve_output_path(output=output, content_id=report_model.metadata.content_id)
    temporary_path: Path | None = None
    try:
        temporary_path = _render_report_pdf_to_temporary_file(
            report_model=report_model,
            directory=output_path.parent,
        )
        _validate_rendered_pdf(path=temporary_path)
        _publish_rendered_pdf(temporary_path=temporary_path, output_path=output_path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)

    if not output_path.is_file() or output_path.is_symlink():
        raise ReportExportError(
            code=ReportExportErrorCode.OUTPUT_PUBLICATION_FAILED,
            message="Published report path is not a regular file.",
        )
    return AnalysisReportPdfExportOutcome(output_path=output_path, report_model=report_model)


def _resolve_output_path(*, output: str | None, content_id: str) -> Path:
    if output is None:
        digest = content_id.removeprefix("sha256:")
        return (Path.cwd() / f"jelica-report-{digest}.pdf").resolve(strict=False)

    normalized_output = output.strip()
    if normalized_output == "":
        raise ReportExportError(
            code=ReportExportErrorCode.INVALID_OUTPUT_PATH,
            message="Output path must not be empty.",
        )

    explicit_output = Path(normalized_output).expanduser()
    if not explicit_output.is_absolute():
        explicit_output = Path.cwd() / explicit_output
    normalized_path = explicit_output.resolve(strict=False)
    parent = normalized_path.parent
    if not parent.is_dir():
        raise ReportExportError(
            code=ReportExportErrorCode.OUTPUT_DIRECTORY_NOT_FOUND,
            message="Output directory does not exist.",
        )
    return normalized_path


def _render_report_pdf_to_temporary_file(
    *, report_model: AnalysisReportModel, directory: Path
) -> Path:
    renderer = PdfReportRenderer()
    payload = renderer.render(model=report_model)
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            delete=False,
            dir=directory,
            prefix="jelica-report.",
            suffix=".tmp",
        ) as temporary_file:
            temporary_file.write(payload)
            return Path(temporary_file.name)
    except OSError as error:
        raise ReportExportError(
            code=ReportExportErrorCode.PDF_RENDER_FAILED,
            message="PDF report could not be rendered.",
        ) from error


def _validate_rendered_pdf(*, path: Path) -> None:
    try:
        payload = path.read_bytes()
    except OSError as error:
        raise ReportExportError(
            code=ReportExportErrorCode.PDF_VALIDATION_FAILED,
            message="Rendered PDF report cannot be read for validation.",
        ) from error

    if len(payload) < 64 or not payload.startswith(b"%PDF-"):
        raise ReportExportError(
            code=ReportExportErrorCode.PDF_VALIDATION_FAILED,
            message="Rendered output does not have a valid PDF header.",
        )
    if b"%%EOF" not in payload[-4096:]:
        raise ReportExportError(
            code=ReportExportErrorCode.PDF_VALIDATION_FAILED,
            message="Rendered output does not include a valid PDF trailer.",
        )


def _publish_rendered_pdf(*, temporary_path: Path, output_path: Path) -> None:
    try:
        os.replace(temporary_path, output_path)
    except OSError as error:
        raise ReportExportError(
            code=ReportExportErrorCode.PDF_PUBLICATION_FAILED,
            message="PDF report could not be published atomically.",
        ) from error


def _wrap_text(
    *,
    text: str,
    style: _FontStyle,
    indent: int,
    page_width: float,
    left_margin: float,
    right_margin: float,
    indent_width: float,
) -> tuple[str, ...]:
    if text == "":
        return ("",)
    available_width = page_width - left_margin - right_margin - (indent * indent_width)
    approximate_char_width = style.size * (0.56 if style.font == "F2" else 0.52)
    max_chars = max(18, int(available_width / approximate_char_width))
    wrapped = textwrap.wrap(
        text,
        width=max_chars,
        break_long_words=True,
        break_on_hyphens=False,
    )
    if len(wrapped) == 0:
        return ("",)
    return tuple(wrapped)


def _format_optional_int(value: int | None) -> str:
    if value is None:
        return "n/a"
    return str(value)


def _format_optional_float(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:.6f}".rstrip("0").rstrip(".")


def _format_message(*, message: ReportMessage) -> str:
    if message.code is None:
        return f"- {message.message}"
    return f"- [{message.code}] {message.message}"


def _format_timestamp(value: str) -> str:
    normalized = value.strip()
    if normalized == "":
        return value
    candidate = normalized
    if candidate.endswith("Z"):
        candidate = candidate[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return value
    if parsed.tzinfo is None:
        return value
    utc_value = parsed.astimezone(UTC)
    return utc_value.strftime("%Y-%m-%d %H:%M:%S UTC")


def _compute_summary(stages: tuple[ReportStage, ...]) -> _StageSummary:
    completed = 0
    with_warnings = 0
    failed = 0
    total_warnings = 0
    total_errors = 0
    for stage in stages:
        lowered = stage.status.lower()
        if lowered.startswith("completed"):
            completed += 1
        if "warning" in lowered or "partial" in lowered or len(stage.warnings) > 0:
            with_warnings += 1
        if (
            any(token in lowered for token in ("failed", "error", "unavailable"))
            or len(stage.errors) > 0
        ):
            failed += 1
        total_warnings += len(stage.warnings)
        total_errors += len(stage.errors)
    return _StageSummary(
        completed_stages=completed,
        stages_with_warnings=with_warnings,
        failed_or_unavailable_stages=failed,
        total_warnings=total_warnings,
        total_errors=total_errors,
    )


def _escape_pdf_text(value: str) -> str:
    latin_safe = value.encode("latin-1", errors="replace").decode("latin-1")
    return latin_safe.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _build_pdf_document(*, pages: list[list[_TextCommand | _LineCommand]]) -> bytes:
    page_streams = [_build_page_stream(commands=commands) for commands in pages]
    page_count = len(page_streams)

    objects: list[bytes] = []
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    kids = " ".join(f"{5 + (index * 2)} 0 R" for index in range(page_count))
    objects.append(f"<< /Type /Pages /Count {page_count} /Kids [{kids}] >>".encode("ascii"))
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold >>")

    for index, stream in enumerate(page_streams):
        page_object_id = 5 + (index * 2)
        content_object_id = page_object_id + 1
        page_object = (
            "<< /Type /Page /Parent 2 0 R "
            "/MediaBox [0 0 595 842] "
            "/Resources << /Font << /F1 3 0 R /F2 4 0 R >> >> "
            f"/Contents {content_object_id} 0 R >>"
        ).encode("ascii")
        content_object = (
            b"<< /Length "
            + str(len(stream)).encode("ascii")
            + b" >>\nstream\n"
            + stream
            + b"\nendstream"
        )
        objects.append(page_object)
        objects.append(content_object)

    payload = bytearray()
    payload.extend(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for object_index, body in enumerate(objects, start=1):
        offsets.append(len(payload))
        payload.extend(f"{object_index} 0 obj\n".encode("ascii"))
        payload.extend(body)
        payload.extend(b"\nendobj\n")

    xref_offset = len(payload)
    payload.extend(f"xref\n0 {len(offsets)}\n".encode("ascii"))
    payload.extend(b"0000000000 65535 f \n")
    for object_offset in offsets[1:]:
        payload.extend(f"{object_offset:010d} 00000 n \n".encode("ascii"))
    payload.extend(
        (
            f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF\n"
        ).encode("ascii")
    )
    return bytes(payload)


def _build_page_stream(*, commands: list[_TextCommand | _LineCommand]) -> bytes:
    rows: list[str] = []
    for command in commands:
        if isinstance(command, _TextCommand):
            escaped = _escape_pdf_text(command.text)
            rows.append("BT")
            rows.append(f"/{command.font} {command.size} Tf")
            rows.append(f"1 0 0 1 {command.x:.2f} {command.y:.2f} Tm")
            rows.append(f"({escaped}) Tj")
            rows.append("ET")
            continue
        rows.append("0.85 G")
        rows.append("0.8 w")
        rows.append(f"{command.x1:.2f} {command.y1:.2f} m")
        rows.append(f"{command.x2:.2f} {command.y2:.2f} l")
        rows.append("S")
        rows.append("0 G")
        rows.append("1 w")
    return "\n".join(rows).encode("latin-1")


__all__ = [
    "AnalysisReportPdfExportOutcome",
    "PdfReportRenderer",
    "ReportExportError",
    "ReportExportErrorCode",
    "export_analysis_report_pdf",
]
