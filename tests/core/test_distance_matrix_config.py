from __future__ import annotations

import json

import pytest

from jelica_core.config import (
    AnalysisConfigInput,
    AnalysisConfigValidationCode,
    ConfigParser,
    ConfigSchemaValidationError,
    ResolvedAnalysisConfig,
    apply_config_overrides,
    parse_cli_overrides,
    resolve_analysis_config,
)


def _resolve(document: dict[str, object]) -> ResolvedAnalysisConfig:
    parsed = ConfigParser().parse(
        json.dumps({"samples": ["sample-a.fa", "sample-b.fa"], **document})
    )
    return resolve_analysis_config(parsed).config


def test_distance_matrix_defaults_to_enabled_with_explicit_p_distance_model() -> None:
    resolved = _resolve({})

    assert resolved.distance_matrix.enabled is True
    assert resolved.distance_matrix.model == "p_distance"


def test_distance_matrix_resolved_model_round_trips_as_explicit_p_distance() -> None:
    resolved = _resolve({"distance_matrix": {}})
    payload = resolved.model_dump(mode="json")

    restored = ResolvedAnalysisConfig.model_validate(payload)

    assert payload["distance_matrix"] == {"enabled": True, "model": "p_distance"}
    assert restored.distance_matrix.model == "p_distance"


def test_distance_matrix_rejects_unknown_model_value() -> None:
    with pytest.raises(ConfigSchemaValidationError):
        _resolve({"distance_matrix": {"model": "jukes_cantor"}})


def test_distance_matrix_rejects_unknown_fields() -> None:
    with pytest.raises(ConfigSchemaValidationError):
        _resolve({"distance_matrix": {"enabled": True, "unexpected": 1}})


def test_enabled_distance_matrix_rejects_alignment_none() -> None:
    with pytest.raises(ConfigSchemaValidationError) as error_info:
        _resolve(
            {
                "alignment": {"mode": "none"},
                "comparative_analysis": {"enabled": False},
                "distance_matrix": {"enabled": True},
            }
        )

    error = error_info.value
    assert error.code is AnalysisConfigValidationCode.DISTANCE_MATRIX_REQUIRES_ALIGNMENT
    assert error.field_path == "distance_matrix.enabled"


@pytest.mark.parametrize("alignment_mode", ("compute", "prealigned"))
def test_enabled_distance_matrix_accepts_alignment_modes_with_canonical_result(
    alignment_mode: str,
) -> None:
    resolved = _resolve(
        {
            "alignment": {"mode": alignment_mode},
            "distance_matrix": {"enabled": True},
        }
    )

    assert resolved.alignment.mode == alignment_mode
    assert resolved.distance_matrix.enabled is True


def test_cli_dot_notation_overrides_distance_matrix_section() -> None:
    base = AnalysisConfigInput(samples=["sample-a.fa", "sample-b.fa"])
    overridden = apply_config_overrides(
        base_config=base,
        overrides=parse_cli_overrides(
            (
                "--distance_matrix.enabled=false",
                "--distance_matrix.model=p_distance",
                "--phylogenetic_tree.enabled=false",
            )
        ),
    )
    resolved = resolve_analysis_config(overridden).config

    assert resolved.distance_matrix.model == "p_distance"
    assert resolved.distance_matrix.enabled is False
