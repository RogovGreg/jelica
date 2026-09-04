from __future__ import annotations

import hashlib
import json
import sys
from collections.abc import Sequence
from contextvars import Token
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from typing import Any, Callable, Final, NoReturn
from uuid import UUID

import typer
from typer._click.exceptions import UsageError
from typer.core import TyperGroup

from jelica_contracts import (
    Event,
    EventComponent,
    EventDefinition,
    JSONValue,
    PublicError,
)
from jelica_core import get_core_info
from jelica_core.analysis import AnalysisPlan, plan_analysis_from_inputs
from jelica_core.events import (
    CoreOperationResult,
    reset_command_id,
    run_add_analytical_task_samples,
    run_cancel_analytical_task,
    run_config_init,
    run_config_path,
    run_config_set,
    run_config_show,
    run_config_unset,
    run_config_validate,
    run_create_analytical_task_from_inputs,
    run_delete_analytical_tasks,
    run_get_analytical_task,
    run_list_analytical_task_jobs,
    run_list_analytical_task_samples,
    run_list_analytical_tasks,
    run_pause_analytical_task,
    run_remove_analytical_task_samples,
    run_reprioritize_analytical_task,
    run_resume_analytical_task,
    run_start_analytical_task,
    run_update_analytical_task,
    set_command_id,
)
from jelica_core.input_sources import InputSourceKind, classify_input_source
from jelica_core.result_package import (
    JelicaPackageValidator,
    ResultPackageLibraryError,
    import_result_package,
    list_result_packages,
    resolve_result_package_path,
)
from jelica_core.runtime import (
    ServiceError,
    ServiceRunningTasksError,
    ServiceStatus,
    TaskDeleteBatchResult,
    TaskDeleteItemResultType,
    TaskReprioritizeResult,
    TaskResumeResult,
    TaskStartResult,
    TaskUpdateResult,
    get_service_status,
    read_service_logs,
    restart_service,
    start_service,
    stop_service,
)
from jelica_core.system_config import (
    DEFAULT_DATA_DIRECTORY,
    DEFAULT_LOG_LEVEL,
    DEFAULT_MAX_PARALLEL_TASKS,
    CoreConfigError,
    CoreConfigService,
    ResolvedCoreConfig,
)
from jelica_core.tasks import (
    AnalyticalTaskJobRecord,
    AnalyticalTaskMutationResult,
    AnalyticalTaskNotFoundError,
    AnalyticalTaskRegistryService,
    AnalyticalTaskSnapshot,
)

from .config_file import ConfigFileReadError, read_config_file_text
from .events import (
    CLI_ANALYZE_ARGUMENT_INVALID,
    CLI_COMMAND_INTERRUPTED,
    CLI_CONFIG_FILE_READ_FAILED,
    CLI_EVENT_CURSOR_NOT_FOUND,
    CLI_INLINE_SEQUENCE_TOO_LONG,
    CLI_INTERNAL_ERROR,
    CLI_RESULT_PACKAGE_RESOLUTION_FAILED,
    CLI_USAGE_ERROR,
)
from .machine_protocol import (
    MACHINE_PROTOCOL_VERSION,
    MachineInvocation,
    create_machine_invocation,
    machine_error_payload,
    machine_success_payload,
    serialize_machine_event,
    serialize_machine_payload,
)
from .results_export import (
    ReportExportError,
    ReportExportErrorCode,
    export_results_pdf_report,
)
from .system_config import CliConfig, CliSystemConfigService
from .terminal import TerminalMode, create_terminal_presenter
from .watcher import (
    EventWatchCursorNotFoundError,
    EventWatchService,
    InactiveTask,
    TaskWatchService,
    WatchTaskRow,
    WatchUpdate,
)

_CLI_PACKAGE_NAME: Final = "jelica-cli"
_CLI_SERVICE_RUNNER_MODULE: Final = "jelica_cli.service_runner"
_ANALYZE_CONTEXT_SETTINGS: Final[dict[str, object]] = {
    "allow_extra_args": True,
    "ignore_unknown_options": True,
    "help_option_names": ["--help"],
}
_ANALYSIS_ALIAS_TARGETS: Final[dict[str, str]] = {
    "align": "alignment",
    "statistics": "sequence_statistics",
    "distance": "distance_matrix",
    "tree": "phylogenetic_tree",
}
_TASK_UPDATE_CONTEXT_SETTINGS: Final[dict[str, object]] = {
    "allow_extra_args": True,
    "ignore_unknown_options": True,
}
_CONFIG_COMMAND_ERROR_PREFIX: Final = "Failed to complete system config operation:"
_CONFIG_VALIDATE_ERROR_PREFIX: Final = "System config is invalid:"
_CLI_INLINE_SEQUENCE_MAX_LENGTH: Final[int] = 128
_BOOTSTRAP_COLOR_ENABLED: Final = True
_BOOTSTRAP_EMOJI_ENABLED: Final = False


@dataclass(frozen=True, slots=True)
class AnalyzeCliArguments:
    config_path: Path | None
    sources: tuple[str, ...]
    raw_overrides: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class TaskUpdateCliArguments:
    config_path: Path | None
    raw_overrides: tuple[str, ...]


class AnalyzeCliArgumentError(ValueError):
    """Raised when analyze CLI arguments are malformed."""


class TaskUpdateCliArgumentError(ValueError):
    """Raised when task update CLI arguments are malformed."""


@dataclass(frozen=True, slots=True)
class WatchCliOutcome:
    rows: tuple[WatchTaskRow, ...]
    missing_task_ids: tuple[str, ...]
    inactive_tasks: tuple[InactiveTask, ...]
    events: tuple[Event, ...]
    interrupted: bool


_TERMINAL = create_terminal_presenter(
    color=_BOOTSTRAP_COLOR_ENABLED,
    emoji=_BOOTSTRAP_EMOJI_ENABLED,
)
_SYSTEM_CONFIG: CliSystemConfigService | None = None
_INVOCATION: MachineInvocation | None = None
_MACHINE_RESPONSE_EMITTED = False


def _start_cli_invocation() -> Token[UUID | None]:
    global _INVOCATION, _MACHINE_RESPONSE_EMITTED, _SYSTEM_CONFIG, _TERMINAL
    _TERMINAL = create_terminal_presenter(
        color=_BOOTSTRAP_COLOR_ENABLED,
        emoji=_BOOTSTRAP_EMOJI_ENABLED,
    )
    _SYSTEM_CONFIG = CliSystemConfigService(on_loaded=_activate_cli_rendering)
    _INVOCATION = create_machine_invocation()
    _MACHINE_RESPONSE_EMITTED = False
    return set_command_id(UUID(_INVOCATION.command_id))


def _current_cli_invocation() -> MachineInvocation:
    global _INVOCATION
    if _INVOCATION is None:
        _INVOCATION = create_machine_invocation()
    return _INVOCATION


def _set_cli_trace_id(trace_id: str | UUID | None) -> None:
    global _INVOCATION
    _INVOCATION = _current_cli_invocation().with_trace_id(trace_id)


def _activate_cli_rendering(config: CliConfig) -> None:
    global _TERMINAL
    _TERMINAL = create_terminal_presenter(color=config.color, emoji=config.emoji)


def _cli_system_config_service() -> CliSystemConfigService:
    global _SYSTEM_CONFIG
    if _SYSTEM_CONFIG is None:
        _SYSTEM_CONFIG = CliSystemConfigService(on_loaded=_activate_cli_rendering)
    return _SYSTEM_CONFIG


def _core_config_service() -> CoreConfigService:
    return _cli_system_config_service().core_service


def _require_cli_system_config() -> None:
    try:
        _cli_system_config_service().load()
    except CoreConfigError as error:
        _TERMINAL.error(str(error))
        raise typer.Exit(code=1) from error


def _resolve_task_references(
    task_references: tuple[str, ...],
    *,
    allow_missing: bool = False,
    output_format: str = "text",
) -> tuple[str, ...]:
    if len(task_references) == 0:
        _exit_with_cli_error(
            definition=CLI_ANALYZE_ARGUMENT_INVALID,
            message_params={"detail": "At least one analytical task reference is required."},
            output_format=output_format,
            verbose=False,
        )

    config_service = _core_config_service()
    try:
        resolved_config = config_service.require_initialized_config()
        registry = AnalyticalTaskRegistryService(database_path=resolved_config.database_path)
    except Exception:
        _exit_with_task_reference_resolution_error(
            task_reference=task_references[0],
            output_format=output_format,
        )

    resolved_task_ids: list[str] = []
    seen_task_ids: set[str] = set()
    for task_reference in task_references:
        try:
            task_id = registry.resolve_task_id(task_reference=task_reference)
        except AnalyticalTaskNotFoundError:
            if not allow_missing:
                _exit_with_task_reference_resolution_error(
                    task_reference=task_reference,
                    output_format=output_format,
                )
            task_id = task_reference.strip()
            if task_id == "":
                _exit_with_task_reference_resolution_error(
                    task_reference=task_reference,
                    output_format=output_format,
                )
        except Exception:
            _exit_with_task_reference_resolution_error(
                task_reference=task_reference,
                output_format=output_format,
            )

        deduplication_key = task_id.casefold()
        if deduplication_key in seen_task_ids:
            continue
        seen_task_ids.add(deduplication_key)
        resolved_task_ids.append(task_id)

    if output_format == "machine" and len(resolved_task_ids) == 1:
        try:
            trace_id = registry.get_task_trace_id(task_id=resolved_task_ids[0])
        except AnalyticalTaskNotFoundError:
            trace_id = None
        except Exception as error:
            _exit_with_cli_error(
                definition=CLI_INTERNAL_ERROR,
                message_params={"detail": f"Cannot resolve task trace metadata: {error}"},
                output_format=output_format,
                verbose=False,
                expected=False,
            )
        _set_cli_trace_id(trace_id)
    return tuple(resolved_task_ids)


def _resolve_task_reference(*, task_reference: str, output_format: str = "text") -> str:
    return _resolve_task_references((task_reference,), output_format=output_format)[0]


def _exit_with_task_reference_resolution_error(
    *,
    task_reference: str,
    output_format: str = "text",
) -> NoReturn:
    result = run_get_analytical_task(
        task_id=task_reference,
        core_config_service=_core_config_service(),
    )
    if not result.ok:
        _exit_with_core_failure(
            result=result,
            output_format=output_format,
            verbose=False,
        )
    _exit_with_cli_error(
        definition=CLI_INTERNAL_ERROR,
        message_params={
            "detail": f"Cannot resolve analytical task reference '{task_reference}'.",
        },
        output_format=output_format,
        verbose=False,
        expected=False,
    )


class MachineProtocolTyperGroup(TyperGroup):
    """Convert machine-mode Typer usage failures into one protocol envelope."""

    def main(
        self,
        args: Sequence[str] | None = None,
        prog_name: str | None = None,
        complete_var: str | None = None,
        standalone_mode: bool = True,
        windows_expand_args: bool = True,
        **extra: Any,
    ) -> Any:
        raw_args = sys.argv[1:] if args is None else list(args)
        if "--machine" not in raw_args:
            return super().main(
                args=args,
                prog_name=prog_name,
                complete_var=complete_var,
                standalone_mode=standalone_mode,
                windows_expand_args=windows_expand_args,
                **extra,
            )

        command_id_token = _start_cli_invocation()
        try:
            try:
                result = super().main(
                    args=args,
                    prog_name=prog_name,
                    complete_var=complete_var,
                    standalone_mode=False,
                    windows_expand_args=windows_expand_args,
                    **extra,
                )
            except UsageError as error:
                public_error = _build_cli_public_error(
                    definition=CLI_USAGE_ERROR,
                    message_params={"detail": error.format_message()},
                    expected=True,
                )
                _print_machine_error(error=public_error)
                if standalone_mode:
                    raise SystemExit(error.exit_code) from None
                raise
        finally:
            reset_command_id(command_id_token)

        if isinstance(result, int) and result == 130 and not _MACHINE_RESPONSE_EMITTED:
            public_error = _build_cli_public_error(
                definition=CLI_COMMAND_INTERRUPTED,
                message_params={},
                expected=True,
            )
            _print_machine_error(error=public_error)
        if standalone_mode and isinstance(result, int):
            raise SystemExit(result)
        return result


app = typer.Typer(
    cls=MachineProtocolTyperGroup,
    add_completion=False,
    help="JELICA command-line interface.",
    no_args_is_help=False,
    rich_markup_mode=None,
)
config_app = typer.Typer(
    add_completion=False,
    help="jelica config <command> — Inspect and update system configuration.",
    rich_markup_mode=None,
)
tasks_app = typer.Typer(
    add_completion=False,
    help="jelica tasks <command> — Inspect and manage analytical tasks.",
    rich_markup_mode=None,
)
tasks_samples_app = typer.Typer(
    add_completion=False,
    help="jelica tasks samples <command> — Manage task sources.",
    rich_markup_mode=None,
)
service_app = typer.Typer(
    add_completion=False,
    help="jelica service <command> — Manage the persistent JELICA Service.",
    rich_markup_mode=None,
)
events_app = typer.Typer(
    add_completion=False,
    help="jelica events <command> — Watch persisted JELICA events.",
    rich_markup_mode=None,
)
results_app = typer.Typer(
    add_completion=False,
    help=(
        "jelica results <command> — Validate, import, list, resolve, and export "
        "JELICA result packages."
    ),
    rich_markup_mode=None,
)
app.add_typer(config_app, name="config", rich_help_panel="Configuration")
app.add_typer(tasks_app, name="tasks", rich_help_panel="Task management")
tasks_app.add_typer(
    tasks_samples_app,
    name="samples",
    rich_help_panel="Task configuration",
)
app.add_typer(service_app, name="service", rich_help_panel="Service")
app.add_typer(events_app, name="events", rich_help_panel="Events")
app.add_typer(results_app, name="results", rich_help_panel="Result packages")


def _print_core_version() -> None:
    core_info = get_core_info()
    _TERMINAL.plain(core_info["version"])


def _print_all_versions() -> None:
    cli_version = version(_CLI_PACKAGE_NAME)
    core_info = get_core_info()
    _TERMINAL.plain(f"{_CLI_PACKAGE_NAME} {cli_version}")
    _TERMINAL.plain(f"{core_info['package']} {core_info['version']}")


@app.callback(invoke_without_command=True)
def callback(
    ctx: typer.Context,
    version_flag: bool = typer.Option(
        False,
        "--version",
        help="Show Core version and exit.",
        is_eager=True,
    ),
    all_versions_flag: bool = typer.Option(
        False,
        "--all-versions",
        help="Show versions of all components and exit.",
        is_eager=True,
    ),
) -> None:
    command_id_token = _start_cli_invocation()
    ctx.call_on_close(lambda: reset_command_id(command_id_token))
    if version_flag and all_versions_flag:
        raise typer.BadParameter("Use only one flag: --version or --all-versions")

    if all_versions_flag:
        _print_all_versions()
        raise typer.Exit(code=0)

    if version_flag:
        _print_core_version()
        raise typer.Exit(code=0)


