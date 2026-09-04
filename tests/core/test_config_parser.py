from __future__ import annotations

import json

import pytest

from jelica_core.config import (
    AnalysisConfigError,
    ConfigParser,
    ConfigSchemaValidationError,
    EmptyConfigJsonError,
    InvalidConfigJsonRootTypeError,
    InvalidConfigJsonSyntaxError,
    ResolvedAnalysisAlignmentConfig,
    UnsupportedConfigSchemaVersionError,
    resolve_analysis_config,
)


def test_config_parser_parses_empty_object() -> None:
    parser = ConfigParser()

    parsed = parser.parse("{}")

    assert parsed.samples is None
    assert parsed.priority == 1
    assert parsed.alignment is None
    assert parsed.reference is None
    assert parsed.statistics is None
    assert parsed.model_extra == {}


def test_config_parser_parses_object_with_samples() -> None:
    parser = ConfigParser()

    parsed = parser.parse('{"samples":[" sample-a ","sample-b"]}')

    assert parsed.samples == ["sample-a", "sample-b"]
    assert parsed.priority == 1


def test_config_parser_parses_explicit_priority() -> None:
    parser = ConfigParser()

    parsed = parser.parse('{"samples":["sample-a"],"priority":5}')

    assert parsed.priority == 5


def test_config_parser_parses_alignment_mode() -> None:
    parser = ConfigParser()

    parsed = parser.parse('{"samples":["sample-a"],"alignment":{"mode":"prealigned"}}')

    assert parsed.alignment is not None
    assert parsed.alignment.mode == "prealigned"


def test_config_parser_parses_reference_selector() -> None:
    parser = ConfigParser()

    parsed = parser.parse(
        '{"samples":["sample-a"],"reference":" data/alignment.afa::NC_045512.2 "}'
    )

    assert parsed.reference == "data/alignment.afa::NC_045512.2"


def test_config_parser_parses_statistics_and_normalizes_kmers() -> None:
    parser = ConfigParser()

    parsed = parser.parse(
        (
            '{"samples":["sample-a"],"statistics":{"kmers":[" atg ","arcg","ATG"],'
            '"kmer_strand":"both"}}'
        )
    )

    assert parsed.statistics is not None
    assert parsed.statistics.kmers == ["ATG", "ARCG"]
    assert parsed.statistics.kmer_strand == "both"


def test_config_parser_accepts_full_iupac_kmer_query() -> None:
    parser = ConfigParser()

    parsed = parser.parse(
        '{"samples":["sample-a"],"statistics":{"kmers":[" ACGTURYSWKMBDHVN "]}}'
    )

    assert parsed.statistics is not None
    assert parsed.statistics.kmers == ["ACGTURYSWKMBDHVN"]


@pytest.mark.parametrize("kmer_value", ["A", "N", " a "])
def test_config_parser_rejects_single_symbol_kmer(kmer_value: str) -> None:
    parser = ConfigParser()

    with pytest.raises(ConfigSchemaValidationError):
        parser.parse(
            json.dumps(
                {
                    "samples": ["sample-a"],
                    "statistics": {"kmers": [kmer_value]},
                }
            )
        )


def test_config_parser_accepts_two_symbol_kmers() -> None:
    parser = ConfigParser()

    parsed = parser.parse('{"samples":["sample-a"],"statistics":{"kmers":["AT","RY"]}}')

    assert parsed.statistics is not None
    assert parsed.statistics.kmers == ["AT", "RY"]


def test_config_parser_parses_object_with_sparse_samples() -> None:
    parser = ConfigParser()

    parsed = parser.parse('{"samples":[" sample-a ",null,"sample-b"]}')

    assert parsed.samples == ["sample-a", None, "sample-b"]


def test_config_parser_preserves_unknown_fields() -> None:
    parser = ConfigParser()

    parsed = parser.parse('{"samples":["sample-a"],"unknown":{"flag":true}}')

    assert parsed.model_extra == {"unknown": {"flag": True}}


def test_config_parser_rejects_empty_text() -> None:
    parser = ConfigParser()

    with pytest.raises(EmptyConfigJsonError):
        parser.parse("   \n\t  ")


def test_config_parser_rejects_invalid_json_syntax() -> None:
    parser = ConfigParser()

    with pytest.raises(InvalidConfigJsonSyntaxError):
        parser.parse('{"samples": [}')


