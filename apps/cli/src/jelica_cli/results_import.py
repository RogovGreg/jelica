from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from jelica_core.result_package import (
    JelicaPackageValidationResult,
    content_digest_from_content_id,
    parse_result_package_filename,
)
from jelica_core.tasks import (
    normalize_human_readable_name,
    validate_human_readable_name,
    validate_task_name,
)

_TEMPLATE_VARIABLE_PATTERN = re.compile(r"\{([^{}]+)\}")
_SUPPORTED_TEMPLATE_VARIABLES = frozenset({"name", "filename", "hash", "index", "i"})


class ResultImportPlanError(ValueError):
    """Raised for CLI validation errors that must happen before importing."""


@dataclass(frozen=True, slots=True)
class ResultImportGroup:
    source: Path
    template: str | None
    files: tuple[Path, ...]
    is_directory: bool
    error: str | None = None


@dataclass(frozen=True, slots=True)
class ResultImportItem:
    source: Path
    file_path: Path
    template: str | None
    is_directory: bool
    index: int | None


@dataclass(frozen=True, slots=True)
class RenderedResultImportName:
    result_name: str | None
    filename_stem: str | None


def parse_rename_option(
    value: str,
    *,
    implicit_current_directory: bool = False,
) -> tuple[Path, str]:
    if implicit_current_directory:
        if "=" in value:
            raise ResultImportPlanError(
                "When no import source is specified, --rename must contain only "
                "the rename template. Example: --rename=\"{index}_{name}\"."
            )
        if value.strip() == "":
            raise ResultImportPlanError("Rename template must not be empty.")
        return Path("."), value
    if "=" not in value:
        raise ResultImportPlanError(
            "When import sources are specified explicitly, --rename must use "
            "SOURCE=TEMPLATE. Example: --rename=\".={index}_{name}\"."
        )
    source_text, template = value.split("=", maxsplit=1)
    source_text = source_text.strip()
    if source_text == "":
        raise ResultImportPlanError("Rename source must not be empty.")
    if template.strip() == "":
        raise ResultImportPlanError("Rename template must not be empty.")
    return Path(source_text).expanduser(), template


def build_result_import_groups(
    *,
    positional_sources: tuple[Path, ...],
    rename_values: tuple[str, ...],
    implicit_current_directory: bool = False,
) -> tuple[ResultImportGroup, ...]:
    if implicit_current_directory:
        entries: list[tuple[Path, str | None]] = [(Path("."), None)]
        if len(rename_values) > 1:
            raise ResultImportPlanError(
                "Rename for the implicit current-directory source was specified "
                "more than once."
            )
        if rename_values:
            _, template = parse_rename_option(
                rename_values[0],
                implicit_current_directory=True,
            )
            entries[0] = (Path("."), template)
    else:
        entries = [
            (source.expanduser(), None) for source in positional_sources
        ]
        positional_sources_by_path: dict[Path, Path] = {}
        for source, _ in entries:
            resolved_source = source.resolve(strict=False)
            if resolved_source in positional_sources_by_path:
                raise ResultImportPlanError(
                    f"Source '{source}' was specified more than once."
                )
            positional_sources_by_path[resolved_source] = source

        rename_by_path: dict[Path, tuple[Path, str]] = {}
        for value in rename_values:
            rename_source, template = parse_rename_option(value)
            resolved_source = rename_source.resolve(strict=False)
            if resolved_source not in positional_sources_by_path:
                raise ResultImportPlanError(
                    f"Rename source '{rename_source}' is not present in the "
                    "explicitly specified import sources."
                )
            if resolved_source in rename_by_path:
                raise ResultImportPlanError(
                    f"Rename for source '{rename_source}' was specified more "
                    "than once."
                )
            rename_by_path[resolved_source] = (rename_source, template)
        entries = [
            (
                source,
                rename_by_path.get(source.resolve(strict=False), (source, None))[1],
            )
            for source, _ in entries
        ]

    groups: list[ResultImportGroup] = []
    for source, template in entries:
        if source.is_dir() and not source.is_symlink():
            files = tuple(
                sorted(
                    (
                        path
                        for path in source.iterdir()
                        if path.is_file() and path.suffix == ".jelica"
                    ),
                    key=lambda path: path.name,
                )
            )
            groups.append(
                ResultImportGroup(
                    source=source,
                    template=template,
                    files=files,
                    is_directory=True,
                )
            )
            continue
        if source.is_file() or source.is_symlink():
            groups.append(
                ResultImportGroup(
                    source=source,
                    template=template,
                    files=(source,),
                    is_directory=False,
                )
            )
            continue
        groups.append(
            ResultImportGroup(
                source=source,
                template=template,
                files=(source,),
                is_directory=False,
                error="Source does not exist or is not a regular file/directory.",
            )
        )
    return tuple(groups)