@app.command(
    "about",
    help="jelica about — Show the application name.",
    rich_help_panel="Application information",
)
def about() -> None:
    _require_cli_system_config()
    _TERMINAL.about()


def _print_result_package_library_error(error: ResultPackageLibraryError) -> None:
    _TERMINAL.plain(f"[{error.code.value}] {error}", style="red")


@results_app.command(
    "validate",
    help="jelica results validate FILE.jelica — Validate one JELICA result package.",
    rich_help_panel="Validation",
)
def results_validate(
    package_path: Path = typer.Argument(..., help="Path to .jelica package file."),
) -> None:
    result = JelicaPackageValidator().validate(package_path)
    if result.valid:
        _TERMINAL.success("Valid JELICA result package")
        if result.format_version is not None:
            _TERMINAL.plain(f"Format version: {result.format_version}")
        if result.content_id is not None:
            _TERMINAL.plain(f"Content ID: {result.content_id}")
        raise typer.Exit(code=0)

    _TERMINAL.plain("Invalid JELICA result package", style="red")
    for issue in result.errors:
        if issue.path is None:
            _TERMINAL.plain(f"[{issue.code.value}] {issue.message}")
            continue
        _TERMINAL.plain(f"[{issue.code.value}] {issue.message} ({issue.path})")
    raise typer.Exit(code=1)


@results_app.command(
    "import",
    help="jelica results import FILE.jelica — Import one package into local store.",
    rich_help_panel="Import",
)
def results_import(
    package_path: Path = typer.Argument(..., help="Path to .jelica package file."),
) -> None:
    try:
        outcome = import_result_package(
            source_path=package_path,
            core_config_service=_core_config_service(),
        )
    except ResultPackageLibraryError as error:
        _print_result_package_library_error(error)
        raise typer.Exit(code=1) from error

    if outcome.already_exists:
        _TERMINAL.plain("JELICA result package already exists")
    else:
        _TERMINAL.success("JELICA result package imported")
    _TERMINAL.plain(f"Content ID: {outcome.content_id}")
    _TERMINAL.plain(f"Path: {outcome.path.resolve(strict=False)}")


@results_app.command(
    "list",
    help="jelica results list — List local result packages from central store.",
    rich_help_panel="Catalog",
)
def results_list() -> None:
    try:
        listing = list_result_packages(core_config_service=_core_config_service())
    except ResultPackageLibraryError as error:
        _print_result_package_library_error(error)
        raise typer.Exit(code=1) from error

    if len(listing.packages) == 0:
        _TERMINAL.plain("No JELICA result packages found")
        raise typer.Exit(code=0)

    for index, package in enumerate(listing.packages):
        if index > 0:
            _TERMINAL.plain("")
        _TERMINAL.plain(f"File name: {package.file_name}")
        if package.valid:
            _TERMINAL.plain(f"Content ID: {package.content_id or '-'}")
            _TERMINAL.plain(f"Task ID: {package.task_id or '-'}")
            _TERMINAL.plain(f"Status: {package.status}")
            _TERMINAL.plain(f"Format version: {package.format_version or '-'}")
            continue
        _TERMINAL.plain(f"Status: {package.status}", style="red")
        if package.issue_code is not None:
            _TERMINAL.plain(f"[{package.issue_code.value}] package entry is invalid")

    if listing.has_invalid_entries:
        raise typer.Exit(code=1)
    raise typer.Exit(code=0)


@results_app.command(
    "path",
    help="jelica results path <task-id|content-id> — Resolve local package path.",
    rich_help_panel="Catalog",
)
def results_path(
    task_or_content_ref: str = typer.Argument(
        ...,
        help="Task ID, full content ID (sha256:<digest>), or bare 64-char digest.",
    ),
    machine: bool = typer.Option(
        False,
        "--machine",
        help="Write one machine protocol JSON response.",
    ),
) -> None:
    try:
        resolved = resolve_result_package_path(
            task_or_content_ref=task_or_content_ref,
            core_config_service=_core_config_service(),
        )
    except ResultPackageLibraryError as error:
        if machine:
            machine_error = _build_cli_public_error(
                definition=CLI_RESULT_PACKAGE_RESOLUTION_FAILED,
                message_params={"detail": f"[{error.code.value}] {error}"},
                expected=True,
            ).model_copy(
                update={
                    "safe_details": {
                        "reference": task_or_content_ref,
                        "result_package_error_code": error.code.value,
                    }
                }
            )
            _print_machine_error(error=machine_error)
            raise typer.Exit(code=1) from error
        _print_result_package_library_error(error)
        raise typer.Exit(code=1) from error

    if machine:
        _print_machine_success(
            data={
                "content_id": resolved.content_id,
                "path": str(resolved.path.resolve(strict=False)),
            }
        )
        raise typer.Exit(code=0)

    _TERMINAL.plain(str(resolved.path.resolve(strict=False)))
    raise typer.Exit(code=0)


@results_app.command(
    "export",
    help=(
        "jelica results export SOURCE --format=pdf [--output=report.pdf] [--open=true] "
        "— Export a PDF report."
    ),
    rich_help_panel="Export",
)
def results_export(
    source: str = typer.Argument(
        ...,
        help="Task ID, content ID, bare digest, or direct path to a .jelica package.",
    ),
    format_name: str = typer.Option(
        ...,
        "--format",
        help="Export format. Use --format=pdf.",
    ),
    output: str | None = typer.Option(
        None,
        "--output",
        help="Output report file path. Use --output=report.pdf.",
    ),
    open_option: str = typer.Option(
        "false",
        "--open",
        help="Open the exported report. Use --open=true or --open=false.",
    ),
) -> None:
    if format_name.strip().lower() != "pdf":
        _TERMINAL.plain("[unsupported_export_format] Only --format=pdf is supported.", style="red")
        raise typer.Exit(code=1)

    try:
        open_after_export = _parse_open_option(value=open_option)
        outcome = export_results_pdf_report(
            source=source,
            output=output,
            open_after_export=open_after_export,
            core_config_service=_core_config_service(),
        )
    except ResultPackageLibraryError as error:
        _print_result_package_library_error(error)
        raise typer.Exit(code=1) from error
    except ReportExportError as error:
        _TERMINAL.plain(f"[{error.code.value}] {error}", style="red")
        raise typer.Exit(code=1) from error

    _TERMINAL.success("PDF report created")
    _TERMINAL.plain(f"Path: {outcome.output_path}")
    if outcome.open_result is None:
        raise typer.Exit(code=0)

    if outcome.open_result.opened:
        _TERMINAL.success("Report opened in the default application.")
        raise typer.Exit(code=0)

    warning_code = outcome.open_result.warning_code
    if warning_code is not None:
        _TERMINAL.warning(f"[{warning_code.value}] The report could not be opened automatically.")
    raise typer.Exit(code=0)


def _parse_open_option(*, value: str) -> bool:
    normalized = value.strip().lower()
    if normalized == "true":
        return True
    if normalized == "false":
        return False
    raise ReportExportError(
        code=ReportExportErrorCode.INVALID_OPEN_VALUE,
        message="Option --open must be a boolean (true/false).",
    )


@app.command(
    "tree",
    context_settings=_ANALYZE_CONTEXT_SETTINGS,
    help="jelica tree [CONFIG.json] [SOURCE...] — Analyze through phylogenetic_tree.",
    rich_help_panel="Main commands",
)
@app.command(
    "distance",
    context_settings=_ANALYZE_CONTEXT_SETTINGS,
    help="jelica distance [CONFIG.json] [SOURCE...] — Analyze through distance_matrix.",
    rich_help_panel="Main commands",
)
@app.command(
    "statistics",
    context_settings=_ANALYZE_CONTEXT_SETTINGS,
    help="jelica statistics [CONFIG.json] [SOURCE...] — Analyze through sequence_statistics.",
    rich_help_panel="Main commands",
)
@app.command(
    "align",
    context_settings=_ANALYZE_CONTEXT_SETTINGS,
    help="jelica align [CONFIG.json] [SOURCE...] — Analyze through alignment.",
    rich_help_panel="Main commands",
)
@app.command(
    "analyze",
    context_settings=_ANALYZE_CONTEXT_SETTINGS,
    help="jelica analyze [CONFIG.json] [SOURCE...] — Create, start, and watch one task.",
    rich_help_panel="Main commands",
)
def analyze(
    ctx: typer.Context,
    verbose: bool = typer.Option(
        False,
        "--verbose",
        help="Show additional events and safe diagnostic fields.",
    ),
    quiet: bool = typer.Option(
        False,
        "--quiet",
        help="Show only errors and the required task result.",
    ),
    plan: bool = typer.Option(
        False,
        "--plan",
        help="Show the potential execution plan without creating or starting a task.",
    ),
    show_plan: bool = typer.Option(
        False,
        "--show-plan",
        help="Show the potential execution plan, then create and run the task.",
    ),
    name: str | None = typer.Option(
        None,
        "--name",
        help="Optional human-readable task name.",
    ),
    trace_id: str | None = typer.Option(
        None,
        "--trace-id",
        help="Use an explicit UUID for the analysis lifecycle trace.",
    ),
    target: str | None = typer.Option(
        None,
        "--target",
        help="Stop analytical execution at this target, then package results.",
    ),
    from_phase: str | None = typer.Option(
        None,
        "--from-phase",
        help="Start execution from this phase using committed prerequisites.",
    ),
    machine: bool = typer.Option(
        False,
        "--machine",
        help="Write one machine protocol JSON response.",
    ),
    no_watch: bool = typer.Option(
        False,
        "--no-watch",
        help="Create and start a task without waiting for terminal state.",
    ),
) -> None:
    output_format = "machine" if machine else "text"
    alias_target = _ANALYSIS_ALIAS_TARGETS.get(ctx.info_name or "")
    if alias_target is not None and target is not None and target.strip().lower() != alias_target:
        _exit_with_cli_error(
            definition=CLI_ANALYZE_ARGUMENT_INVALID,
            message_params={
                "detail": (
                    f"Command '{ctx.info_name}' fixes --target={alias_target}; "
                    f"it cannot be combined with --target={target}."
                )
            },
            output_format=output_format,
            verbose=False,
        )
    effective_target = alias_target or target
    if plan and show_plan:
        _exit_with_cli_error(
            definition=CLI_ANALYZE_ARGUMENT_INVALID,
            message_params={"detail": "Use only one flag: --plan or --show-plan."},
            output_format=output_format,
            verbose=False,
        )
    if plan and no_watch:
        _exit_with_cli_error(
            definition=CLI_ANALYZE_ARGUMENT_INVALID,
            message_params={"detail": "--no-watch cannot be combined with --plan."},
            output_format=output_format,
            verbose=False,
        )
    if verbose and quiet:
        _exit_with_cli_error(
            definition=CLI_ANALYZE_ARGUMENT_INVALID,
            message_params={"detail": "Use only one flag: --verbose or --quiet."},
            output_format=output_format,
            verbose=False,
        )
    terminal_mode = (
        TerminalMode.VERBOSE if verbose else TerminalMode.QUIET if quiet else TerminalMode.STANDARD
    )

    raw_arguments = list(ctx.args)
    _reject_removed_analyze_output_flags(raw_arguments, output_format=output_format)

    normalized_trace_id: str | None = None
    if trace_id is not None:
        try:
            normalized_trace_id = str(UUID(trace_id))
        except ValueError:
            _exit_with_cli_error(
                definition=CLI_ANALYZE_ARGUMENT_INVALID,
                message_params={"detail": "--trace-id must be a valid UUID."},
                output_format=output_format,
                verbose=verbose,
            )
    _set_cli_trace_id(normalized_trace_id)

    try:
        parsed_arguments = _parse_analyze_arguments(raw_arguments)
        parsed_arguments = _with_execution_overrides(
            parsed_arguments,
            target=effective_target,
            from_phase=from_phase,
        )
        config_json = _load_config_json(parsed_arguments.config_path)
    except AnalyzeCliArgumentError as error:
        _exit_with_cli_error(
            definition=CLI_ANALYZE_ARGUMENT_INVALID,
            message_params={"detail": str(error)},
            output_format=output_format,
            verbose=verbose,
        )
    except ConfigFileReadError as error:
        _exit_with_cli_error(
            definition=CLI_CONFIG_FILE_READ_FAILED,
            message_params={"detail": str(error)},
            output_format=output_format,
            verbose=verbose,
        )
    except Exception as error:
        _exit_with_cli_error(
            definition=CLI_INTERNAL_ERROR,
            message_params={"detail": str(error)},
            output_format=output_format,
            verbose=verbose,
            expected=False,
        )

    analysis_plan: AnalysisPlan | None = None
    if plan or show_plan:
        analysis_plan = _build_analysis_plan_for_cli(
            parsed_arguments=parsed_arguments,
            config_json=config_json,
            output_format=output_format,
            verbose=verbose,
        )
        if not machine:
            _print_analysis_plan(analysis_plan)
        if plan:
            if machine:
                _print_machine_success(
                    data={"plan": analysis_plan.model_dump(mode="json")},
                    trace_id=normalized_trace_id,
                )
            raise typer.Exit(code=0)

    _validate_cli_inline_sequence_length(
        sources=parsed_arguments.sources,
        output_format=output_format,
        verbose=verbose,
    )

    watch_started_at = datetime.now(UTC)
    result = run_create_analytical_task_from_inputs(
        name=name,
        trace_id=normalized_trace_id,
        config_json=config_json,
        raw_overrides=parsed_arguments.raw_overrides,
        positional_sources=parsed_arguments.sources,
        core_config_service=_core_config_service(),
    )

    if not result.ok:
        _exit_with_core_failure(
            result=result,
            output_format=output_format,
            verbose=verbose,
        )

    task = result.value
    if task is None:
        _exit_with_cli_error(
            definition=CLI_INTERNAL_ERROR,
            message_params={"detail": "Core returned no task payload for successful operation."},
            output_format=output_format,
            verbose=verbose,
            expected=False,
        )

    _set_cli_trace_id(task.config.trace_id)

    if not machine:
        _TERMINAL.analysis_started(task.task_id)
        if task.name is not None:
            _TERMINAL.plain(f"Task name: {task.name}")
        if verbose:
            _print_execution_selection_diagnostics(task.config)

    _ensure_execution_service(
        output_format=output_format,
        verbose=verbose,
        task_id=task.task_id,
    )
    start_result = run_start_analytical_task(
        task_id=task.task_id,
        detached=True,
        background_runner_module=_CLI_SERVICE_RUNNER_MODULE,
        core_config_service=_core_config_service(),
    )

    if not start_result.ok:
        if not machine:
            _TERMINAL.plain(f"task_id: {task.task_id}")
        _exit_with_core_failure(
            result=start_result,
            output_format=output_format,
            verbose=verbose,
        )

    if start_result.value is None:
        _exit_with_cli_error(
            definition=CLI_INTERNAL_ERROR,
            message_params={"detail": "Core returned no start payload for successful operation."},
            output_format=output_format,
            verbose=verbose,
            expected=False,
        )

    if no_watch:
        started_snapshot = _load_execution_task_snapshot(
            task_id=task.task_id,
            expected_job_id=start_result.value.job.job_id,
            output_format=output_format,
            verbose=verbose,
        )
        started_state = started_snapshot.task.state.value
        if machine:
            data: dict[str, Any] = {
                "task": task.model_dump(mode="json"),
                "execution": start_result.value.model_dump(mode="json"),
                "final_state": started_state,
            }
            if analysis_plan is not None:
                data["plan"] = analysis_plan.model_dump(mode="json")
            _print_machine_success(
                data=data,
                trace_id=(str(task.config.trace_id) if task.config.trace_id is not None else None),
            )
            raise typer.Exit(code=0)

        _TERMINAL.plain(f"task_id: {started_snapshot.task.task_id}")
        _TERMINAL.plain(f"state: {started_state}")
        _TERMINAL.success(
            "Task started. Use 'jelica tasks show <task-id>' or 'jelica tasks watch <task-id>'."
        )
        raise typer.Exit(code=0)

    watched = _watch_execution_task(
        task_id=task.task_id,
        event_since=watch_started_at,
        mode=terminal_mode,
        render=not machine,
        output_format=output_format,
        verbose=verbose,
    )

    if watched.interrupted:
        if machine:
            _exit_machine_interrupted(
                task_ids=(task.task_id,),
                trace_id=task.config.trace_id,
            )
        raise typer.Exit(code=130)

    final_row = next((row for row in watched.rows if row.task_id == task.task_id), None)
    if final_row is None:
        if machine:
            _exit_with_cli_error(
                definition=CLI_INTERNAL_ERROR,
                message_params={"detail": "Analysis task has no final runtime state."},
                output_format=output_format,
                verbose=verbose,
                expected=False,
            )
        raise typer.Exit(code=1)
    if machine:
        if final_row.state != "completed":
            _exit_with_cli_error(
                definition=CLI_INTERNAL_ERROR,
                message_params={"detail": f"Analysis task finished in state '{final_row.state}'."},
                output_format=output_format,
                verbose=verbose,
                expected=False,
            )
        data: dict[str, Any] = {
            "task": task.model_dump(mode="json"),
            "execution": start_result.value.model_dump(mode="json"),
            "final_state": final_row.state,
        }
        if analysis_plan is not None:
            data["plan"] = analysis_plan.model_dump(mode="json")
        _print_machine_success(
            data=data,
            trace_id=str(task.config.trace_id) if task.config.trace_id is not None else None,
        )
        raise typer.Exit(code=0)
    raise typer.Exit(code=0 if final_row.state == "completed" else 1)


