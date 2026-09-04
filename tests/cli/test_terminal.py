from __future__ import annotations

from datetime import date
from io import StringIO

import pytest

from jelica_cli.terminal import TerminalPresenter, create_terminal_presenter, jelica_emoji
from jelica_cli.watcher import WatchTaskRow

ANSI_ESCAPE = "\x1b["


def test_color_enabled_preserves_styled_terminal_output() -> None:
    stream = StringIO()
    presenter = create_terminal_presenter(
        color=True,
        emoji=True,
        file=stream,
        force_terminal=True,
    )

    presenter.plain("Normal", style="blue")
    presenter.success("Success")
    presenter.warning("Careful")
    presenter.error("Broken")

    output = stream.getvalue()
    assert ANSI_ESCAPE in output
    assert "Normal" in output
    assert "Success" in output
    assert "Warning: Careful" in output
    assert "Error: Broken" in output


def test_color_disabled_removes_ansi_from_messages_table_and_watch() -> None:
    stream = StringIO()
    presenter = create_terminal_presenter(
        color=False,
        emoji=False,
        file=stream,
        force_terminal=True,
    )
    rows = (
        WatchTaskRow(
            task_id="task-1",
            job_id="job-1",
            state="running",
            stage="alignment",
            progress=42,
            warning_count=1,
        ),
    )

    presenter.info("Normal")
    presenter.success("Success")
    presenter.warning("Careful")
    presenter.error("Broken")
    with presenter.watch_live(
        rows,
        wait_for_new_tasks=False,
        enabled=True,
    ) as live:
        assert live is None
    presenter.finish_watch(
        rows,
        wait_for_new_tasks=False,
        live_was_enabled=True,
    )

    output = stream.getvalue()
    assert ANSI_ESCAPE not in output
    assert "Normal" in output
    assert "Success" in output
    assert "Warning: Careful" in output
    assert "Error: Broken" in output
    assert "task-1" in output
    assert "42%" in output


@pytest.mark.parametrize(
    ("value", "expected"),
    (
        (date(2026, 8, 5), "🌲"),
        (date(2026, 12, 24), "🎄"),
        (date(2027, 1, 7), "🎄"),
    ),
)
def test_jelica_emoji_selects_ordinary_and_seasonal_symbols(
    value: date,
    expected: str,
) -> None:
    assert jelica_emoji(value) == expected


def test_emoji_enabled_decorates_about_and_analysis_started() -> None:
    stream = StringIO()
    base_presenter = create_terminal_presenter(
        color=False,
        emoji=True,
        file=stream,
        force_terminal=True,
    )
    ordinary_presenter = TerminalPresenter(
        console=base_presenter.console,
        color_enabled=False,
        emoji_enabled=True,
        date_provider=lambda: date(2026, 8, 5),
    )
    seasonal_presenter = TerminalPresenter(
        console=base_presenter.console,
        color_enabled=False,
        emoji_enabled=True,
        date_provider=lambda: date(2026, 12, 25),
    )

    ordinary_presenter.about()
    seasonal_presenter.analysis_started("task-1")

    assert stream.getvalue().splitlines() == [
        "🌲 JELICA — Juxtaposing Evolutionary Lineages in Comparative Analysis",
        "🎄 Analysis task task-1 was created and started.",
    ]


def test_emoji_disabled_does_not_call_selector_and_keeps_spacing_clean() -> None:
    stream = StringIO()
    base_presenter = create_terminal_presenter(
        color=False,
        emoji=False,
        file=stream,
        force_terminal=True,
    )

    def unexpected_selector(_: date) -> str:
        raise AssertionError("symbol selector must not be called when emoji is disabled")

    def unexpected_date_provider() -> date:
        raise AssertionError("date provider must not be called when emoji is disabled")

    presenter = TerminalPresenter(
        console=base_presenter.console,
        color_enabled=False,
        emoji_enabled=False,
        symbol_selector=unexpected_selector,
        date_provider=unexpected_date_provider,
    )

    presenter.about()
    presenter.analysis_started("task-1")

    assert stream.getvalue().splitlines() == [
        "JELICA — Juxtaposing Evolutionary Lineages in Comparative Analysis",
        "Analysis task task-1 was created and started.",
    ]
