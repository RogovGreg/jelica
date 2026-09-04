from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import date
from enum import StrEnum
from typing import Any, Callable, Iterator, TextIO

from rich import box
from rich.console import Console
from rich.live import Live
from rich.table import Table
from rich.text import Text

from jelica_contracts import Event, EventType

from .watcher import WatchTaskRow


class TerminalMode(StrEnum):
    STANDARD = "standard"
    VERBOSE = "verbose"
    QUIET = "quiet"


def _text(message: object, *, style: str | None = None) -> Text:
    if style is None:
        return Text(str(message))
    return Text(str(message), style=style)


def jelica_emoji(value: date) -> str:
    if (value.month == 12 and 24 <= value.day <= 26) or (value.month == 1 and 6 <= value.day <= 8):
        return "🎄"
    return "🌲"


def create_terminal_presenter(
    *,
    color: bool = True,
    emoji: bool = True,
    file: TextIO | None = None,
    force_terminal: bool | None = None,
) -> TerminalPresenter:
    console = _create_console(
        color=color,
        emoji=emoji,
        file=file,
        force_terminal=force_terminal,
    )
    return TerminalPresenter(
        console=console,
        color_enabled=color,
        emoji_enabled=emoji,
    )


def _create_console(
    *,
    color: bool,
    emoji: bool,
    file: TextIO | None = None,
    force_terminal: bool | None = None,
) -> Console:
    console = Console(
        file=file,
        force_terminal=force_terminal,
        highlight=False,
        no_color=True if not color else False if force_terminal else None,
        color_system="standard" if color and force_terminal else "auto" if color else None,
        emoji=emoji,
    )
    return console


