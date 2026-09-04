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


def test_clade_detection_defaults_to_disabled_with_explicit_method_and_threshold() -> None:
    resolved = _resolve({})

    assert resolved.clade_detection.enabled is False
    assert resolved.clade_detection.method == "max_pairwise_distance"
    assert resolved.clade_detection.max_within_clade_distance is None


def test_clade_detection_resolved_settings_round_trip_explicitly() -> None:
    resolved = _resolve({"clade_detection": {}})
    payload = resolved.model_dump(mode="json")

    restored = ResolvedAnalysisConfig.model_validate(payload)

    assert payload["clade_detection"] == {
        "enabled": False,
        "method": "max_pairwise_distance",
        "max_within_clade_distance": None,
    }
    assert restored.clade_detection.method == "max_pairwise_distance"


def test_clade_detection_rejects_unknown_method_value() -> None:
    with pytest.raises(ConfigSchemaValidationError):
        _resolve({"clade_detection": {"method": "diameter_cut"}})


def test_clade_detection_rejects_unknown_fields() -> None:
    with pytest.raises(ConfigSchemaValidationError):
        _resolve({"clade_detection": {"enabled": False, "unexpected": 1}})


@pytest.mark.parametrize("threshold", (-0.1, 1.1, float("nan"), float("inf"), True, "0.1"))
def test_clade_detection_rejects_invalid_threshold_values(threshold: object) -> None:
    with pytest.raises(ConfigSchemaValidationError):
        _resolve({"clade_detection": {"max_within_clade_distance": threshold}})


def test_enabled_clade_detection_requires_threshold() -> None:
    with pytest.raises(ConfigSchemaValidationError) as error_info:
        _resolve({"clade_detection": {"enabled": True}})

    error = error_info.value
    assert error.code is AnalysisConfigValidationCode.CLADE_DETECTION_THRESHOLD_REQUIRED
    assert error.field_path == "clade_detection.max_within_clade_distance"


def test_enabled_clade_detection_requires_enabled_distance_matrix() -> None:
    with pytest.raises(ConfigSchemaValidationError) as error_info:
        _resolve(
            {
                "distance_matrix": {"enabled": False},
                "phylogenetic_tree": {"enabled": False},
                "clade_detection": {
                    "enabled": True,
                    "max_within_clade_distance": 0.25,
                },
            }
        )

    error = error_info.value
    assert (
        error.code
        is AnalysisConfigValidationCode.CLADE_DETECTION_REQUIRES_DISTANCE_MATRIX
    )
    assert error.field_path == "clade_detection.enabled"


def test_enabled_clade_detection_requires_enabled_phylogenetic_tree() -> None:
    with pytest.raises(ConfigSchemaValidationError) as error_info:
        _resolve(
            {
                "phylogenetic_tree": {"enabled": False},
                "clade_detection": {
                    "enabled": True,
                    "max_within_clade_distance": 0.25,
                },
            }
        )

    error = error_info.value
    assert (
        error.code
        is AnalysisConfigValidationCode.CLADE_DETECTION_REQUIRES_PHYLOGENETIC_TREE
    )
    assert error.field_path == "clade_detection.enabled"


def test_disabled_clade_detection_keeps_optional_threshold_value() -> None:
    resolved = _resolve(
        {
            "distance_matrix": {"enabled": False},
            "phylogenetic_tree": {"enabled": False},
            "clade_detection": {
                "enabled": False,
                "max_within_clade_distance": 0.25,
            },
        }
    )

    assert resolved.clade_detection.enabled is False
    assert resolved.clade_detection.max_within_clade_distance == pytest.approx(0.25)


def test_cli_dot_notation_overrides_clade_detection_section() -> None:
    base = AnalysisConfigInput(samples=["sample-a.fa", "sample-b.fa"])
    overridden = apply_config_overrides(
        base_config=base,
        overrides=parse_cli_overrides(
            (
                "--clade_detection.enabled=true",
                "--clade_detection.method=max_pairwise_distance",
                "--clade_detection.max_within_clade_distance=0.1",
            )
        ),
    )
    resolved = resolve_analysis_config(overridden).config

    assert resolved.clade_detection.enabled is True
    assert resolved.clade_detection.method == "max_pairwise_distance"
    assert resolved.clade_detection.max_within_clade_distance == pytest.approx(0.1)