@tasks_app.command(
    "list",
    help="jelica tasks list — List analytical tasks.",
    rich_help_panel="Inspect and watch",
)
def tasks_list(
    state: list[str] | None = typer.Option(
        None,
        "--state",
        help="Filter by analytical task state. Can be passed multiple times.",
    ),
    limit: int = typer.Option(
        50,
        "--limit",
        help="Maximum number of tasks to return.",
    ),
    offset: int = typer.Option(
        0,
        "--offset",
        help="Number of tasks to skip from the beginning.",
    ),
    machine: bool = typer.Option(
        False,
        "--machine",
        help="Write one machine protocol JSON response.",
    ),
) -> None:
    output_format = "machine" if machine else "text"
    result = run_list_analytical_tasks(
        states=None if state is None else tuple(state),
        limit=limit,
        offset=offset,
        core_config_service=_core_config_service(),
    )
    if not result.ok:
        _exit_with_core_failure(
            result=result,
            output_format=output_format,
            verbose=False,
        )

    tasks = result.value
    if tasks is None:
        _exit_with_cli_error(
            definition=CLI_INTERNAL_ERROR,
            message_params={"detail": "Core returned no tasks payload for successful operation."},
            output_format=output_format,
            verbose=False,
            expected=False,
        )

    if machine:
        machine_tasks = _task_snapshots_machine_payload(
            tasks,
            output_format=output_format,
        )
        _print_machine_success(
            data={
                "tasks": machine_tasks,
                "count": len(machine_tasks),
                "limit": limit,
                "offset": offset,
            },
        )
        return

    if len(tasks) == 0:
        _TERMINAL.plain("Analytical tasks were not found.")
        return

    _print_tasks_list(tasks)


@tasks_app.command(
    "show",
    help="jelica tasks show TASK_REF... — Show one or more analytical tasks.",
    rich_help_panel="Inspect and watch",
)
def tasks_show(
    task_references: list[str] = typer.Argument(
        ...,
        help="One or more analytical task IDs or names.",
    ),
    machine: bool = typer.Option(
        False,
        "--machine",
        help="Write one machine protocol JSON response.",
    ),
) -> None:
    output_format = "machine" if machine else "text"
    task_ids = _resolve_task_references(
        tuple(task_references),
        output_format=output_format,
    )
    task_snapshots: list[AnalyticalTaskSnapshot] = []
    for task_id in task_ids:
        result = run_get_analytical_task(
            task_id=task_id,
            core_config_service=_core_config_service(),
        )
        if not result.ok:
            _exit_with_core_failure(
                result=result,
                output_format=output_format,
                verbose=False,
            )
        task = result.value
        if task is None:
            _exit_with_cli_error(
                definition=CLI_INTERNAL_ERROR,
                message_params={
                    "detail": "Core returned no task payload for successful operation."
                },
                output_format=output_format,
                verbose=False,
                expected=False,
            )
        task_snapshots.append(task)

    if machine:
        machine_tasks = _task_snapshots_machine_payload(
            task_snapshots,
            output_format=output_format,
        )
        trace_id = _single_task_trace_id(machine_tasks)
        _print_machine_success(
            data={"tasks": machine_tasks, "count": len(machine_tasks)},
            trace_id=trace_id,
        )
        return

    for index, task in enumerate(task_snapshots):
        if index > 0:
            _TERMINAL.plain("")
        _print_task_details(task)


@tasks_app.command(
    "jobs",
    help="jelica tasks jobs TASK_REF — Show task job history.",
    rich_help_panel="Inspect and watch",
)
def tasks_jobs(
    task_reference: str = typer.Argument(..., help="Analytical task ID or name."),
    limit: int = typer.Option(
        50,
        "--limit",
        help="Maximum number of jobs to return.",
    ),
    offset: int = typer.Option(
        0,
        "--offset",
        help="Number of jobs to skip from the beginning.",
    ),
    machine: bool = typer.Option(
        False,
        "--machine",
        help="Write one machine protocol JSON response.",
    ),
) -> None:
    output_format = "machine" if machine else "text"
    task_id = _resolve_task_reference(
        task_reference=task_reference,
        output_format=output_format,
    )
    result = run_list_analytical_task_jobs(
        task_id=task_id,
        limit=limit,
        offset=offset,
        core_config_service=_core_config_service(),
    )
    if not result.ok:
        _exit_with_core_failure(
            result=result,
            output_format=output_format,
            verbose=False,
        )

    jobs = result.value
    if jobs is None:
        _exit_with_cli_error(
            definition=CLI_INTERNAL_ERROR,
            message_params={"detail": "Core returned no jobs payload for successful operation."},
            output_format=output_format,
            verbose=False,
            expected=False,
        )

    if machine:
        _print_machine_success(
            data={
                "task_id": task_id,
                "jobs": [job.model_dump(mode="json") for job in jobs],
                "count": len(jobs),
                "limit": limit,
                "offset": offset,
            },
            trace_id=str(result.event.trace_id) if result.event.trace_id is not None else None,
        )
        return

    if len(jobs) == 0:
        _TERMINAL.plain(f"Jobs were not found for task '{task_id}'.")
        return

    _print_task_jobs(task_id=task_id, jobs=jobs)


def _run_task_execution_command(
    *,
    task_references: tuple[str, ...],
    operation: Callable[..., CoreOperationResult[Any]],
    operation_name: str,
    operation_kwargs: dict[str, Any],
    result_printer: Callable[[Any], None],
    machine: bool = False,
) -> None:
    output_format = "machine" if machine else "text"
    task_ids = _resolve_task_references(task_references, output_format=output_format)
    watch_started_at = datetime.now(UTC)
    _ensure_execution_service(
        output_format=output_format,
        verbose=False,
        task_id=task_ids[0],
    )

    operation_results: list[CoreOperationResult[Any]] = []
    lifecycle_results: list[Any] = []
    for task_id in task_ids:
        result = operation(
            task_id=task_id,
            detached=True,
            background_runner_module=_CLI_SERVICE_RUNNER_MODULE,
            core_config_service=_core_config_service(),
            **operation_kwargs,
        )
        if not result.ok:
            _exit_with_core_failure(
                result=result,
                output_format=output_format,
                verbose=False,
            )
        lifecycle_result = result.value
        if lifecycle_result is None:
            _exit_with_cli_error(
                definition=CLI_INTERNAL_ERROR,
                message_params={
                    "detail": (
                        f"Core returned no {operation_name} payload for successful operation."
                    )
                },
                output_format=output_format,
                verbose=False,
                expected=False,
            )
        operation_results.append(result)
        lifecycle_results.append(lifecycle_result)

    watched_task_ids = tuple(item.task.task_id for item in lifecycle_results)
    if len(watched_task_ids) == 1:
        watched = _watch_execution_task(
            task_id=watched_task_ids[0],
            event_since=watch_started_at,
            mode=TerminalMode.STANDARD,
            render=not machine,
            output_format=output_format,
            verbose=False,
        )
    else:
        watched = _watch_execution_tasks(
            task_ids=watched_task_ids,
            event_since=watch_started_at,
            mode=TerminalMode.STANDARD,
            render=not machine,
            output_format=output_format,
            verbose=False,
        )
    if watched.interrupted:
        if machine:
            trace_id = operation_results[0].event.trace_id if len(operation_results) == 1 else None
            _exit_machine_interrupted(
                task_ids=watched_task_ids,
                trace_id=trace_id,
            )
        raise typer.Exit(code=130)

    for lifecycle_result in lifecycle_results:
        final_row = next(
            (row for row in watched.rows if row.job_id == lifecycle_result.job.job_id),
            None,
        )
        if final_row is None or final_row.state != "completed":
            if machine:
                state = "missing" if final_row is None else final_row.state
                _exit_with_cli_error(
                    definition=CLI_INTERNAL_ERROR,
                    message_params={
                        "detail": (
                            f"Task {lifecycle_result.task.task_id} finished in state '{state}'."
                        )
                    },
                    output_format=output_format,
                    verbose=False,
                    expected=False,
                )
            raise typer.Exit(code=1)

    final_results: list[Any] = []
    for lifecycle_result in lifecycle_results:
        final_snapshot = _load_execution_task_snapshot(
            task_id=lifecycle_result.task.task_id,
            expected_job_id=lifecycle_result.job.job_id,
            output_format=output_format,
            verbose=False,
        )
        final_job = final_snapshot.active_or_latest_job
        if final_job is None:
            _exit_with_cli_error(
                definition=CLI_INTERNAL_ERROR,
                message_params={"detail": "Completed task has no latest job payload."},
                output_format=output_format,
                verbose=False,
                expected=False,
            )
        final_results.append(
            lifecycle_result.model_copy(update={"task": final_snapshot.task, "job": final_job})
        )

    if machine:
        machine_trace_id = (
            str(operation_results[0].event.trace_id)
            if len(operation_results) == 1 and operation_results[0].event.trace_id is not None
            else None
        )
        _print_machine_success(
            data={
                "operation": operation_name,
                "tasks": [result.model_dump(mode="json") for result in final_results],
                "count": len(final_results),
            },
            trace_id=machine_trace_id,
        )
        return

    for result, lifecycle_result in zip(
        operation_results,
        final_results,
        strict=True,
    ):
        _print_event(
            result.event,
            system_log_path=result.system_log_path,
            task_log_path=result.task_log_path,
        )
        result_printer(lifecycle_result)
        summary_context = _load_input_processing_summary_context(
            task_id=lifecycle_result.task.task_id,
            job_id=lifecycle_result.job.job_id,
        )
        if summary_context is not None:
            _print_input_processing_terminal_summary(context=summary_context)


@tasks_app.command(
    "start",
    help="jelica tasks start TASK_REF... — Start analytical tasks.",
    rich_help_panel="Execution control",
)
def tasks_start(
    task_references: list[str] = typer.Argument(
        ...,
        help="One or more analytical task IDs or names.",
    ),
    priority: int | None = typer.Option(
        None,
        "--priority",
        help="Override priority for a newly created job.",
    ),
    machine: bool = typer.Option(
        False,
        "--machine",
        help="Write one machine protocol JSON response.",
    ),
) -> None:
    if machine or len(task_references) > 1:
        _run_task_execution_command(
            task_references=tuple(task_references),
            operation=run_start_analytical_task,
            operation_name="start",
            operation_kwargs={"priority": priority},
            result_printer=_print_task_start_result,
            machine=machine,
        )
        return

    task_id = _resolve_task_reference(task_reference=task_references[0])
    watch_started_at = datetime.now(UTC)
    _ensure_execution_service(
        output_format="text",
        verbose=False,
        task_id=task_id,
    )
    result = run_start_analytical_task(
        task_id=task_id,
        priority=priority,
        detached=True,
        background_runner_module=_CLI_SERVICE_RUNNER_MODULE,
        core_config_service=_core_config_service(),
    )
    if not result.ok:
        _exit_with_core_failure(
            result=result,
            output_format="text",
            verbose=False,
        )

    start_result = result.value
    if start_result is None:
        _exit_with_cli_error(
            definition=CLI_INTERNAL_ERROR,
            message_params={
                "detail": "Core returned no start payload for successful operation.",
            },
            output_format="text",
            verbose=False,
            expected=False,
        )

    watched = _watch_execution_task(
        task_id=start_result.task.task_id,
        event_since=watch_started_at,
        mode=TerminalMode.STANDARD,
        render=True,
        output_format="text",
        verbose=False,
    )
    if watched.interrupted:
        raise typer.Exit(code=130)

    final_row = next(
        (row for row in watched.rows if row.job_id == start_result.job.job_id),
        None,
    )
    if final_row is None or final_row.state != "completed":
        raise typer.Exit(code=1)

    final_snapshot = _load_execution_task_snapshot(
        task_id=start_result.task.task_id,
        expected_job_id=start_result.job.job_id,
        output_format="text",
        verbose=False,
    )
    final_job = final_snapshot.active_or_latest_job
    if final_job is None:
        _exit_with_cli_error(
            definition=CLI_INTERNAL_ERROR,
            message_params={"detail": "Completed task has no latest job payload."},
            output_format="text",
            verbose=False,
            expected=False,
        )
    start_result = start_result.model_copy(update={"task": final_snapshot.task, "job": final_job})

    _print_event(
        result.event,
        system_log_path=result.system_log_path,
        task_log_path=result.task_log_path,
    )
    _print_task_start_result(start_result)
    summary_context = _load_input_processing_summary_context(
        task_id=start_result.task.task_id,
        job_id=start_result.job.job_id,
    )
    if summary_context is not None:
        _print_input_processing_terminal_summary(context=summary_context)


