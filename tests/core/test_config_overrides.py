from __future__ import annotations

import pytest

from jelica_core.config import (
    MAX_CONFIG_OVERRIDE_ARRAY_INDEX,
    AnalysisConfigInput,
    ConfigArrayIndexSegment,
    ConfigObjectKeySegment,
    ConfigOverride,
    ConfigOverrideApplicationError,
    ConfigSchemaValidationError,
    InvalidConfigOverridePathError,
    apply_config_overrides,
    parse_cli_override,
    parse_cli_overrides,
    resolve_analysis_config,
)


def _parsed_value(raw_override: str) -> object:
    parsed = parse_cli_override(raw_override=raw_override, order=0)
    return parsed.value


def _apply_raw_overrides(
    raw_overrides: list[str],
    *,
    base_config: AnalysisConfigInput | None = None,
) -> AnalysisConfigInput:
    config = base_config or AnalysisConfigInput()
    parsed = parse_cli_overrides(raw_overrides)
    return apply_config_overrides(base_config=config, overrides=parsed)


def test_override_value_string_without_json_quotes() -> None:
    assert _parsed_value("--method=mafft") == "mafft"


def test_override_value_json_string() -> None:
    assert _parsed_value('--method="mafft"') == "mafft"


def test_override_value_integer() -> None:
    assert _parsed_value("--minimum=10") == 10


def test_override_value_float() -> None:
    assert _parsed_value("--minimum=3.14") == 3.14


def test_override_value_boolean() -> None:
    assert _parsed_value("--enabled=true") is True


def test_override_value_null() -> None:
    assert _parsed_value("--value=null") is None


def test_override_value_json_array() -> None:
    assert _parsed_value("--values=[1,2,3]") == [1, 2, 3]


def test_override_value_json_object() -> None:
    assert _parsed_value('--filters={"minimum":1000}') == {"minimum": 1000}


def test_override_value_empty_string_after_equals() -> None:
    assert _parsed_value("--name=") == ""


def test_override_value_with_additional_equals_symbol() -> None:
    assert _parsed_value("--token=abc=def=ghi") == "abc=def=ghi"


def test_override_key_case_is_preserved() -> None:
    override = parse_cli_override(raw_override="--Output.Format=json", order=0)

    first_segment = override.path[0]
    second_segment = override.path[1]
    assert isinstance(first_segment, ConfigObjectKeySegment)
    assert isinstance(second_segment, ConfigObjectKeySegment)
    assert first_segment.key == "Output"
    assert second_segment.key == "Format"


def test_override_rejects_empty_key() -> None:
    with pytest.raises(InvalidConfigOverridePathError):
        parse_cli_override(raw_override="--=value", order=0)


def test_override_rejects_missing_equals() -> None:
    with pytest.raises(InvalidConfigOverridePathError):
        parse_cli_override(raw_override="--samples", order=0)


def test_override_rejects_empty_path_segment() -> None:
    with pytest.raises(InvalidConfigOverridePathError):
        parse_cli_override(raw_override="--a..b=1", order=0)


def test_override_rejects_negative_array_index() -> None:
    with pytest.raises(InvalidConfigOverridePathError):
        parse_cli_override(raw_override="--filters.-1.minimum=10", order=0)


def test_override_creates_nested_object() -> None:
    updated = _apply_raw_overrides(["--options.method=mafft"])

    assert updated.model_dump()["options"] == {"method": "mafft"}


def test_override_creates_array_of_objects() -> None:
    updated = _apply_raw_overrides(["--filters.0.minimum=1000"])

    assert updated.model_dump()["filters"] == [{"minimum": 1000}]


def test_override_creates_sparse_array_with_nulls() -> None:
    updated = _apply_raw_overrides(["--filters.2.minimum=1000"])

    assert updated.model_dump()["filters"] == [None, None, {"minimum": 1000}]


def test_override_replaces_incompatible_scalar_with_object() -> None:
    base = AnalysisConfigInput.model_validate({"samples": ["sample-a"], "options": "flat"})

    updated = _apply_raw_overrides(["--options.method=clustal"], base_config=base)

    assert updated.model_dump()["options"] == {"method": "clustal"}


def test_override_replaces_incompatible_scalar_with_array() -> None:
    base = AnalysisConfigInput.model_validate({"samples": ["sample-a"], "filters": "invalid"})

    updated = _apply_raw_overrides(["--filters.0.minimum=1000"], base_config=base)

    assert updated.model_dump()["filters"] == [{"minimum": 1000}]


def test_override_repeated_exact_path_replaces_previous_value() -> None:
    updated = _apply_raw_overrides(["--format=fasta", "--format=genbank"])

    assert updated.model_dump()["format"] == "genbank"


def test_override_object_then_nested_change() -> None:
    updated = _apply_raw_overrides(
        ['--filters=[{"minimum":500}]', "--filters.0.minimum=1000"],
    )

    assert updated.model_dump()["filters"] == [{"minimum": 1000}]


def test_override_nested_change_then_full_replace() -> None:
    updated = _apply_raw_overrides(
        ["--filters.0.minimum=1000", '--filters=[{"minimum":500}]'],
    )

    assert updated.model_dump()["filters"] == [{"minimum": 500}]


def test_override_operations_are_applied_in_stable_order() -> None:
    parsed = [
        ConfigOverride(
            raw_parameter="value",
            path=(ConfigObjectKeySegment(key="value"),),
            value="first",
            order=0,
        ),
        ConfigOverride(
            raw_parameter="value",
            path=(ConfigObjectKeySegment(key="value"),),
            value="second",
            order=1,
        ),
    ]

    updated = apply_config_overrides(base_config=AnalysisConfigInput(), overrides=parsed)

    assert updated.model_dump()["value"] == "second"