class TerminalPresenter:
    def __init__(
        self,
        *,
        console: Console | None = None,
        color_enabled: bool = True,
        emoji_enabled: bool = True,
        symbol_selector: Callable[[date], str] | None = None,
        date_provider: Callable[[], date] | None = None,
    ) -> None:
        if console is None:
            console = _create_console(
                color=color_enabled,
                emoji=emoji_enabled,
            )
        self.console = console
        self.color_enabled = color_enabled
        self.emoji_enabled = emoji_enabled
        self._symbol_selector = jelica_emoji if symbol_selector is None else symbol_selector
        self._date_provider = date.today if date_provider is None else date_provider
        self._specific_error_tasks: set[str] = set()

    def plain(self, message: object = "", *, style: str | None = None) -> None:
        self.console.print(_text(message, style=style), soft_wrap=True)

    def raw(self, message: str) -> None:
        stream = self.console.file
        stream.write(message)
        if not message.endswith("\n"):
            stream.write("\n")
        stream.flush()

    def json(self, payload: Any) -> None:
        self.raw(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))

    def reset_event_context(self) -> None:
        self._specific_error_tasks.clear()

    def info(self, message: str) -> None:
        self.plain(message)

    def success(self, message: str) -> None:
        self.plain(message, style="green")

    def warning(self, message: str) -> None:
        prefix = "" if message.startswith("Warning:") else "Warning: "
        self.plain(f"{prefix}{message}", style="yellow")

    def error(self, message: str) -> None:
        prefix = "" if message.startswith("Error:") else "Error: "
        self.plain(f"{prefix}{message}", style="red")

    def analysis_started(self, task_id: str) -> None:
        self.success(self._decorate(f"Analysis task {task_id} was created and started."))

    def about(self) -> None:
        self.info(
            self._decorate("JELICA — Juxtaposing Evolutionary Lineages in Comparative Analysis")
        )

    def _decorate(self, message: str) -> str:
        if not self.emoji_enabled:
            return message
        symbol = self._symbol_selector(self._date_provider()).strip()
        return message if symbol == "" else f"{symbol} {message}"

    def event(self, event: Event, *, mode: TerminalMode = TerminalMode.STANDARD) -> None:
        if (
            event.name == "CORE_COMPARATIVE_ANALYSIS_OPERATION_FAILED"
            and mode is not TerminalMode.VERBOSE
        ):
            return
        if mode is TerminalMode.QUIET and event.type not in {
            EventType.ERROR,
            EventType.CRITICAL,
        }:
            return
        if event.type in {EventType.ERROR, EventType.CRITICAL}:
            if (
                event.name == "CORE_RUNTIME_JOB_FAILED"
                and event.task_id in self._specific_error_tasks
            ):
                return
            if event.name != "CORE_RUNTIME_JOB_FAILED" and event.task_id is not None:
                self._specific_error_tasks.add(event.task_id)
            self.error(event.message)
            self._event_diagnostics(event, mode=mode)
            return
        if event.type is EventType.WARNING:
            if mode is not TerminalMode.QUIET:
                self.warning(event.message)
            self._event_diagnostics(event, mode=mode)
            return

        standard_message = _standard_event_message(event)
        if standard_message is None and mode is not TerminalMode.VERBOSE:
            return
        message = standard_message or event.message
        if event.type is EventType.SUCCESS:
            self.success(message)
        else:
            self.info(message)
        self._event_diagnostics(event, mode=mode)

    def watch_missing(self, task_id: str) -> None:
        self.error(f"Task {task_id} was not found.")

    def watch_inactive(
        self,
        task_id: str,
        state: str,
        task_name: str | None = None,
    ) -> None:
        name_suffix = "" if task_name is None else f" Name: {task_name}."
        self.info(f"Task {task_id} is {state}; it was not added to watch.{name_suffix}")

    @contextmanager
    def watch_live(
        self,
        rows: tuple[WatchTaskRow, ...],
        *,
        wait_for_new_tasks: bool,
        enabled: bool,
    ) -> Iterator[Live | None]:
        if not enabled or not self.color_enabled:
            yield None
            return
        live = Live(
            self.watch_table(rows, wait_for_new_tasks=wait_for_new_tasks),
            console=self.console,
            refresh_per_second=4,
            transient=False,
        )
        with live:
            yield live

    def update_watch(
        self,
        live: Live | None,
        rows: tuple[WatchTaskRow, ...],
        *,
        wait_for_new_tasks: bool,
    ) -> None:
        if live is None:
            return
        live.update(self.watch_table(rows, wait_for_new_tasks=wait_for_new_tasks))

    def finish_watch(
        self,
        rows: tuple[WatchTaskRow, ...],
        *,
        wait_for_new_tasks: bool,
        live_was_enabled: bool,
    ) -> None:
        if live_was_enabled and self.color_enabled:
            return
        if len(rows) > 0:
            self.console.print(self.watch_table(rows, wait_for_new_tasks=wait_for_new_tasks))

    def watch_table(
        self,
        rows: tuple[WatchTaskRow, ...],
        *,
        wait_for_new_tasks: bool,
    ) -> Table:
        table = Table(box=box.SIMPLE, show_edge=False, highlight=False)
        table.add_column("Task name", no_wrap=True)
        table.add_column("Task ID", no_wrap=True)
        table.add_column("State", no_wrap=True)
        table.add_column("Stage")
        table.add_column("Progress", justify="right", no_wrap=True)
        table.add_column("Warnings", justify="right", no_wrap=True)
        for row in rows:
            row_style = _task_row_style(row.state)
            progress_style = "dim" if not row.terminal else None
            warning_style = "yellow" if row.warning_count > 0 else None
            table.add_row(
                row.task_name or "-",
                row.task_id,
                row.state,
                row.stage or "-",
                _text(f"{row.progress}%", style=progress_style),
                _text(row.warning_count, style=warning_style),
                style=row_style,
            )
        if wait_for_new_tasks:
            table.caption = "Waiting for active tasks..."
            table.caption_style = "dim"
        return table

    def watching_stopped(
        self,
        rows: tuple[WatchTaskRow, ...],
        *,
        explicit: bool,
        requested_task_ids: tuple[str, ...] = tuple(),
    ) -> None:
        unfinished = tuple(row for row in rows if not row.terminal)
        if len(unfinished) == 1:
            task_id = unfinished[0].task_id
            self.info(f"Watching stopped. Task {task_id} continues running.")
            self.info(f"Resume watching: jelica tasks watch {task_id}")
            self.info(f"Cancel task: jelica tasks cancel {task_id}")
            return
        if len(unfinished) > 1:
            self.info(f"Watching stopped. {len(unfinished)} unfinished tasks continue running.")
            resume = "jelica tasks watch TASK_ID..." if explicit else "jelica tasks watch"
            self.info(f"Resume watching: {resume}")
            self.info("Cancel a task: jelica tasks cancel TASK_ID")
            return
        self.info("Watching stopped. No task states were changed.")
        if len(rows) == 0 and len(requested_task_ids) == 1:
            task_id = requested_task_ids[0]
            self.info(f"Resume watching: jelica tasks watch {task_id}")
            self.info(f"Cancel task: jelica tasks cancel {task_id}")
        elif len(rows) == 0 and len(requested_task_ids) > 1:
            self.info("Resume watching: jelica tasks watch TASK_ID...")
            self.info("Cancel a task: jelica tasks cancel TASK_ID")
        elif len(rows) == 0 and not explicit:
            self.info("Resume watching: jelica tasks watch")

    def _event_diagnostics(self, event: Event, *, mode: TerminalMode) -> None:
        if mode is not TerminalMode.VERBOSE:
            return
        self.plain(f"event: {event.name} ({event.code})", style="dim")
        if event.diagnostics is not None:
            diagnostics = event.diagnostics.model_dump(mode="json", exclude_none=True)
            if diagnostics:
                self.plain(
                    json.dumps(diagnostics, ensure_ascii=False, indent=2, sort_keys=True),
                    style="dim",
                )