@tasks_app.command(
    "delete",
    help="jelica tasks delete TASK_REF... — Delete tasks and associated files.",
    rich_help_panel="Cleanup",
)
def tasks_delete(
    task_references: list[str] = typer.Argument(
        ...,
        help="One or more analytical task IDs or names.",
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        help="Confirm task deletion without interactive prompt.",
    ),
    machine: bool = typer.Option(
        False,
        "--machine",
        help="Write one machine protocol JSON response. Requires --yes.",
    ),
) -> None:
    output_format = "machine" if machine else "text"
    task_ids = _resolve_task_references(
        tuple(task_references),
        allow_missing=True,
        output_format=output_format,
    )
    if machine and not yes:
        _exit_with_cli_error(
            definition=CLI_ANALYZE_ARGUMENT_INVALID,
            message_params={"detail": "tasks delete --machine requires --yes."},
            output_format=output_format,
            verbose=False,
        )
    if not yes:
        confirmed = typer.confirm(
            f"Delete {len(task_ids)} analytical tasks and their associated files?",
            default=False,
        )
        if not confirmed:
            raise typer.Exit(code=1)

    result = run_delete_analytical_tasks(
        task_ids=task_ids,
        core_config_service=_core_config_service(),
    )
    if not result.ok:
        _exit_with_core_failure(
            result=result,
            output_format=output_format,
            verbose=False,
        )

    batch_result = result.value
    if batch_result is None:
        _exit_with_cli_error(
            definition=CLI_INTERNAL_ERROR,
            message_params={"detail": "Core returned no delete payload for successful operation."},
            output_format=output_format,
            verbose=False,
            expected=False,
        )

    command_exit_code = _task_delete_exit_code(batch_result)
    if machine:
        if command_exit_code != 0:
            error = _build_cli_public_error(
                definition=CLI_INTERNAL_ERROR,
                message_params={"detail": "One or more task deletions were not completed."},
                expected=True,
            ).model_copy(update={"safe_details": {"result": batch_result.model_dump(mode="json")}})
            _print_machine_error(error=error)
            raise typer.Exit(code=1)
        _print_machine_success(
            data={"result": batch_result.model_dump(mode="json")},
            trace_id=str(result.event.trace_id) if result.event.trace_id is not None else None,
        )
        raise typer.Exit(code=0)
    _print_task_delete_result(batch_result)
    raise typer.Exit(code=command_exit_code)


@tasks_app.command(
    "watch",
    help="jelica tasks watch [TASK_REF...] — Watch unfinished analytical tasks.",
    rich_help_panel="Inspect and watch",
)
def tasks_watch(
    task_references: list[str] | None = typer.Argument(
        None,
        help="Zero or more analytical task IDs or names.",
    ),
    machine: bool = typer.Option(
        False,
        "--machine",
        help="Write a machine protocol JSONL stream.",
    ),
) -> None:
    output_format = "machine" if machine else "text"
    requested_references = tuple(task_references or ())
    normalized_task_ids = (
        tuple()
        if len(requested_references) == 0
        else _resolve_task_references(
            requested_references,
            allow_missing=not machine,
            output_format=output_format,
        )
    )

    try:
        service = TaskWatchService(core_config_service=_core_config_service())
        if machine:
            outcome = _run_watch_session(
                service=service,
                task_ids=normalized_task_ids,
                mode=TerminalMode.STANDARD,
                render=False,
                event_callback=lambda event: _TERMINAL.raw(serialize_machine_event(event=event)),
                row_callback=_print_machine_task_update,
            )
        else:
            outcome = _run_watch_session(
                service=service,
                task_ids=normalized_task_ids,
                mode=TerminalMode.STANDARD,
                render=True,
            )
    except Exception as error:
        _exit_with_cli_error(
            definition=CLI_INTERNAL_ERROR,
            message_params={"detail": f"Cannot watch analytical tasks: {error}"},
            output_format=output_format,
            verbose=False,
            expected=False,
        )

    if machine:
        for inactive_task in outcome.inactive_tasks:
            _print_machine_task_update(inactive_task)
    succeeded = _watch_outcome_succeeded(outcome)
    if outcome.interrupted:
        if machine:
            interrupted_task_ids = normalized_task_ids or tuple(row.task_id for row in outcome.rows)
            trace_id = (
                outcome.rows[0].trace_id
                if len(outcome.rows) == 1
                else _current_cli_invocation().trace_id
            )
            _exit_machine_interrupted(
                task_ids=interrupted_task_ids,
                trace_id=trace_id,
            )
        raise typer.Exit(code=130)
    raise typer.Exit(code=0 if succeeded else 1)


@events_app.command(
    "watch",
    help="jelica events watch — Watch the persisted JELICA system event stream.",
    rich_help_panel="Watch",
)
def events_watch(
    task_reference: str | None = typer.Option(
        None,
        "--task",
        help="Show only events for one analytical task ID or name.",
    ),
    after: UUID | None = typer.Option(
        None,
        "--after",
        help="Resume strictly after this event ID.",
    ),
    machine: bool = typer.Option(
        False,
        "--machine",
        help="Write a machine protocol JSONL stream.",
    ),
) -> None:
    output_format = "machine" if machine else "text"
    task_id = (
        None
        if task_reference is None
        else _resolve_task_reference(
            task_reference=task_reference,
            output_format=output_format,
        )
    )

    def emit_event(event: Event) -> None:
        if machine:
            _TERMINAL.raw(serialize_machine_event(event=event))
            return
        task_suffix = "" if event.task_id is None else f" task={event.task_id}"
        _TERMINAL.plain(
            f"{event.timestamp.isoformat()} [{event.type.value}] "
            f"{event.name}{task_suffix}: {event.message}"
        )

    def emit_events(events: tuple[Event, ...]) -> None:
        for event in events:
            emit_event(event)

    try:
        service = EventWatchService(
            task_id=task_id,
            core_config_service=_core_config_service(),
        )
        emit_events(service.prepare(after_event_id=after))
        service.watch(emit_events)
    except EventWatchCursorNotFoundError as error:
        _exit_with_cli_error(
            definition=CLI_EVENT_CURSOR_NOT_FOUND,
            message_params={"event_id": str(error.event_id)},
            output_format=output_format,
            verbose=False,
        )
    except KeyboardInterrupt:
        if machine:
            _exit_machine_interrupted(
                task_ids=tuple() if task_id is None else (task_id,),
                trace_id=_current_cli_invocation().trace_id,
            )
        raise typer.Exit(code=130) from None
    except Exception as error:
        _exit_with_cli_error(
            definition=CLI_INTERNAL_ERROR,
            message_params={"detail": f"Cannot watch JELICA events: {error}"},
            output_format=output_format,
            verbose=False,
            expected=False,
        )

    raise typer.Exit(code=0)


def _print_machine_task_update(task: WatchTaskRow | InactiveTask) -> None:
    if isinstance(task, WatchTaskRow):
        job_id = task.job_id
        stage = task.stage
        progress = task.progress
        warning_count = task.warning_count
    else:
        job_id = None
        stage = None
        progress = 0
        warning_count = 0
    _TERMINAL.raw(
        serialize_machine_payload(
            {
                "machine_protocol_version": MACHINE_PROTOCOL_VERSION,
                "type": "task.update",
                "trace_id": None if task.trace_id is None else str(task.trace_id),
                "command_id": _current_cli_invocation().command_id,
                "data": {
                    "task_id": task.task_id,
                    "task_name": task.task_name,
                    "job_id": job_id,
                    "status": task.state,
                    "stage": stage,
                    "progress": progress,
                    "warning_count": warning_count,
                },
            }
        )
    )


@tasks_app.command(
    "update",
    context_settings=_TASK_UPDATE_CONTEXT_SETTINGS,
    help="jelica tasks update TASK_REF [...] — Update an inactive task config.",
    rich_help_panel="Task configuration",
)
def tasks_update(
    ctx: typer.Context,
    task_reference: str = typer.Argument(..., help="Analytical task ID or name."),
    machine: bool = typer.Option(
        False,
        "--machine",
        help="Write one machine protocol JSON response.",
    ),
) -> None:
    output_format = "machine" if machine else "text"
    task_id = _resolve_task_reference(
        task_reference=task_reference,
        output_format=output_format,
    )
    try:
        parsed_arguments = _parse_task_update_arguments(list(ctx.args))
        config_json = _load_config_json(parsed_arguments.config_path)
    except TaskUpdateCliArgumentError as error:
        _exit_with_cli_error(
            definition=CLI_ANALYZE_ARGUMENT_INVALID,
            message_params={"detail": str(error)},
            output_format=output_format,
            verbose=False,
        )
    except ConfigFileReadError as error:
        _exit_with_cli_error(
            definition=CLI_CONFIG_FILE_READ_FAILED,
            message_params={"detail": str(error)},
            output_format=output_format,
            verbose=False,
        )

    result = run_update_analytical_task(
        task_id=task_id,
        config_json=config_json,
        raw_overrides=parsed_arguments.raw_overrides,
        core_config_service=_core_config_service(),
    )
    if not result.ok:
        _exit_with_core_failure(
            result=result,
            output_format=output_format,
            verbose=False,
        )

    update_result = result.value
    if update_result is None:
        _exit_with_cli_error(
            definition=CLI_INTERNAL_ERROR,
            message_params={"detail": "Core returned no update payload for successful operation."},
            output_format=output_format,
            verbose=False,
            expected=False,
        )

    if machine:
        _print_machine_success(
            data={"result": update_result.model_dump(mode="json")},
            trace_id=str(result.event.trace_id) if result.event.trace_id is not None else None,
        )
        return

    _print_event(
        result.event,
        system_log_path=result.system_log_path,
        task_log_path=result.task_log_path,
    )
    _print_task_update_result(update_result)


@tasks_samples_app.command("list", help="jelica tasks samples list TASK_REF — List task sources.")
def tasks_samples_list(
    task_reference: str = typer.Argument(..., help="Analytical task ID or name."),
) -> None:
    task_id = _resolve_task_reference(task_reference=task_reference)
    result = run_list_analytical_task_samples(
        task_id=task_id,
        core_config_service=_core_config_service(),
    )
    if not result.ok:
        _exit_with_core_failure(
            result=result,
            output_format="text",
            verbose=False,
        )

    samples = result.value
    if samples is None:
        _exit_with_cli_error(
            definition=CLI_INTERNAL_ERROR,
            message_params={"detail": "Core returned no samples payload for successful operation."},
            output_format="text",
            verbose=False,
            expected=False,
        )

    _TERMINAL.plain(f"task_id: {task_id}")
    _TERMINAL.plain(f"samples_count: {len(samples)}")
    for index, source in enumerate(samples):
        _TERMINAL.plain(f"{index}: {_format_sample_source_for_display(source)}")


@tasks_samples_app.command(
    "add",
    help="jelica tasks samples add TASK_REF SOURCE... — Add task sources.",
)
def tasks_samples_add(
    task_reference: str = typer.Argument(..., help="Analytical task ID or name."),
    sources: list[str] = typer.Argument(..., help="One or more sources to append."),
) -> None:
    task_id = _resolve_task_reference(task_reference=task_reference)
    result = run_add_analytical_task_samples(
        task_id=task_id,
        sources=tuple(sources),
        core_config_service=_core_config_service(),
    )
    if not result.ok:
        _exit_with_core_failure(
            result=result,
            output_format="text",
            verbose=False,
        )

    update_result = result.value
    if update_result is None:
        _exit_with_cli_error(
            definition=CLI_INTERNAL_ERROR,
            message_params={
                "detail": "Core returned no update payload for successful samples add operation."
            },
            output_format="text",
            verbose=False,
            expected=False,
        )

    _print_event(
        result.event,
        system_log_path=result.system_log_path,
        task_log_path=result.task_log_path,
    )
    _print_task_update_result(update_result)


@tasks_samples_app.command(
    "remove",
    help="jelica tasks samples remove TASK_REF INDEX... — Remove task sources.",
)
def tasks_samples_remove(
    task_reference: str = typer.Argument(..., help="Analytical task ID or name."),
    indices: list[int] = typer.Argument(..., help="One or more sample indices to remove."),
) -> None:
    task_id = _resolve_task_reference(task_reference=task_reference)
    result = run_remove_analytical_task_samples(
        task_id=task_id,
        indices=tuple(indices),
        core_config_service=_core_config_service(),
    )
    if not result.ok:
        _exit_with_core_failure(
            result=result,
            output_format="text",
            verbose=False,
        )

    update_result = result.value
    if update_result is None:
        _exit_with_cli_error(
            definition=CLI_INTERNAL_ERROR,
            message_params={
                "detail": "Core returned no update payload for successful samples remove operation."
            },
            output_format="text",
            verbose=False,
            expected=False,
        )

    _print_event(
        result.event,
        system_log_path=result.system_log_path,
        task_log_path=result.task_log_path,
    )
    _print_task_update_result(update_result)


@tasks_app.command(
    "reprioritize",
    help="jelica tasks reprioritize TASK_REF PRIORITY — Change active job priority.",
    rich_help_panel="Execution control",
)
def tasks_reprioritize(
    task_reference: str = typer.Argument(..., help="Analytical task ID or name."),
    priority: int = typer.Argument(..., help="New priority for the active job."),
    machine: bool = typer.Option(
        False,
        "--machine",
        help="Write one machine protocol JSON response.",
    ),
) -> None:
    output_format = "machine" if machine else "text"
    task_id = _resolve_task_reference(
        task_reference=task_reference,
        output_format=output_format,
    )
    result = run_reprioritize_analytical_task(
        task_id=task_id,
        priority=priority,
        core_config_service=_core_config_service(),
    )
    if not result.ok:
        _exit_with_core_failure(
            result=result,
            output_format=output_format,
            verbose=False,
        )

    reprioritize_result = result.value
    if reprioritize_result is None:
        _exit_with_cli_error(
            definition=CLI_INTERNAL_ERROR,
            message_params={
                "detail": "Core returned no reprioritize payload for successful operation."
            },
            output_format=output_format,
            verbose=False,
            expected=False,
        )

    if machine:
        _print_machine_success(
            data={"result": reprioritize_result.model_dump(mode="json")},
            trace_id=str(result.event.trace_id) if result.event.trace_id is not None else None,
        )
        return

    _print_event(
        result.event,
        system_log_path=result.system_log_path,
        task_log_path=result.task_log_path,
    )
    _print_task_reprioritize_result(reprioritize_result)