def test_override_rejects_excessive_array_index() -> None:
    too_large_index = MAX_CONFIG_OVERRIDE_ARRAY_INDEX + 1

    with pytest.raises(InvalidConfigOverridePathError):
        parse_cli_override(raw_override=f"--filters.{too_large_index}.minimum=1", order=0)


def test_override_application_does_not_mutate_input_model() -> None:
    base = AnalysisConfigInput.model_validate({"samples": ["sample-a"], "options": {"a": 1}})
    base_dump = base.model_dump()
    base_fields_set = set(base.model_fields_set)

    _apply_raw_overrides(["--options.a=2"], base_config=base)

    assert base.model_dump() == base_dump
    assert base.model_fields_set == base_fields_set


def test_override_rejects_root_level_array_index() -> None:
    parsed = parse_cli_overrides(["--0=value"])

    with pytest.raises(ConfigOverrideApplicationError):
        apply_config_overrides(base_config=AnalysisConfigInput(), overrides=parsed)


def test_override_array_segment_is_parsed_as_array_index_type() -> None:
    parsed = parse_cli_override(raw_override="--filters.0.minimum=1000", order=0)

    assert isinstance(parsed.path[1], ConfigArrayIndexSegment)


def test_sparse_samples_override_passes_model_and_resolver() -> None:
    updated = _apply_raw_overrides(
        ['--samples=["Sample_5.fasta"]', "--samples.2=Sample_6.fasta"],
    )
    resolution = resolve_analysis_config(updated)

    assert updated.samples == ["Sample_5.fasta", None, "Sample_6.fasta"]
    assert resolution.config.samples == ["Sample_5.fasta", None, "Sample_6.fasta"]


def test_priority_override_is_applied() -> None:
    updated = _apply_raw_overrides(
        ["--priority=5"],
        base_config=AnalysisConfigInput(samples=["sample-a"]),
    )
    resolution = resolve_analysis_config(updated)

    assert updated.priority == 5
    assert resolution.config.priority == 5


def test_priority_override_below_minimum_is_rejected() -> None:
    with pytest.raises(ConfigSchemaValidationError):
        _apply_raw_overrides(
            ["--priority=0"],
            base_config=AnalysisConfigInput(samples=["sample-a"]),
        )


def test_alignment_mode_override_is_applied() -> None:
    updated = _apply_raw_overrides(
        ["--alignment.mode=none"],
        base_config=AnalysisConfigInput(samples=["sample-a"]),
    )
    resolution = resolve_analysis_config(updated)

    assert resolution.config.alignment.mode == "none"


def test_reference_override_is_applied() -> None:
    updated = _apply_raw_overrides(
        ["--reference=data/alignment.afa::NC_045512.2"],
        base_config=AnalysisConfigInput(samples=["sample-a"]),
    )
    resolution = resolve_analysis_config(updated)

    assert resolution.config.reference == "data/alignment.afa::NC_045512.2"


def test_statistics_kmer_strand_override_is_applied() -> None:
    updated = _apply_raw_overrides(
        ["--statistics.kmer_strand=reverse_complement"],
        base_config=AnalysisConfigInput(samples=["sample-a"]),
    )
    resolution = resolve_analysis_config(updated)

    assert resolution.config.statistics.kmer_strand == "reverse_complement"


def test_typed_alignment_dot_notation_overrides_are_applied() -> None:
    updated = _apply_raw_overrides(
        [
            "--alignment.mode=compute",
            "--alignment.engine=mafft",
            "--alignment.construction=reference_guided",
            "--alignment.mafft.strategy=l_ins_i",
            "--alignment.mafft.direction_adjustment=fast",
            "--alignment.mafft.memory_mode=save",
            "--alignment.mafft.threads=4",
            "--alignment.mafft.gap_open_penalty=1.53",
            "--alignment.mafft.offset=0.0",
            "--alignment.mafft.progressive_threads=auto",
            "--alignment.mafft.iterative_threads=disabled",
            "--reference=ref",
        ],
        base_config=AnalysisConfigInput(samples=["sample-a"]),
    )
    resolution = resolve_analysis_config(updated)

    alignment = resolution.config.alignment
    assert alignment.engine == "mafft"
    assert alignment.construction == "reference_guided"
    assert alignment.mafft is not None
    assert alignment.mafft.strategy == "l_ins_i"
    assert alignment.mafft.direction_adjustment == "fast"
    assert alignment.mafft.memory_mode == "save"
    assert alignment.mafft.threads == 4
    assert alignment.mafft.gap_open_penalty == 1.53
    assert alignment.mafft.offset == 0.0
    assert alignment.mafft.progressive_threads == "auto"
    assert alignment.mafft.iterative_threads == "disabled"


def test_dot_notation_rejects_auto_strategy_scoring_override() -> None:
    with pytest.raises(ConfigSchemaValidationError):
        _apply_raw_overrides(
            [
                "--alignment.mode=compute",
                "--alignment.mafft.strategy=auto",
                "--alignment.mafft.gap_open_penalty=1.0",
            ],
            base_config=AnalysisConfigInput(samples=["sample-a"]),
        )


def test_dot_notation_rejects_mafft_settings_for_prealigned_mode() -> None:
    with pytest.raises(ConfigSchemaValidationError):
        _apply_raw_overrides(
            [
                "--alignment.mode=prealigned",
                "--alignment.mafft.strategy=l_ins_i",
            ],
            base_config=AnalysisConfigInput(samples=["sample-a"]),
        )