def test_config_parser_json_syntax_error_includes_line_and_column() -> None:
    parser = ConfigParser()

    with pytest.raises(InvalidConfigJsonSyntaxError) as error_info:
        parser.parse('{\n  "samples": [\n}')

    error = error_info.value
    assert error.line == 3
    assert error.column >= 1
    assert "line 3, column" in str(error)


@pytest.mark.parametrize(
    ("json_text", "expected_type"),
    [
        ("[]", "array"),
        ('"value"', "string"),
        ("10", "number"),
        ("false", "boolean"),
        ("null", "null"),
    ],
)
def test_config_parser_rejects_non_object_root_values(
    json_text: str,
    expected_type: str,
) -> None:
    parser = ConfigParser()

    with pytest.raises(InvalidConfigJsonRootTypeError) as error_info:
        parser.parse(json_text)

    assert error_info.value.json_type == expected_type


def test_config_parser_rejects_known_field_schema_mismatch() -> None:
    parser = ConfigParser()

    with pytest.raises(ConfigSchemaValidationError):
        parser.parse('{"samples":"sample-a"}')


def test_config_parser_rejects_empty_sample_string() -> None:
    parser = ConfigParser()

    with pytest.raises(ConfigSchemaValidationError):
        parser.parse('{"samples":["sample-a",""]}')


def test_config_parser_rejects_whitespace_only_sample_string() -> None:
    parser = ConfigParser()

    with pytest.raises(ConfigSchemaValidationError):
        parser.parse('{"samples":["sample-a","   "]}')


def test_config_parser_rejects_numeric_sample_item() -> None:
    parser = ConfigParser()

    with pytest.raises(ConfigSchemaValidationError):
        parser.parse('{"samples":["sample-a",123]}')


def test_config_parser_rejects_object_sample_item() -> None:
    parser = ConfigParser()

    with pytest.raises(ConfigSchemaValidationError):
        parser.parse('{"samples":["sample-a",{"path":"x"}]}')


def test_config_parser_rejects_unknown_alignment_mode() -> None:
    parser = ConfigParser()

    with pytest.raises(ConfigSchemaValidationError):
        parser.parse('{"samples":["sample-a"],"alignment":{"mode":"custom"}}')


def test_config_parser_rejects_empty_reference_selector() -> None:
    parser = ConfigParser()

    with pytest.raises(ConfigSchemaValidationError):
        parser.parse('{"samples":["sample-a"],"reference":"   "}')


def test_config_parser_rejects_unknown_kmer_strand() -> None:
    parser = ConfigParser()

    with pytest.raises(ConfigSchemaValidationError):
        parser.parse('{"samples":["sample-a"],"statistics":{"kmer_strand":"unknown"}}')


def test_config_parser_rejects_kmer_with_gap() -> None:
    parser = ConfigParser()

    with pytest.raises(ConfigSchemaValidationError):
        parser.parse('{"samples":["sample-a"],"statistics":{"kmers":["AC-G"]}}')


def test_config_parser_rejects_kmer_with_invalid_symbol() -> None:
    parser = ConfigParser()

    with pytest.raises(ConfigSchemaValidationError):
        parser.parse('{"samples":["sample-a"],"statistics":{"kmers":["AXG"]}}')


def test_config_parser_rejects_priority_below_minimum() -> None:
    parser = ConfigParser()

    with pytest.raises(ConfigSchemaValidationError):
        parser.parse('{"samples":["sample-a"],"priority":0}')


def test_config_parser_unsupported_schema_version_is_reported_by_resolver() -> None:
    parser = ConfigParser()
    parsed = parser.parse(json.dumps({"schema_version": 999, "samples": ["sample-a"]}))

    with pytest.raises(UnsupportedConfigSchemaVersionError):
        resolve_analysis_config(parsed)


def test_config_parser_exposes_only_domain_errors() -> None:
    parser = ConfigParser()

    with pytest.raises(AnalysisConfigError):
        parser.parse('{"samples":[}')

    with pytest.raises(AnalysisConfigError):
        parser.parse('{"samples": 10}')