def _run_task_control_command(
    *,
    task_references: tuple[str, ...],
    operation: Callable[..., CoreOperationResult[AnalyticalTaskMutationResult]],
    operation_name: str,
    machine: bool = False,
) -> None:
    output_format = "machine" if machine else "text"
    task_ids = _resolve_task_references(task_references, output_format=output_format)
    operation_results: list[CoreOperationResult[AnalyticalTaskMutationResult]] = []
    mutations: list[AnalyticalTaskMutationResult] = []
    for task_id in task_ids:
        result = operation(
            task_id=task_id,
            core_config_service=_core_config_service(),
        )
        if not result.ok:
            _exit_with_core_failure(
                result=result,
                output_format=output_format,
                verbose=False,
            )
        mutation = result.value
        if mutation is None:
            _exit_with_cli_error(
                definition=CLI_INTERNAL_ERROR,
                message_params={
                    "detail": (
                        f"Core returned no {operation_name} payload for successful operation."
                    )
                },
                output_format=output_format,
                verbose=False,
                expected=False,
            )
        operation_results.append(result)
        mutations.append(mutation)

    if machine:
        trace_id = (
            str(operation_results[0].event.trace_id)
            if len(operation_results) == 1 and operation_results[0].event.trace_id is not None
            else None
        )
        _print_machine_success(
            data={
                "operation": operation_name,
                "tasks": [mutation.model_dump(mode="json") for mutation in mutations],
                "count": len(mutations),
            },
            trace_id=trace_id,
        )
        return

    for result, mutation in zip(operation_results, mutations, strict=True):
        _print_event(
            result.event,
            system_log_path=result.system_log_path,
            task_log_path=result.task_log_path,
        )
        _print_task_control_result(mutation)


@tasks_app.command(
    "pause",
    help="jelica tasks pause TASK_REF... — Pause active tasks.",
    rich_help_panel="Execution control",
)
def tasks_pause(
    task_references: list[str] = typer.Argument(
        ...,
        help="One or more analytical task IDs or names.",
    ),
    machine: bool = typer.Option(
        False,
        "--machine",
        help="Write one machine protocol JSON response.",
    ),
) -> None:
    _run_task_control_command(
        task_references=tuple(task_references),
        operation=run_pause_analytical_task,
        operation_name="pause",
        machine=machine,
    )


@tasks_app.command(
    "stop",
    help="jelica tasks stop TASK_REF... — Pause active tasks recoverably.",
    rich_help_panel="Execution control",
)
def tasks_stop(
    task_references: list[str] = typer.Argument(
        ...,
        help="One or more analytical task IDs or names.",
    ),
    machine: bool = typer.Option(
        False,
        "--machine",
        help="Write one machine protocol JSON response.",
    ),
) -> None:
    _run_task_control_command(
        task_references=tuple(task_references),
        operation=run_pause_analytical_task,
        operation_name="stop",
        machine=machine,
    )


@tasks_app.command(
    "resume",
    help="jelica tasks resume TASK_REF... — Resume paused tasks.",
    rich_help_panel="Execution control",
)
def tasks_resume(
    task_references: list[str] = typer.Argument(
        ...,
        help="One or more analytical task IDs or names.",
    ),
    machine: bool = typer.Option(
        False,
        "--machine",
        help="Write one machine protocol JSON response.",
    ),
) -> None:
    if machine or len(task_references) > 1:
        _run_task_execution_command(
            task_references=tuple(task_references),
            operation=run_resume_analytical_task,
            operation_name="resume",
            operation_kwargs={},
            result_printer=_print_task_resume_result,
            machine=machine,
        )
        return

    task_id = _resolve_task_reference(task_reference=task_references[0])
    watch_started_at = datetime.now(UTC)
    _ensure_execution_service(
        output_format="text",
        verbose=False,
        task_id=task_id,
    )
    result = run_resume_analytical_task(
        task_id=task_id,
        detached=True,
        background_runner_module=_CLI_SERVICE_RUNNER_MODULE,
        core_config_service=_core_config_service(),
    )
    if not result.ok:
        _exit_with_core_failure(
            result=result,
            output_format="text",
            verbose=False,
        )
    resume_result = result.value
    if resume_result is None:
        _exit_with_cli_error(
            definition=CLI_INTERNAL_ERROR,
            message_params={"detail": "Core returned no resume payload for successful operation."},
            output_format="text",
            verbose=False,
            expected=False,
        )

    watched = _watch_execution_task(
        task_id=resume_result.task.task_id,
        event_since=watch_started_at,
        mode=TerminalMode.STANDARD,
        render=True,
        output_format="text",
        verbose=False,
    )
    if watched.interrupted:
        raise typer.Exit(code=130)

    final_row = next(
        (row for row in watched.rows if row.job_id == resume_result.job.job_id),
        None,
    )
    if final_row is None or final_row.state != "completed":
        raise typer.Exit(code=1)

    final_snapshot = _load_execution_task_snapshot(
        task_id=resume_result.task.task_id,
        expected_job_id=resume_result.job.job_id,
        output_format="text",
        verbose=False,
    )
    final_job = final_snapshot.active_or_latest_job
    if final_job is None:
        _exit_with_cli_error(
            definition=CLI_INTERNAL_ERROR,
            message_params={"detail": "Completed task has no latest job payload."},
            output_format="text",
            verbose=False,
            expected=False,
        )
    resume_result = resume_result.model_copy(update={"task": final_snapshot.task, "job": final_job})

    _print_event(
        result.event,
        system_log_path=result.system_log_path,
        task_log_path=result.task_log_path,
    )
    _print_task_resume_result(resume_result)
    summary_context = _load_input_processing_summary_context(
        task_id=resume_result.task.task_id,
        job_id=resume_result.job.job_id,
    )
    if summary_context is not None:
        _print_input_processing_terminal_summary(context=summary_context)


@tasks_app.command(
    "cancel",
    help="jelica tasks cancel TASK_REF... — Cancel active tasks.",
    rich_help_panel="Execution control",
)
def tasks_cancel(
    task_references: list[str] = typer.Argument(
        ...,
        help="One or more analytical task IDs or names.",
    ),
    machine: bool = typer.Option(
        False,
        "--machine",
        help="Write one machine protocol JSON response.",
    ),
) -> None:
    _run_task_control_command(
        task_references=tuple(task_references),
        operation=run_cancel_analytical_task,
        operation_name="cancel",
        machine=machine,
    )


@service_app.command(
    "start",
    help="jelica service start — Start the persistent JELICA Service.",
)
def service_start() -> None:
    try:
        result = start_service(
            core_config_service=_core_config_service(),
            runner_module=_CLI_SERVICE_RUNNER_MODULE,
        )
    except (ServiceError, CoreConfigError, OSError) as error:
        _exit_with_service_command_error(error)

    if result.already_running:
        _TERMINAL.info("JELICA Service is already running.")
    else:
        _TERMINAL.success("JELICA Service started.")
    _print_service_status(result.status, detailed=False)
    _print_service_version_warning(result.status)


@service_app.command(
    "stop",
    help="jelica service stop [--force] — Stop the persistent JELICA Service.",
)
def service_stop(
    force: bool = typer.Option(
        False,
        "--force",
        help="Safely interrupt running tasks before stopping the Service.",
    ),
) -> None:
    try:
        result = stop_service(
            force=force,
            core_config_service=_core_config_service(),
        )
    except ServiceRunningTasksError as error:
        _exit_with_service_command_error(error)
    except (ServiceError, CoreConfigError, OSError) as error:
        _exit_with_service_command_error(error)

    if result.already_stopped:
        _TERMINAL.info("JELICA Service is already stopped.")
        return
    _TERMINAL.success("JELICA Service stopped.")
    if len(result.interrupted_task_ids) > 0:
        _TERMINAL.info("Interrupted tasks: " + ", ".join(result.interrupted_task_ids))


@service_app.command(
    "restart",
    help="jelica service restart [--force] — Restart the persistent JELICA Service.",
)
def service_restart(
    force: bool = typer.Option(
        False,
        "--force",
        help="Safely interrupt and resume running tasks during restart.",
    ),
) -> None:
    try:
        result = restart_service(
            force=force,
            core_config_service=_core_config_service(),
            runner_module=_CLI_SERVICE_RUNNER_MODULE,
        )
    except ServiceRunningTasksError as error:
        _exit_with_service_command_error(error)
    except (ServiceError, CoreConfigError, OSError) as error:
        _exit_with_service_command_error(error)

    _TERMINAL.success("JELICA Service restarted.")
    if len(result.interrupted_task_ids) > 0:
        _TERMINAL.info("Interrupted tasks: " + ", ".join(result.interrupted_task_ids))
    if len(result.resumed_task_ids) > 0:
        _TERMINAL.info("Returned to execution: " + ", ".join(result.resumed_task_ids))
    _print_service_status(result.status, detailed=False)
    _print_service_version_warning(result.status)


@service_app.command(
    "status",
    help="jelica service status [--detailed] — Show JELICA Service status.",
)
def service_status(
    detailed: bool = typer.Option(
        False,
        "--detailed",
        help="Show available task, worker, and log details.",
    ),
) -> None:
    try:
        status = get_service_status(core_config_service=_core_config_service())
    except (ServiceError, CoreConfigError, OSError) as error:
        _exit_with_service_command_error(error)

    _print_service_status(status, detailed=detailed)
    _print_service_version_warning(status)


@service_app.command(
    "logs",
    help="jelica service logs [--tail N] — Show persisted Service/runtime logs.",
)
def service_logs(
    tail: int = typer.Option(
        200,
        "--tail",
        min=1,
        help="Number of most recent persisted log lines to show.",
    ),
) -> None:
    try:
        logs = read_service_logs(
            tail=tail,
            core_config_service=_core_config_service(),
        )
    except (ServiceError, CoreConfigError, OSError, ValueError) as error:
        _exit_with_service_command_error(error)

    _TERMINAL.plain(f"Log: {logs.path}")
    if len(logs.lines) == 0:
        _TERMINAL.info("No persisted Service log entries were found.")
        return
    for line in logs.lines:
        _TERMINAL.raw(line)


def main() -> None:
    app()


def _load_config_json(config_path: Path | None) -> str | None:
    if config_path is None:
        return None
    resolved_config_path = config_path.resolve()
    config_json = read_config_file_text(resolved_config_path)
    return config_json


def _build_analysis_plan_for_cli(
    *,
    parsed_arguments: AnalyzeCliArguments,
    config_json: str | None,
    output_format: str,
    verbose: bool,
) -> AnalysisPlan:
    try:
        return plan_analysis_from_inputs(
            config_json=config_json,
            raw_overrides=parsed_arguments.raw_overrides,
            positional_sources=parsed_arguments.sources,
            core_config_service=_core_config_service(),
        )
    except Exception as error:
        _exit_with_cli_error(
            definition=CLI_ANALYZE_ARGUMENT_INVALID,
            message_params={"detail": str(error)},
            output_format=output_format,
            verbose=verbose,
        )