def _standard_event_message(event: Event) -> str | None:
    context = event.context or {}
    if event.name == "CORE_RUNTIME_STAGE_STARTED":
        return f"Stage started: {context.get('stage_id', event.stage or 'unknown')}"
    if event.name == "CORE_RUNTIME_STAGE_COMMITTED":
        if context.get("stage_id", event.stage) == "comparative_analysis":
            return None
        return f"Stage completed: {context.get('stage_id', event.stage or 'unknown')}"
    if event.name == "CORE_INPUT_ACQUISITION_COMPLETED":
        return event.message
    if event.name == "CORE_INPUT_PROCESSING_STARTED":
        file_count = context.get("input_file_count", "?")
        return f"Input processing started: {file_count} files."
    if event.name == "CORE_INPUT_PROCESSING_COMPLETED":
        valid = context.get("valid_sample_count", "?")
        invalid = context.get("invalid_sample_count", "?")
        return f"Input processing completed: {valid} valid, {invalid} invalid."
    if event.name == "CORE_ALIGNMENT_SKIPPED":
        reason = context.get("reason")
        return "Alignment skipped." if reason is None else f"Alignment skipped: {reason}."
    if event.name == "CORE_ALIGNMENT_COMPLETED":
        return "Alignment completed."
    if event.name == "CORE_ALIGNMENT_RESULT_PUBLISHED":
        return "Alignment result published."
    if event.name == "CORE_COMPARATIVE_ANALYSIS_SKIPPED":
        return "Comparative analysis skipped."
    if event.name == "CORE_COMPARATIVE_ANALYSIS_RESULT_PUBLISHED":
        return "Comparative-analysis result published."
    if event.name == "CORE_COMPARATIVE_ANALYSIS_COMPLETED":
        return "Comparative analysis completed."
    if event.name == "CORE_RUNTIME_JOB_COMPLETED":
        return f"Task {event.task_id or '<unknown>'} completed."
    return None


def _task_row_style(state: str) -> str | None:
    if state == "completed":
        return "green"
    if state == "failed":
        return "red"
    if state in {"paused", "cancelled"}:
        return "dim"
    return None