def test_compute_alignment_defaults_are_fully_resolved() -> None:
    resolution = resolve_analysis_config(ConfigParser().parse('{"samples":["sample-a"]}'))

    alignment = resolution.config.alignment
    assert alignment.mode == "compute"
    assert alignment.engine == "mafft"
    assert alignment.construction == "joint"
    assert alignment.mafft is not None
    assert alignment.mafft.model_dump(mode="json") == {
        "strategy": "auto",
        "direction_adjustment": "none",
        "memory_mode": "auto",
        "threads": 1,
        "gap_open_penalty": None,
        "offset": None,
        "progressive_threads": "auto",
        "iterative_threads": "auto",
    }


def test_resolved_compute_alignment_applies_defaults_to_legacy_shape() -> None:
    alignment = ResolvedAnalysisAlignmentConfig.model_validate({"mode": "compute"})

    assert alignment.engine == "mafft"
    assert alignment.construction == "joint"
    assert alignment.mafft is not None
    assert alignment.mafft.strategy == "auto"


def test_config_parser_rejects_unknown_alignment_engine() -> None:
    with pytest.raises(ConfigSchemaValidationError):
        ConfigParser().parse(
            '{"samples":["sample-a"],"alignment":{"mode":"compute","engine":"muscle"}}'
        )


@pytest.mark.parametrize(
    "strategy",
    [
        "auto",
        "fft_ns_1",
        "fft_ns_2",
        "fft_ns_i",
        "nw_ns_1",
        "nw_ns_2",
        "nw_ns_i",
        "g_ins_i",
        "l_ins_i",
        "e_ins_i",
    ],
)
def test_config_parser_accepts_supported_mafft_strategies(strategy: str) -> None:
    parsed = ConfigParser().parse(
        json.dumps(
            {
                "samples": ["sample-a"],
                "alignment": {"mode": "compute", "mafft": {"strategy": strategy}},
            }
        )
    )

    assert parsed.alignment is not None
    assert parsed.alignment.mafft is not None
    assert parsed.alignment.mafft.strategy == strategy


def test_config_parser_rejects_unknown_mafft_strategy() -> None:
    with pytest.raises(ConfigSchemaValidationError):
        ConfigParser().parse(
            '{"samples":["sample-a"],"alignment":{"mode":"compute",'
            '"mafft":{"strategy":"custom"}}}'
        )


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("gap_open_penalty", 1.53),
        ("offset", 0.0),
        ("progressive_threads", "auto"),
        ("iterative_threads", "disabled"),
    ],
)
def test_auto_strategy_rejects_algorithm_and_scoring_overrides(
    field_name: str, value: object
) -> None:
    document = {
        "samples": ["sample-a"],
        "alignment": {
            "mode": "compute",
            "mafft": {"strategy": "auto", field_name: value},
        },
    }

    with pytest.raises(ConfigSchemaValidationError):
        ConfigParser().parse(json.dumps(document))


def test_named_strategy_accepts_typed_mafft_overrides() -> None:
    parsed = ConfigParser().parse(
        json.dumps(
            {
                "samples": ["sample-a"],
                "alignment": {
                    "mode": "compute",
                    "mafft": {
                        "strategy": "l_ins_i",
                        "direction_adjustment": "accurate",
                        "memory_mode": "save",
                        "threads": "auto",
                        "gap_open_penalty": 1.53,
                        "offset": 0,
                        "progressive_threads": 2,
                        "iterative_threads": "disabled",
                    },
                },
            }
        )
    )
    resolution = resolve_analysis_config(parsed)

    mafft = resolution.config.alignment.mafft
    assert mafft is not None
    assert mafft.strategy == "l_ins_i"
    assert mafft.direction_adjustment == "accurate"
    assert mafft.memory_mode == "save"
    assert mafft.threads == "auto"
    assert mafft.gap_open_penalty == 1.53
    assert mafft.offset == 0.0
    assert mafft.progressive_threads == 2
    assert mafft.iterative_threads == "disabled"


@pytest.mark.parametrize("value", ["auto", 1, 8])
def test_mafft_threads_accept_auto_or_positive_strict_integer(value: object) -> None:
    parsed = ConfigParser().parse(
        json.dumps(
            {
                "samples": ["sample-a"],
                "alignment": {"mode": "compute", "mafft": {"threads": value}},
            }
        )
    )

    assert parsed.alignment is not None
    assert parsed.alignment.mafft is not None
    assert parsed.alignment.mafft.threads == value