def _print_analysis_plan(plan: AnalysisPlan) -> None:
    _TERMINAL.plain("Analysis plan")
    _TERMINAL.plain(
        "Potential only: input acquisition and biological input validation were not run."
    )
    _TERMINAL.plain(f"Target: {plan.target}")
    _TERMINAL.plain(f"From phase: {plan.from_phase}")
    _TERMINAL.plain(f"Resolved start phase: {plan.resolved_start_phase}")
    _TERMINAL.plain("")
    _TERMINAL.plain("Sources:")
    for source in plan.sources:
        _TERMINAL.plain(f"  - {source if source is not None else '<empty slot>'}")

    _TERMINAL.plain("")
    _TERMINAL.plain("Resolved configuration:")
    _TERMINAL.raw(
        json.dumps(
            plan.resolved_config.model_dump(mode="json"),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )

    _TERMINAL.plain("")
    _TERMINAL.plain("Potential execution:")
    for phase in plan.potential_phases:
        if not phase.selected:
            reason = phase.skipped_reason or "outside selected execution range"
            _TERMINAL.plain(f"  - {phase.name} (skipped: {reason})")
            continue
        if phase.enabled:
            _TERMINAL.plain(f"  ✓ {phase.name}")
            continue
        reason = f": {phase.disabled_reason}" if phase.disabled_reason is not None else ""
        _TERMINAL.plain(f"  - {phase.name} (disabled{reason})")

    if len(plan.warnings) == 0:
        return
    _TERMINAL.plain("")
    _TERMINAL.plain("Configuration warnings:")
    for warning in plan.warnings:
        _TERMINAL.plain(f"  - {warning}")


def _print_execution_selection_diagnostics(config: Any) -> None:
    execution = config.execution
    target = _enum_cli_value(execution.target)
    from_phase = _enum_cli_value(execution.from_phase)
    resolved_start_phase = "input_processing" if from_phase in {"auto", "raw"} else from_phase
    skipped = (
        "none"
        if resolved_start_phase == "input_processing"
        else f"phases before {resolved_start_phase}"
    )
    _TERMINAL.plain("Execution selection:", style="dim")
    _TERMINAL.plain(f"  target: {target}", style="dim")
    _TERMINAL.plain(f"  from_phase: {from_phase}", style="dim")
    _TERMINAL.plain(f"  resolved_start_phase: {resolved_start_phase}", style="dim")
    _TERMINAL.plain(f"  skipped_phases: {skipped}", style="dim")


def _enum_cli_value(value: Any) -> str:
    enum_value = getattr(value, "value", value)
    return str(enum_value)


def _reject_removed_analyze_output_flags(
    raw_arguments: list[str],
    *,
    output_format: str = "text",
) -> None:
    for argument in raw_arguments:
        if (
            argument == "--json"
            or argument.startswith("--json=")
            or argument == "--output"
            or argument.startswith("--output=")
        ):
            _exit_with_cli_error(
                definition=CLI_ANALYZE_ARGUMENT_INVALID,
                message_params={
                    "detail": (
                        "Options '--json' and '--output' no longer select machine output. "
                        "Use '--machine' instead."
                    )
                },
                output_format=output_format,
                verbose=False,
            )


def _parse_analyze_arguments(raw_arguments: list[str]) -> AnalyzeCliArguments:
    positional_arguments: list[str] = []
    raw_overrides: list[str] = []

    for argument in raw_arguments:
        if argument.startswith("--"):
            if "=" not in argument:
                raise AnalyzeCliArgumentError(
                    "dynamic parameters must use the --parameter=value format."
                )
            raw_overrides.append(argument)
            continue
        positional_arguments.append(argument)

    if len(positional_arguments) == 0:
        return AnalyzeCliArguments(
            config_path=None,
            sources=(".",) if not _has_samples_override(raw_overrides) else tuple(),
            raw_overrides=tuple(raw_overrides),
        )

    first_argument = positional_arguments[0]
    if _is_json_path(first_argument):
        config_path = Path(first_argument)
        sources = positional_arguments[1:]
    else:
        config_path = None
        sources = positional_arguments

    for source in sources:
        if _is_json_path(source):
            raise AnalyzeCliArgumentError(
                f"JSON config must be the first positional argument: '{source}'."
            )

    return AnalyzeCliArguments(
        config_path=config_path,
        sources=tuple(sources),
        raw_overrides=tuple(raw_overrides),
    )


def _with_execution_overrides(
    arguments: AnalyzeCliArguments,
    *,
    target: str | None,
    from_phase: str | None,
) -> AnalyzeCliArguments:
    raw_overrides = list(arguments.raw_overrides)
    if target is not None:
        raw_overrides.append(f"--execution.target={target}")
    if from_phase is not None:
        raw_overrides.append(f"--execution.from_phase={from_phase}")
    return AnalyzeCliArguments(
        config_path=arguments.config_path,
        sources=arguments.sources,
        raw_overrides=tuple(raw_overrides),
    )


def _has_samples_override(raw_overrides: list[str]) -> bool:
    for raw_override in raw_overrides:
        parameter = raw_override[2:].split("=", 1)[0]
        if parameter == "samples" or parameter.startswith("samples."):
            return True
    return False


def _validate_cli_inline_sequence_length(
    *,
    sources: tuple[str, ...],
    output_format: str,
    verbose: bool,
) -> None:
    for source in sources:
        classification = classify_input_source(source)
        if (
            classification.kind is InputSourceKind.INLINE_SEQUENCE
            and classification.inline_length is not None
            and classification.inline_length > _CLI_INLINE_SEQUENCE_MAX_LENGTH
        ):
            _exit_with_cli_error(
                definition=CLI_INLINE_SEQUENCE_TOO_LONG,
                message_params={
                    "detail": (
                        "Direct inline sequence input via CLI is limited to 128 nucleotide "
                        "symbols after whitespace removal. This limit exists because shell and "
                        "OS command-line argument length behavior varies by platform; it is not "
                        "a limitation of JELICA Core analytics. For longer sequences, use a "
                        "JSON task config or provide FASTA/GenBank/TXT files."
                    )
                },
                output_format=output_format,
                verbose=verbose,
            )


def _parse_task_update_arguments(raw_arguments: list[str]) -> TaskUpdateCliArguments:
    positional_arguments: list[str] = []
    raw_overrides: list[str] = []

    for argument in raw_arguments:
        if argument.startswith("--"):
            if "=" not in argument:
                raise TaskUpdateCliArgumentError(
                    "dynamic parameters must use the --parameter=value format."
                )
            raw_overrides.append(argument)
            continue
        positional_arguments.append(argument)

    if len(positional_arguments) > 1:
        raise TaskUpdateCliArgumentError(
            "tasks update accepts at most one positional config.json argument."
        )

    config_path: Path | None = None
    if len(positional_arguments) == 1:
        candidate = positional_arguments[0]
        if not _is_json_path(candidate):
            raise TaskUpdateCliArgumentError(
                "tasks update positional config must be a JSON file path ending with '.json'."
            )
        config_path = Path(candidate)

    return TaskUpdateCliArguments(
        config_path=config_path,
        raw_overrides=tuple(raw_overrides),
    )


def _is_json_path(argument: str) -> bool:
    return argument.lower().endswith(".json")


def _print_tasks_list(tasks: list[AnalyticalTaskSnapshot]) -> None:
    _TERMINAL.plain(f"Analytical tasks ({len(tasks)}):")
    for snapshot in tasks:
        payload = snapshot.task.model_dump(mode="json")
        job_payload = (
            snapshot.active_or_latest_job.model_dump(mode="json")
            if snapshot.active_or_latest_job is not None
            else None
        )
        job_state = "-" if job_payload is None else str(job_payload["state"])
        _TERMINAL.plain(
            f"- {payload['task_id']} | name={payload['name']} | state={payload['state']} | "
            f"default_priority={payload['default_priority']} | "
            f"active_job_id={payload['active_job_id']} | "
            f"latest_job_id={payload['latest_job_id']} | "
            f"job_state={job_state} | "
            f"config_revision={payload['current_config_revision']} | "
            f"created_at={payload['created_at']} | updated_at={payload['updated_at']}"
        )


def _task_snapshots_machine_payload(
    task_snapshots: list[AnalyticalTaskSnapshot],
    *,
    output_format: str,
) -> list[dict[str, JSONValue]]:
    try:
        resolved_config = _core_config_service().require_initialized_config()
        registry = AnalyticalTaskRegistryService(database_path=resolved_config.database_path)
        payloads: list[dict[str, JSONValue]] = []
        for snapshot in task_snapshots:
            payload: dict[str, JSONValue] = snapshot.task.model_dump(mode="json")
            trace_id = registry.get_task_trace_id(task_id=snapshot.task.task_id)
            payload["trace_id"] = str(trace_id) if trace_id is not None else None
            payload["active_or_latest_job"] = (
                snapshot.active_or_latest_job.model_dump(mode="json")
                if snapshot.active_or_latest_job is not None
                else None
            )
            payloads.append(payload)
        return payloads
    except Exception as error:
        _exit_with_cli_error(
            definition=CLI_INTERNAL_ERROR,
            message_params={"detail": f"Cannot resolve task trace metadata: {error}"},
            output_format=output_format,
            verbose=False,
            expected=False,
        )


def _single_task_trace_id(task_payloads: list[dict[str, JSONValue]]) -> str | None:
    if len(task_payloads) != 1:
        return None
    trace_id = task_payloads[0].get("trace_id")
    return trace_id if isinstance(trace_id, str) else None


def _print_task_details(task_snapshot: AnalyticalTaskSnapshot) -> None:
    payload = task_snapshot.task.model_dump(mode="json")
    job_payload = (
        task_snapshot.active_or_latest_job.model_dump(mode="json")
        if task_snapshot.active_or_latest_job is not None
        else None
    )
    _TERMINAL.plain(f"task_id: {payload['task_id']}")
    _TERMINAL.plain(f"name: {payload['name']}")
    _TERMINAL.plain(f"state: {payload['state']}")
    _TERMINAL.plain(f"default_priority: {payload['default_priority']}")
    _TERMINAL.plain(f"current_config_revision: {payload['current_config_revision']}")
    _TERMINAL.plain(f"current_config_relative_path: {payload['current_config_relative_path']}")
    _TERMINAL.plain(f"current_config_hash: {payload['current_config_hash']}")
    _TERMINAL.plain(f"active_job_id: {payload['active_job_id']}")
    _TERMINAL.plain(f"latest_job_id: {payload['latest_job_id']}")
    _TERMINAL.plain(f"created_at: {payload['created_at']}")
    _TERMINAL.plain(f"updated_at: {payload['updated_at']}")
    _TERMINAL.plain(f"task_dir_relative_path: {payload['task_dir_relative_path']}")
    _TERMINAL.plain(f"record_version: {payload['record_version']}")
    _TERMINAL.plain("active_or_latest_job:")
    if job_payload is None:
        _TERMINAL.plain("  null")
    else:
        _TERMINAL.json(job_payload)


def _print_task_jobs(*, task_id: str, jobs: list[AnalyticalTaskJobRecord]) -> None:
    _TERMINAL.plain(f"Jobs for task {task_id} ({len(jobs)}):")
    for job in jobs:
        payload = job.model_dump(mode="json")
        _TERMINAL.plain(
            f"- {payload['job_id']} | state={payload['state']} | "
            f"priority={payload['priority']} | progress={payload['progress']} | "
            f"config_revision={payload['config_revision']} | created_at={payload['created_at']}"
        )


def _print_task_start_result(result: TaskStartResult) -> None:
    task_payload = result.task.model_dump(mode="json")
    job_payload = result.job.model_dump(mode="json")
    _TERMINAL.success(
        f"Task {task_payload['task_id']} started as job {job_payload['job_id']} "
        f"({job_payload['state']})."
    )


_INPUT_PROCESSING_RUNTIME_EVENTS: Final[frozenset[str]] = frozenset(
    {
        "INPUT_PROCESSING_STARTED",
        "INPUT_PROCESSING_FILE_PROCESSED",
        "INPUT_PROCESSING_VALIDATION_FAILED",
        "INPUT_PROCESSING_FAILED",
    }
)


def _normalize_input_processing_event_name(event_name: str) -> str | None:
    normalized = event_name.strip().upper()
    if normalized.startswith("CORE_"):
        normalized = normalized.removeprefix("CORE_")
    if normalized in _INPUT_PROCESSING_RUNTIME_EVENTS:
        return normalized
    return None


def _event_context_text(context: dict[str, JSONValue], key: str) -> str | None:
    value = context.get(key)
    if isinstance(value, str):
        text = value.strip()
        return text if text != "" else None
    return None


def _event_context_int(context: dict[str, JSONValue], key: str) -> int | None:
    value = context.get(key)
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return None


def _render_input_processing_runtime_event(
    *,
    event_name: str,
    context: dict[str, JSONValue],
) -> None:
    if event_name == "INPUT_PROCESSING_STARTED":
        file_count = _event_context_int(context, "input_file_count")
        if file_count is None:
            return
        _TERMINAL.info(f"Input processing started: {file_count} files.")
        return

    if event_name == "INPUT_PROCESSING_FILE_PROCESSED":
        file_index = _event_context_int(context, "file_index")
        total_file_count = _event_context_int(context, "total_file_count")
        processing_status = _event_context_text(context, "processing_status")
        primary_issue_code = _event_context_text(context, "primary_issue_code")
        primary_issue_message = _event_context_text(context, "primary_issue_message")
        if file_index is None or total_file_count is None:
            return
        if processing_status == "failed":
            failed_issue_code = primary_issue_code or "unknown_issue"
            if failed_issue_code in {
                "input_file_not_found",
                "input_file_unreadable",
                "input_format_unsupported",
                "fasta_malformed",
                "genbank_malformed",
            }:
                failed_issue_code = "malformed_input_file"
            detail = "" if primary_issue_message is None else f" {primary_issue_message}"
            _TERMINAL.error(
                f"Input file {file_index}/{total_file_count} failed ({failed_issue_code}).{detail}"
            )
        return

    if event_name == "INPUT_PROCESSING_VALIDATION_FAILED":
        issue_codes_value = context.get("dataset_issue_codes")
        issue_codes: list[str] = []
        if isinstance(issue_codes_value, list):
            issue_codes = [str(item) for item in issue_codes_value if isinstance(item, str)]
        if len(issue_codes) > 0:
            _TERMINAL.error(f"Dataset validation failed: {', '.join(issue_codes)}")
        else:
            _TERMINAL.error("Dataset validation failed.")
        return

    if event_name == "INPUT_PROCESSING_FAILED":
        failure_detail = _event_context_text(context, "detail")
        if failure_detail is not None:
            _TERMINAL.error(f"Input processing failed: {failure_detail}")
        return


def _build_runtime_input_processing_callback() -> Callable[
    [str, dict[str, JSONValue] | None], None
]:
    def _callback(event_name: str, context: dict[str, JSONValue] | None) -> None:
        normalized_event_name = _normalize_input_processing_event_name(event_name)
        if normalized_event_name is None or context is None:
            return
        _render_input_processing_runtime_event(
            event_name=normalized_event_name,
            context=context,
        )

    return _callback


def _ensure_execution_service(
    *,
    output_format: str,
    verbose: bool,
    task_id: str,
) -> ServiceStatus:
    try:
        result = start_service(
            core_config_service=_core_config_service(),
            runner_module=_CLI_SERVICE_RUNNER_MODULE,
        )
    except (ServiceError, CoreConfigError, OSError) as error:
        _exit_with_execution_service_error(
            detail=str(error),
            output_format=output_format,
            verbose=verbose,
            task_id=task_id,
        )

    status = result.status
    if status.version_compatible is False:
        _exit_with_execution_service_error(
            detail=_service_version_conflict_detail(status),
            output_format=output_format,
            verbose=verbose,
            task_id=task_id,
        )
    return status


def _exit_with_execution_service_error(
    *,
    detail: str,
    output_format: str,
    verbose: bool,
    task_id: str,
) -> NoReturn:
    if output_format == "machine":
        _exit_with_cli_error(
            definition=CLI_INTERNAL_ERROR,
            message_params={"detail": detail},
            output_format=output_format,
            verbose=verbose,
            expected=True,
        )
    if output_format != "text":
        raise ValueError(f"Unsupported CLI output format: {output_format}")
    _ = verbose, task_id
    _TERMINAL.error(detail)
    raise typer.Exit(code=1)


def _watch_execution_task(
    *,
    task_id: str,
    event_since: datetime,
    mode: TerminalMode,
    render: bool,
    output_format: str,
    verbose: bool,
) -> WatchCliOutcome:
    return _watch_execution_tasks(
        task_ids=(task_id,),
        event_since=event_since,
        mode=mode,
        render=render,
        output_format=output_format,
        verbose=verbose,
    )


def _watch_execution_tasks(
    *,
    task_ids: tuple[str, ...],
    event_since: datetime,
    mode: TerminalMode,
    render: bool,
    output_format: str,
    verbose: bool,
) -> WatchCliOutcome:
    try:
        service = TaskWatchService(
            event_since=event_since,
            core_config_service=_core_config_service(),
        )
        return _run_watch_session(
            service=service,
            task_ids=task_ids,
            mode=mode,
            render=render,
            include_explicit_inactive=True,
            wait_for_initial_rows=True,
        )
    except Exception as error:
        task_reference_text = ", ".join(task_ids)
        _exit_with_cli_error(
            definition=CLI_INTERNAL_ERROR,
            message_params={"detail": f"Cannot watch task(s) {task_reference_text}: {error}"},
            output_format=output_format,
            verbose=verbose,
            expected=False,
        )


def _load_execution_task_snapshot(
    *,
    task_id: str,
    expected_job_id: str,
    output_format: str,
    verbose: bool,
) -> AnalyticalTaskSnapshot:
    result = run_get_analytical_task(
        task_id=task_id,
        core_config_service=_core_config_service(),
    )
    if not result.ok:
        _exit_with_core_failure(
            result=result,
            output_format=output_format,
            verbose=verbose,
        )
    snapshot = result.value
    if snapshot is None:
        _exit_with_cli_error(
            definition=CLI_INTERNAL_ERROR,
            message_params={"detail": "Core returned no task snapshot after execution."},
            output_format=output_format,
            verbose=verbose,
            expected=False,
        )
    job = snapshot.active_or_latest_job
    if job is None or job.job_id != expected_job_id:
        _exit_with_cli_error(
            definition=CLI_INTERNAL_ERROR,
            message_params={
                "detail": (f"Task {task_id} no longer refers to expected job {expected_job_id}.")
            },
            output_format=output_format,
            verbose=verbose,
            expected=False,
        )
    return snapshot


def _run_watch_session(
    *,
    service: TaskWatchService,
    task_ids: tuple[str, ...],
    mode: TerminalMode,
    render: bool,
    include_explicit_inactive: bool = False,
    stop_condition: Callable[[], bool] | None = None,
    wait_for_initial_rows: bool = False,
    event_callback: Callable[[Event], None] | None = None,
    row_callback: Callable[[WatchTaskRow], None] | None = None,
) -> WatchCliOutcome:
    _TERMINAL.reset_event_context()
    try:
        preparation = service.prepare(
            task_ids,
            include_explicit_inactive=include_explicit_inactive,
        )
    except KeyboardInterrupt:
        outcome = WatchCliOutcome(
            rows=tuple(),
            missing_task_ids=tuple(),
            inactive_tasks=tuple(),
            events=tuple(),
            interrupted=True,
        )
        if render:
            _TERMINAL.watching_stopped(
                tuple(),
                explicit=len(task_ids) > 0,
                requested_task_ids=task_ids,
            )
        return outcome

    inactive_tasks = preparation.inactive_tasks
    if wait_for_initial_rows and preparation.explicit and len(preparation.rows) == 0:
        inactive_tasks = tuple(task for task in inactive_tasks if task.state != "waiting")

    if render:
        for task_id in preparation.missing_task_ids:
            _TERMINAL.watch_missing(task_id)
        for inactive in inactive_tasks:
            _TERMINAL.watch_inactive(
                inactive.task_id,
                inactive.state,
                inactive.task_name,
            )

    latest_rows = preparation.rows
    observed_events = list(preparation.events)
    emitted_rows: dict[str, WatchTaskRow] = {}
    if row_callback is not None:
        for row in latest_rows:
            row_callback(row)
            emitted_rows[row.task_id] = row
    if event_callback is not None:
        for event in preparation.events:
            event_callback(event)
    interrupted = False
    waiting_for_new_tasks = not preparation.explicit
    waiting_for_initial_rows = (
        wait_for_initial_rows and preparation.explicit and len(latest_rows) == 0
    )
    if waiting_for_initial_rows:
        waiting_for_new_tasks = True
    live_enabled = (
        render
        and mode is not TerminalMode.QUIET
        and (len(latest_rows) > 0 or waiting_for_new_tasks)
    )

    with _TERMINAL.watch_live(
        latest_rows,
        wait_for_new_tasks=waiting_for_new_tasks,
        enabled=live_enabled,
    ) as live:
        if render:
            for event in preparation.events:
                _TERMINAL.event(event, mode=mode)

        def _update(update: WatchUpdate) -> None:
            nonlocal latest_rows, waiting_for_new_tasks, waiting_for_initial_rows
            latest_rows = update.rows
            if waiting_for_initial_rows and len(latest_rows) > 0:
                waiting_for_initial_rows = False
                waiting_for_new_tasks = False
            observed_events.extend(update.events)
            if event_callback is not None:
                for event in update.events:
                    event_callback(event)
            if row_callback is not None:
                for row in latest_rows:
                    if emitted_rows.get(row.task_id) == row:
                        continue
                    row_callback(row)
                    emitted_rows[row.task_id] = row
            if render:
                for event in update.events:
                    _TERMINAL.event(event, mode=mode)
                _TERMINAL.update_watch(
                    live,
                    latest_rows,
                    wait_for_new_tasks=waiting_for_new_tasks,
                )

        try:
            if not service.complete or waiting_for_initial_rows:
                final_update = service.watch(
                    _update,
                    stop_condition=stop_condition,
                    wait_for_observed_rows=wait_for_initial_rows and preparation.explicit,
                )
                latest_rows = final_update.rows
                if waiting_for_initial_rows and len(latest_rows) > 0:
                    waiting_for_initial_rows = False
                    waiting_for_new_tasks = False
        except KeyboardInterrupt:
            interrupted = True
            if render:
                _TERMINAL.update_watch(
                    live,
                    latest_rows,
                    wait_for_new_tasks=False,
                )

    if render:
        _TERMINAL.finish_watch(
            latest_rows,
            wait_for_new_tasks=waiting_for_new_tasks,
            live_was_enabled=live_enabled,
        )
        if interrupted:
            _TERMINAL.watching_stopped(
                latest_rows,
                explicit=preparation.explicit,
                requested_task_ids=task_ids,
            )

    return WatchCliOutcome(
        rows=latest_rows,
        missing_task_ids=preparation.missing_task_ids,
        inactive_tasks=inactive_tasks,
        events=tuple(observed_events),
        interrupted=interrupted,
    )


def _watch_outcome_succeeded(outcome: WatchCliOutcome) -> bool:
    if outcome.interrupted or len(outcome.missing_task_ids) > 0:
        return False
    return all(row.state == "completed" for row in outcome.rows) and all(
        task.state == "completed" for task in outcome.inactive_tasks
    )


def _load_input_processing_summary_context(
    *, task_id: str, job_id: str
) -> dict[str, JSONValue] | None:
    try:
        resolved = _core_config_service().load_resolved_config()
    except Exception:
        return None
    manifest_path = (
        resolved.tasks_dir
        / task_id
        / "jobs"
        / job_id
        / "stages"
        / "input_processing"
        / "input_processing"
        / "input_processing_manifest.json"
    )
    if not manifest_path.is_file():
        return None
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    dataset_summary = payload.get("dataset_summary")
    if not isinstance(dataset_summary, dict):
        return None
    valid_sample_count = dataset_summary.get("valid_sample_count")
    invalid_sample_count = dataset_summary.get("invalid_sample_count")
    unique_sequence_count = dataset_summary.get("unique_sequence_count")
    duplicate_logical_sample_count = dataset_summary.get("duplicate_logical_sample_count")
    comparative_available = dataset_summary.get("comparative_analysis_available")
    parsed_record_count = dataset_summary.get("discovered_record_count")
    if not isinstance(valid_sample_count, int):
        return None
    if not isinstance(invalid_sample_count, int):
        return None
    if not isinstance(unique_sequence_count, int):
        return None
    if not isinstance(duplicate_logical_sample_count, int):
        return None
    if not isinstance(comparative_available, bool):
        return None
    if not isinstance(parsed_record_count, int):
        return None
    processed_input_file_count: int | None = None
    failed_input_file_count: int | None = None
    failed_input_files: list[JSONValue] = []
    processed_files = payload.get("processed_files")
    if isinstance(processed_files, list):
        processed_count = 0
        failed_count = 0
        failed_files: list[JSONValue] = []
        for raw_file in processed_files:
            if not isinstance(raw_file, dict):
                continue
            status = raw_file.get("status")
            if status == "processed":
                processed_count += 1
                continue
            if status != "failed":
                continue
            failed_count += 1
            relative_path = raw_file.get("relative_path")
            normalized_relative_path = "<unknown>"
            if isinstance(relative_path, str):
                stripped_path = relative_path.strip()
                if stripped_path != "":
                    normalized_relative_path = stripped_path
            issue_code = "unknown_issue"
            validation_issues = raw_file.get("validation_issues")
            if isinstance(validation_issues, list):
                for raw_issue in validation_issues:
                    if not isinstance(raw_issue, dict):
                        continue
                    code = raw_issue.get("code")
                    if isinstance(code, str) and code.strip() != "":
                        issue_code = code.strip()
                        break
            failed_files.append(
                {
                    "relative_path": normalized_relative_path,
                    "code": issue_code,
                }
            )
        processed_input_file_count = processed_count
        failed_input_file_count = failed_count
        failed_input_files = failed_files
    issue_codes: list[JSONValue] = []
    dataset_issues = payload.get("dataset_issues")
    if isinstance(dataset_issues, list):
        for issue in dataset_issues:
            if not isinstance(issue, dict):
                continue
            code = issue.get("code")
            if isinstance(code, str):
                issue_codes.append(code)
    summary_context: dict[str, JSONValue] = {
        "parsed_record_count": parsed_record_count,
        "valid_sample_count": valid_sample_count,
        "invalid_sample_count": invalid_sample_count,
        "unique_sequence_count": unique_sequence_count,
        "duplicate_logical_sample_count": duplicate_logical_sample_count,
        "comparative_analysis_available": comparative_available,
        "manifest_path": "input_processing/input_processing_manifest.json",
        "dataset_issue_codes": issue_codes,
    }
    if processed_input_file_count is not None and failed_input_file_count is not None:
        summary_context["processed_input_file_count"] = processed_input_file_count
        summary_context["failed_input_file_count"] = failed_input_file_count
        summary_context["failed_input_files"] = failed_input_files
    return summary_context


def _print_input_processing_terminal_summary(*, context: dict[str, JSONValue]) -> None:
    valid_sample_count = _event_context_int(context, "valid_sample_count")
    invalid_sample_count = _event_context_int(context, "invalid_sample_count")
    failed_input_file_count = _event_context_int(context, "failed_input_file_count")
    dataset_issue_codes_value = context.get("dataset_issue_codes")
    dataset_issue_codes: list[str] = []
    if isinstance(dataset_issue_codes_value, list):
        dataset_issue_codes = [
            item
            for item in dataset_issue_codes_value
            if isinstance(item, str) and item.strip() != ""
        ]
    if valid_sample_count is None:
        return

    if len(dataset_issue_codes) > 0:
        return

    invalid_text = "?" if invalid_sample_count is None else str(invalid_sample_count)
    _TERMINAL.success(
        f"Input processing completed: {valid_sample_count} valid, {invalid_text} invalid."
    )
    if failed_input_file_count is not None and failed_input_file_count > 0:
        _TERMINAL.warning(f"{failed_input_file_count} input files failed.")


def _task_delete_exit_code(result: TaskDeleteBatchResult) -> int:
    for item in result.items:
        if item.result in (
            TaskDeleteItemResultType.NOT_FOUND,
            TaskDeleteItemResultType.REJECTED,
        ):
            return 1
    return 0


def _print_task_delete_result(result: TaskDeleteBatchResult) -> None:
    deleted_count = sum(
        1 for item in result.items if item.result is TaskDeleteItemResultType.DELETED
    )
    deletion_requested_count = sum(
        1 for item in result.items if item.result is TaskDeleteItemResultType.DELETION_REQUESTED
    )
    already_satisfied_count = sum(
        1 for item in result.items if item.result is TaskDeleteItemResultType.ALREADY_SATISFIED
    )
    not_found_count = sum(
        1 for item in result.items if item.result is TaskDeleteItemResultType.NOT_FOUND
    )
    rejected_count = sum(
        1 for item in result.items if item.result is TaskDeleteItemResultType.REJECTED
    )

    for item in result.items:
        message = f"{item.task_id}: {item.result.value}"
        if item.result in {
            TaskDeleteItemResultType.NOT_FOUND,
            TaskDeleteItemResultType.REJECTED,
        }:
            _TERMINAL.error(message)
        else:
            _TERMINAL.success(message)
    _TERMINAL.info(
        "Task deletion: "
        f"{deleted_count} deleted, {deletion_requested_count} requested, "
        f"{already_satisfied_count} unchanged, {not_found_count} not found, "
        f"{rejected_count} rejected."
    )


def _print_task_control_result(result: AnalyticalTaskMutationResult) -> None:
    if result.task is None or result.job is None:
        return
    task_payload = result.task.model_dump(mode="json")
    job_payload = result.job.model_dump(mode="json")
    _TERMINAL.success(
        f"Task {task_payload['task_id']}: {result.result_type.value}; "
        f"job {job_payload['job_id']} is {job_payload['state']}."
    )


def _print_task_update_result(result: TaskUpdateResult) -> None:
    payload = result.model_dump(mode="json")
    _TERMINAL.success(
        f"Task {payload['task_id']} updated: config revision "
        f"{payload['current_config_revision']}, priority {payload['default_priority']}."
    )


def _format_sample_source_for_display(source: str | None) -> str:
    if source is None:
        return "null"
    classification = classify_input_source(source)
    if (
        classification.kind is InputSourceKind.INLINE_SEQUENCE
        and classification.inline_sequence is not None
        and classification.inline_length is not None
    ):
        source_hash = hashlib.sha256(classification.inline_sequence.encode("utf-8")).hexdigest()
        return (
            f"inline_sequence(length={classification.inline_length}, sha256={source_hash[:12]}...)"
        )
    return source


def _print_task_reprioritize_result(result: TaskReprioritizeResult) -> None:
    payload = result.model_dump(mode="json")
    _TERMINAL.success(
        f"Task {payload['task_id']} priority changed from "
        f"{payload['old_priority']} to {payload['new_priority']}."
    )


def _print_task_resume_result(result: TaskResumeResult) -> None:
    task_payload = result.task.model_dump(mode="json")
    job_payload = result.job.model_dump(mode="json")
    _TERMINAL.success(
        f"Task {task_payload['task_id']} resumed as job {job_payload['job_id']} "
        f"({job_payload['state']})."
    )


def _exit_with_service_command_error(error: BaseException) -> NoReturn:
    _TERMINAL.error(str(error))
    raise typer.Exit(code=1) from error


def _print_service_status(status: ServiceStatus, *, detailed: bool) -> None:
    _TERMINAL.plain(f"status: {'running' if status.running else 'stopped'}")
    _TERMINAL.plain(f"service_id: {status.service_id or '-'}")
    _TERMINAL.plain(f"pid: {status.pid if status.pid is not None else '-'}")
    _TERMINAL.plain(f"jelica_version: {status.jelica_version or '-'}")
    _TERMINAL.plain(
        f"started_at: {status.started_at.isoformat() if status.started_at is not None else '-'}"
    )
    _TERMINAL.plain(
        "last_heartbeat: "
        + (status.last_heartbeat.isoformat() if status.last_heartbeat is not None else "-")
    )
    _TERMINAL.plain(f"state: {status.state.value}")
    _TERMINAL.plain(f"configured_workers: {status.configured_workers}")
    _TERMINAL.plain(f"active_workers: {status.active_workers}")
    _TERMINAL.plain(f"queued_tasks: {status.queued_tasks}")
    _TERMINAL.plain(f"running_tasks: {status.running_tasks}")
    if not detailed:
        return
    _TERMINAL.plain("queued_task_ids: " + (", ".join(status.queued_task_ids) or "-"))
    _TERMINAL.plain("running_task_ids: " + (", ".join(status.running_task_ids) or "-"))
    _TERMINAL.plain("active_worker_task_ids: " + (", ".join(status.active_worker_task_ids) or "-"))
    _TERMINAL.plain(f"log_path: {status.log_path}")


def _service_version_conflict_detail(status: ServiceStatus) -> str:
    service_version = status.jelica_version or "unknown"
    detail = (
        f"Running Service version {service_version} differs from CLI/Core version "
        f"{status.cli_jelica_version}."
    )
    if status.running_tasks > 0:
        return (
            f"{detail} {status.running_tasks} task(s) are running; the Service was not "
            "interrupted. Use 'jelica service restart --force' only as an explicit action."
        )
    return f"{detail} Restart is recommended: jelica service restart."


def _print_service_version_warning(status: ServiceStatus) -> None:
    if not status.running or status.version_compatible is not False:
        return
    _TERMINAL.warning(_service_version_conflict_detail(status))


@config_app.command(
    "init",
    help="jelica config init — Initialize system configuration.",
    rich_help_panel="Setup and validation",
)
def config_init(
    data_dir: str | None = typer.Option(
        None,
        "--data-dir",
        help="Value for data.directory in system config.",
    ),
    max_parallel_tasks: int | None = typer.Option(
        None,
        "--max-parallel-tasks",
        "--max-workers",
        help="Value for execution.max_parallel_tasks in system config.",
    ),
    log_level: str | None = typer.Option(
        None,
        "--log-level",
        help="Value for logging.level in system config.",
    ),
    non_interactive: bool = typer.Option(
        False,
        "--non-interactive",
        help="Use defaults for missing options without prompts.",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="Backward-compatible flag. Existing config.toml is reused and never overwritten.",
    ),
    verbose: bool = typer.Option(
        False,
        "--verbose",
        help="Show safe diagnostic fields when available.",
    ),
    machine: bool = typer.Option(
        False,
        "--machine",
        help="Write one machine protocol JSON response without prompting.",
    ),
) -> None:
    output_format = "machine" if machine else "text"
    requested_data_dir = data_dir
    requested_max_parallel_tasks = max_parallel_tasks
    requested_log_level = log_level

    if not non_interactive and not machine:
        if requested_data_dir is None:
            requested_data_dir = typer.prompt("data.directory", default=DEFAULT_DATA_DIRECTORY)
        if requested_max_parallel_tasks is None:
            requested_max_parallel_tasks = typer.prompt(
                "execution.max_parallel_tasks",
                default=DEFAULT_MAX_PARALLEL_TASKS,
                type=int,
            )
        if requested_log_level is None:
            requested_log_level = typer.prompt("logging.level", default=DEFAULT_LOG_LEVEL)

    result = run_config_init(
        data_directory=requested_data_dir,
        max_parallel_tasks=requested_max_parallel_tasks,
        log_level=requested_log_level,
        force=force,
        core_config_service=_core_config_service(),
    )
    if not result.ok:
        _exit_with_core_failure(
            result=result,
            output_format=output_format,
            verbose=verbose,
            text_prefix=_CONFIG_COMMAND_ERROR_PREFIX,
        )

    resolved_config = result.value
    if resolved_config is None:
        _exit_with_cli_error(
            definition=CLI_INTERNAL_ERROR,
            message_params={"detail": "Core returned no config payload for successful operation."},
            output_format=output_format,
            verbose=verbose,
            expected=False,
        )

    config_path_value = str(_core_config_service().get_config_path())
    if machine:
        _print_machine_success(
            data={
                "config_path": config_path_value,
                "config": _resolved_application_config_payload(resolved_config),
            }
        )
        return
    _TERMINAL.success("System config initialized successfully.")
    _TERMINAL.plain(f"config_path: {config_path_value}")
    if verbose:
        _print_resolved_core_config(resolved_config)


@config_app.command(
    "path",
    help="jelica config path — Show the system config path.",
    rich_help_panel="Inspect",
)
def config_path(
    verbose: bool = typer.Option(
        False,
        "--verbose",
        help="Show safe diagnostic fields when available.",
    ),
    machine: bool = typer.Option(
        False,
        "--machine",
        help="Write one machine protocol JSON response.",
    ),
) -> None:
    output_format = "machine" if machine else "text"
    result = run_config_path(core_config_service=_core_config_service())
    if not result.ok:
        _exit_with_core_failure(
            result=result,
            output_format=output_format,
            verbose=verbose,
            text_prefix=_CONFIG_COMMAND_ERROR_PREFIX,
        )

    config_path_value = result.value
    if config_path_value is None:
        _exit_with_cli_error(
            definition=CLI_INTERNAL_ERROR,
            message_params={"detail": "Core returned no path payload for successful operation."},
            output_format=output_format,
            verbose=verbose,
            expected=False,
        )

    if machine:
        _print_machine_success(data={"config_path": str(config_path_value)})
        return
    _TERMINAL.plain(config_path_value)


@config_app.command(
    "show",
    help="jelica config show — Show the persisted system configuration.",
    rich_help_panel="Inspect",
)
def config_show(
    verbose: bool = typer.Option(
        False,
        "--verbose",
        help="Show safe diagnostic fields when available.",
    ),
    machine: bool = typer.Option(
        False,
        "--machine",
        help="Write one machine protocol JSON response.",
    ),
) -> None:
    output_format = "machine" if machine else "text"
    result = run_config_show(core_config_service=_core_config_service())
    if not result.ok:
        _exit_with_core_failure(
            result=result,
            output_format=output_format,
            verbose=verbose,
            text_prefix=_CONFIG_COMMAND_ERROR_PREFIX,
        )

    resolved_config = result.value
    if resolved_config is None:
        _exit_with_cli_error(
            definition=CLI_INTERNAL_ERROR,
            message_params={"detail": "Core returned no config payload for successful operation."},
            output_format=output_format,
            verbose=verbose,
            expected=False,
        )

    config_document = _cli_system_config_service().show_document()
    if machine:
        _print_machine_success(data={"config": config_document})
        return
    _TERMINAL.json(config_document)


@config_app.command(
    "validate",
    help="jelica config validate — Validate system configuration.",
    rich_help_panel="Setup and validation",
)
def config_validate(
    verbose: bool = typer.Option(
        False,
        "--verbose",
        help="Show safe diagnostic fields when available.",
    ),
    machine: bool = typer.Option(
        False,
        "--machine",
        help="Write one machine protocol JSON response.",
    ),
) -> None:
    output_format = "machine" if machine else "text"
    result = run_config_validate(core_config_service=_core_config_service())
    if not result.ok:
        _exit_with_core_failure(
            result=result,
            output_format=output_format,
            verbose=verbose,
            text_prefix=_CONFIG_VALIDATE_ERROR_PREFIX,
        )

    resolved_config = result.value
    if resolved_config is None:
        _exit_with_cli_error(
            definition=CLI_INTERNAL_ERROR,
            message_params={"detail": "Core returned no config payload for successful operation."},
            output_format=output_format,
            verbose=verbose,
            expected=False,
        )

    if machine:
        _print_machine_success(
            data={
                "valid": True,
                "config": _resolved_application_config_payload(resolved_config),
            }
        )
        return
    _TERMINAL.success("System config is valid.")


@config_app.command(
    "set",
    help="jelica config set KEY=VALUE — Set one config value.",
    rich_help_panel="Edit",
    context_settings={"allow_extra_args": True},
)
def config_set(
    ctx: typer.Context,
    parameter: str = typer.Argument(
        ...,
        metavar="KEY=VALUE",
        help="KEY=VALUE assignment (e.g. execution.max_parallel_tasks=8).",
    ),
    verbose: bool = typer.Option(
        False,
        "--verbose",
        help="Show safe diagnostic fields when available.",
    ),
    machine: bool = typer.Option(
        False,
        "--machine",
        help="Write one machine protocol JSON response.",
    ),
) -> None:
    output_format = "machine" if machine else "text"
    if len(ctx.args) > 1:
        raise typer.BadParameter("expected one optional legacy value", param_hint="value")
    value = ctx.args[0] if ctx.args else None
    if value is None:
        if "=" not in parameter:
            raise typer.BadParameter("expected KEY=VALUE assignment", param_hint="KEY=VALUE")
        parameter, value = parameter.split("=", 1)
    elif "=" in parameter:
        raise typer.BadParameter(
            "use KEY=VALUE as one argument (or omit '=' for legacy two-argument form)",
            param_hint="KEY=VALUE",
        )
    if value.strip() == "":
        _exit_with_cli_error(
            definition=CLI_ANALYZE_ARGUMENT_INVALID,
            message_params={
                "detail": (
                    f"{_CONFIG_COMMAND_ERROR_PREFIX} empty values are not allowed for 'config set'."
                )
            },
            output_format=output_format,
            verbose=verbose,
        )

    result = run_config_set(
        parameter=parameter,
        value=value,
        core_config_service=_core_config_service(),
    )
    if not result.ok:
        _exit_with_core_failure(
            result=result,
            output_format=output_format,
            verbose=verbose,
            text_prefix=_CONFIG_COMMAND_ERROR_PREFIX,
        )

    resolved_config = result.value
    if resolved_config is None:
        _exit_with_cli_error(
            definition=CLI_INTERNAL_ERROR,
            message_params={"detail": "Core returned no config payload for successful operation."},
            output_format=output_format,
            verbose=verbose,
            expected=False,
        )

    if machine:
        _print_machine_success(
            data={
                "parameter": parameter,
                "config": _resolved_application_config_payload(resolved_config),
            }
        )
        return
    _TERMINAL.success(f"Parameter '{parameter}' updated.")
    if verbose:
        _print_resolved_core_config(resolved_config)


@config_app.command(
    "unset",
    help="jelica config unset PARAMETER — Restore one default config value.",
    rich_help_panel="Edit",
)
def config_unset(
    parameter: str = typer.Argument(
        ...,
        help="Parameter path or alias (e.g. execution.max_parallel_tasks or max_parallel_tasks).",
    ),
    verbose: bool = typer.Option(
        False,
        "--verbose",
        help="Show safe diagnostic fields when available.",
    ),
    machine: bool = typer.Option(
        False,
        "--machine",
        help="Write one machine protocol JSON response.",
    ),
) -> None:
    output_format = "machine" if machine else "text"
    result = run_config_unset(
        parameter=parameter,
        core_config_service=_core_config_service(),
    )
    if not result.ok:
        _exit_with_core_failure(
            result=result,
            output_format=output_format,
            verbose=verbose,
            text_prefix=_CONFIG_COMMAND_ERROR_PREFIX,
        )

    resolved_config = result.value
    if resolved_config is None:
        _exit_with_cli_error(
            definition=CLI_INTERNAL_ERROR,
            message_params={"detail": "Core returned no config payload for successful operation."},
            output_format=output_format,
            verbose=verbose,
            expected=False,
        )

    if machine:
        _print_machine_success(
            data={
                "parameter": parameter,
                "config": _resolved_application_config_payload(resolved_config),
            }
        )
        return
    _TERMINAL.success(f"Parameter '{parameter}' was reset to its explicit default value.")
    if verbose:
        _print_resolved_core_config(resolved_config)


def _print_resolved_core_config(resolved_config: ResolvedCoreConfig) -> None:
    payload = _resolved_application_config_payload(resolved_config)
    _TERMINAL.json(payload)


def _resolved_application_config_payload(
    resolved_config: ResolvedCoreConfig,
) -> dict[str, JSONValue]:
    payload = _resolved_core_config_payload(resolved_config)
    payload["cli"] = _cli_system_config_service().load().cli.model_dump(mode="json")
    return payload


def _resolved_core_config_payload(resolved_config: ResolvedCoreConfig) -> dict[str, JSONValue]:
    payload = resolved_config.model_dump(mode="json")
    ncbi_api_key = resolved_config.ncbi_api_key.strip()
    payload["ncbi_api_key"] = "<configured>" if ncbi_api_key != "" else "<not configured>"
    return payload


def _print_machine_success(
    *,
    data: dict[str, Any],
    trace_id: str | None = None,
) -> None:
    global _MACHINE_RESPONSE_EMITTED
    invocation = _current_cli_invocation().with_trace_id(trace_id)
    _TERMINAL.raw(
        serialize_machine_payload(machine_success_payload(invocation=invocation, data=data))
    )
    _MACHINE_RESPONSE_EMITTED = True


def _print_machine_error(*, error: PublicError) -> None:
    global _MACHINE_RESPONSE_EMITTED
    current_invocation = _current_cli_invocation()
    trace_id = (
        error.event.trace_id if error.event.trace_id is not None else current_invocation.trace_id
    )
    invocation = current_invocation.with_trace_id(trace_id)
    _TERMINAL.raw(
        serialize_machine_payload(
            machine_error_payload(
                invocation=invocation,
                error=error,
            )
        )
    )
    _MACHINE_RESPONSE_EMITTED = True


def _exit_machine_interrupted(
    *,
    task_ids: tuple[str, ...],
    trace_id: str | UUID | None,
) -> NoReturn:
    _set_cli_trace_id(trace_id)
    error = _build_cli_public_error(
        definition=CLI_COMMAND_INTERRUPTED,
        message_params={},
        expected=True,
    ).model_copy(update={"safe_details": {"task_ids": list(task_ids)}})
    _print_machine_error(error=error)
    raise typer.Exit(code=130)


def _exit_with_core_failure(
    *,
    result: CoreOperationResult[Any],
    output_format: str,
    verbose: bool,
    text_prefix: str | None = None,
) -> NoReturn:
    error = result.error
    if error is None:
        _exit_with_cli_error(
            definition=CLI_INTERNAL_ERROR,
            message_params={"detail": "Core operation failed without structured error payload."},
            output_format=output_format,
            verbose=verbose,
            expected=False,
        )

    if output_format == "machine":
        _print_machine_error(error=error)
        raise typer.Exit(code=1)
    if output_format != "text":
        raise ValueError(f"Unsupported CLI output format: {output_format}")

    if text_prefix is not None and verbose:
        _TERMINAL.plain(text_prefix, style="dim")
    _print_event(
        error.event,
        system_log_path=result.system_log_path,
        task_log_path=result.task_log_path,
        verbose=verbose,
    )
    raise typer.Exit(code=1)


def _exit_with_cli_error(
    *,
    definition: EventDefinition,
    message_params: dict[str, JSONValue],
    output_format: str,
    verbose: bool,
    expected: bool = True,
) -> NoReturn:
    error = _build_cli_public_error(
        definition=definition,
        message_params=message_params,
        expected=expected,
    )
    if output_format == "machine":
        _print_machine_error(error=error)
        raise typer.Exit(code=_cli_error_exit_code(definition))
    if output_format != "text":
        raise ValueError(f"Unsupported CLI output format: {output_format}")

    _print_event(error.event, verbose=verbose)
    raise typer.Exit(code=_cli_error_exit_code(definition))


def _cli_error_exit_code(definition: EventDefinition) -> int:
    if definition.category in {"cli_arguments", "cli_output"}:
        return 2
    return 1


def _build_cli_public_error(
    *,
    definition: EventDefinition,
    message_params: dict[str, JSONValue],
    expected: bool,
) -> PublicError:
    invocation = _current_cli_invocation()
    event = Event(
        code=definition.code,
        name=definition.name,
        type=definition.default_type,
        title=definition.title,
        message=definition.render_message(params=message_params),
        component=EventComponent.CLI,
        trace_id=UUID(invocation.trace_id) if invocation.trace_id is not None else None,
        command_id=UUID(invocation.command_id),
    )
    return PublicError(
        event=event,
        expected=expected,
        retryable=False,
        can_continue=False,
        safe_details=None,
    )


def _print_event(
    event: Event,
    *,
    system_log_path: Path | None = None,
    task_log_path: Path | None = None,
    verbose: bool = False,
) -> None:
    mode = TerminalMode.VERBOSE if verbose else TerminalMode.STANDARD
    _TERMINAL.event(event, mode=mode)
    if verbose and system_log_path is not None:
        _TERMINAL.plain(f"system_log: {system_log_path}", style="dim")
    if verbose and task_log_path is not None:
        _TERMINAL.plain(f"task_log: {task_log_path}", style="dim")