def expand_result_import_items(
    *,
    groups: tuple[ResultImportGroup, ...],
    no_name: bool,
) -> tuple[ResultImportItem, ...]:
    items: list[ResultImportItem] = []
    for group in groups:
        if no_name and group.template is not None:
            raise ResultImportPlanError(
                "--no-name and --rename are mutually exclusive."
            )
        if group.error is not None:
            items.append(
                ResultImportItem(
                    source=group.source,
                    file_path=group.source,
                    template=group.template,
                    is_directory=False,
                    index=None,
                )
            )
            continue
        for index, file_path in enumerate(group.files, start=1):
            items.append(
                ResultImportItem(
                    source=group.source,
                    file_path=file_path,
                    template=group.template,
                    is_directory=group.is_directory,
                    index=index if group.is_directory else None,
                )
            )
    return tuple(items)


def validate_import_source(
    *,
    item: ResultImportItem,
    validation: JelicaPackageValidationResult,
) -> str:
    if not item.file_path.is_file() or item.file_path.is_symlink():
        raise ValueError("Source must reference a regular file.")
    if item.file_path.suffix != ".jelica":
        raise ValueError("Source file must have the .jelica extension.")
    if not validation.valid or validation.content_id is None:
        first_code = (
            validation.errors[0].code.value
            if validation.errors
            else "validation_failed"
        )
        raise ValueError(f"Source package failed validation ({first_code}).")
    return validation.content_id


def render_result_import_name(
    *,
    item: ResultImportItem,
    content_id: str,
) -> RenderedResultImportName:
    default_name = _default_result_name(
        filename=item.file_path.name,
        content_id=content_id,
    )
    if item.template is None:
        return RenderedResultImportName(
            result_name=default_name,
            filename_stem=None,
        )

    variables = set(_TEMPLATE_VARIABLE_PATTERN.findall(item.template))
    unsupported = sorted(variables.difference(_SUPPORTED_TEMPLATE_VARIABLES))
    if unsupported:
        raise ValueError(
            "Unsupported rename template variable(s): " + ", ".join(unsupported)
        )
    if not item.is_directory and variables.intersection({"index", "i"}):
        raise ValueError("{index} and {i} are only available for directory imports.")
    if item.is_directory and not variables:
        rendered = f"{item.template}_{item.index}"
    else:
        source_name = default_name or _default_source_stem(
            filename=item.file_path.name,
            content_id=content_id,
        )
        normalized_source_name = normalize_human_readable_name(source_name)
        rendered = item.template
        replacements = {
            "name": normalized_source_name,
            "filename": normalized_source_name,
            "hash": content_digest_from_content_id(content_id),
            "index": str(item.index or ""),
            "i": str(item.index or ""),
        }
        rendered = _TEMPLATE_VARIABLE_PATTERN.sub(
            lambda match: replacements[match.group(1)],
            rendered,
        )
    normalized_rendered = normalize_human_readable_name(rendered)
    validate_human_readable_name(normalized_rendered, max_length=None)
    if item.is_directory and not variables:
        user_supplied_part = normalize_human_readable_name(item.template).strip("_")
    else:
        user_supplied_part = _TEMPLATE_VARIABLE_PATTERN.sub("", item.template)
        user_supplied_part = normalize_human_readable_name(user_supplied_part).strip(
            "_"
        )
    if user_supplied_part and len(user_supplied_part) > 64:
        raise ValueError(
            "result package human-readable name must be at most 64 characters"
        )
    if "{hash}" in item.template:
        return RenderedResultImportName(
            result_name=None,
            filename_stem=normalized_rendered,
        )
    if len(normalized_rendered) > 64:
        return RenderedResultImportName(
            result_name=None,
            filename_stem=(
                f"{normalized_rendered}__{content_digest_from_content_id(content_id)}"
            ),
        )
    return RenderedResultImportName(
        result_name=validate_task_name(normalized_rendered),
        filename_stem=None,
    )


def _default_result_name(*, filename: str, content_id: str) -> str | None:
    parsed = parse_result_package_filename(filename)
    if parsed is not None and parsed.content_digest == content_digest_from_content_id(
        content_id
    ):
        return parsed.result_name
    return validate_task_name(
        _default_source_stem(filename=filename, content_id=content_id)
    )


def _default_source_stem(*, filename: str, content_id: str) -> str:
    _ = content_id
    if not filename.endswith(".jelica"):
        raise ValueError("Source file must have the .jelica extension.")
    return filename[: -len(".jelica")]


__all__ = [
    "RenderedResultImportName",
    "ResultImportGroup",
    "ResultImportItem",
    "ResultImportPlanError",
    "build_result_import_groups",
    "expand_result_import_items",
    "parse_rename_option",
    "render_result_import_name",
    "validate_import_source",
]