@pytest.mark.parametrize("value", [0, -1, 1.5, True, "1", "disabled"])
def test_mafft_threads_reject_non_positive_or_non_strict_values(value: object) -> None:
    document = {
        "samples": ["sample-a"],
        "alignment": {"mode": "compute", "mafft": {"threads": value}},
    }

    with pytest.raises(ConfigSchemaValidationError):
        ConfigParser().parse(json.dumps(document))


@pytest.mark.parametrize("field_name", ["progressive_threads", "iterative_threads"])
@pytest.mark.parametrize("value", ["auto", "disabled", 1, 8])
def test_named_strategy_phase_threads_accept_supported_values(
    field_name: str, value: object
) -> None:
    parsed = ConfigParser().parse(
        json.dumps(
            {
                "samples": ["sample-a"],
                "alignment": {
                    "mode": "compute",
                    "mafft": {"strategy": "fft_ns_i", field_name: value},
                },
            }
        )
    )

    assert parsed.alignment is not None
    assert parsed.alignment.mafft is not None
    assert getattr(parsed.alignment.mafft, field_name) == value


@pytest.mark.parametrize("field_name", ["progressive_threads", "iterative_threads"])
@pytest.mark.parametrize("value", [0, -1, 1.5, True, "1"])
def test_named_strategy_phase_threads_reject_invalid_values(
    field_name: str, value: object
) -> None:
    document = {
        "samples": ["sample-a"],
        "alignment": {
            "mode": "compute",
            "mafft": {"strategy": "fft_ns_i", field_name: value},
        },
    }

    with pytest.raises(ConfigSchemaValidationError):
        ConfigParser().parse(json.dumps(document))


@pytest.mark.parametrize("field_name", ["gap_open_penalty", "offset"])
@pytest.mark.parametrize("value", [-0.1, float("nan"), float("inf"), float("-inf"), True, "1.0"])
def test_mafft_scoring_overrides_require_nonnegative_finite_numbers(
    field_name: str, value: object
) -> None:
    document = {
        "samples": ["sample-a"],
        "alignment": {
            "mode": "compute",
            "mafft": {"strategy": "g_ins_i", field_name: value},
        },
    }

    with pytest.raises(ConfigSchemaValidationError):
        ConfigParser().parse(json.dumps(document))


def test_config_parser_rejects_arbitrary_mafft_arguments() -> None:
    with pytest.raises(ConfigSchemaValidationError):
        ConfigParser().parse(
            '{"samples":["sample-a"],"alignment":{"mode":"compute",'
            '"mafft":{"extra_arguments":["--treeout"]}}}'
        )


@pytest.mark.parametrize("mode", ["prealigned", "none"])
@pytest.mark.parametrize(
    "compute_setting",
    [
        {"engine": "mafft"},
        {"construction": "joint"},
        {"mafft": {}},
    ],
)
def test_non_compute_modes_reject_compute_alignment_settings(
    mode: str, compute_setting: dict[str, object]
) -> None:
    document = {
        "samples": ["sample-a"],
        "alignment": {"mode": mode, **compute_setting},
    }

    with pytest.raises(ConfigSchemaValidationError):
        ConfigParser().parse(json.dumps(document))


def test_none_mode_remains_supported_without_compute_settings() -> None:
    resolution = resolve_analysis_config(
        ConfigParser().parse(
            '{"samples":["sample-a"],"alignment":{"mode":"none"},'
            '"comparative_analysis":{"enabled":false},'
            '"distance_matrix":{"enabled":false},"phylogenetic_tree":{"enabled":false}}'
        )
    )

    assert resolution.config.alignment.mode == "none"
    assert resolution.config.alignment.engine is None
    assert resolution.config.alignment.construction is None
    assert resolution.config.alignment.mafft is None


def test_reference_guided_requires_top_level_reference() -> None:
    with pytest.raises(ConfigSchemaValidationError):
        ConfigParser().parse(
            '{"samples":["sample-a"],"alignment":'
            '{"mode":"compute","construction":"reference_guided"}}'
        )


def test_reference_guided_accepts_top_level_reference() -> None:
    resolution = resolve_analysis_config(
        ConfigParser().parse(
            '{"samples":["sample-a"],"reference":"ref",'
            '"alignment":{"mode":"compute","construction":"reference_guided"}}'
        )
    )

    assert resolution.config.alignment.construction == "reference_guided"
    assert resolution.config.reference == "ref"
