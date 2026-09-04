from __future__ import annotations

from jelica_cli.events import CLI_EVENT_DEFINITIONS, CLI_EVENTS_BY_CODE, CLI_EVENTS_BY_NAME


def test_cli_event_catalog_has_unique_codes_and_names() -> None:
    assert len(CLI_EVENT_DEFINITIONS) == len(CLI_EVENTS_BY_CODE)
    assert len(CLI_EVENT_DEFINITIONS) == len(CLI_EVENTS_BY_NAME)


def test_cli_event_catalog_respects_cli_code_range_and_prefix() -> None:
    for definition in CLI_EVENT_DEFINITIONS:
        assert 3000 <= definition.code <= 3999
        assert definition.name.startswith("CLI_")
