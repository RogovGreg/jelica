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


def test_phylogenetic_tree_defaults_to_enabled_neighbor_joining_midpoint() -> None:
    resolved = _resolve({})

    assert resolved.phylogenetic_tree.enabled is True
    assert resolved.phylogenetic_tree.method == "neighbor_joining"
    assert resolved.phylogenetic_tree.rooting == "midpoint"


def test_phylogenetic_tree_resolved_settings_round_trip_explicitly() -> None:
    resolved = _resolve({"phylogenetic_tree": {}})
    payload = resolved.model_dump(mode="json")

    restored = ResolvedAnalysisConfig.model_validate(payload)

    assert payload["phylogenetic_tree"] == {
        "enabled": True,
        "method": "neighbor_joining",
        "rooting": "midpoint",
    }
    assert restored.phylogenetic_tree.method == "neighbor_joining"
    assert restored.phylogenetic_tree.rooting == "midpoint"


def test_phylogenetic_tree_rejects_unknown_method_value() -> None:
    with pytest.raises(ConfigSchemaValidationError):
        _resolve({"phylogenetic_tree": {"method": "upgma"}})


def test_phylogenetic_tree_rejects_unknown_rooting_value() -> None:
    with pytest.raises(ConfigSchemaValidationError):
        _resolve({"phylogenetic_tree": {"rooting": "outgroup"}})


def test_phylogenetic_tree_rejects_unknown_fields() -> None:
    with pytest.raises(ConfigSchemaValidationError):
        _resolve({"phylogenetic_tree": {"enabled": True, "unexpected": 1}})


def test_enabled_phylogenetic_tree_requires_enabled_distance_matrix() -> None:
    with pytest.raises(ConfigSchemaValidationError) as error_info:
        _resolve(
            {
                "distance_matrix": {"enabled": False},
                "phylogenetic_tree": {"enabled": True},
            }
        )

    error = error_info.value
    assert (
        error.code
        is AnalysisConfigValidationCode.PHYLOGENETIC_TREE_REQUIRES_DISTANCE_MATRIX
    )
    assert error.field_path == "phylogenetic_tree.enabled"


def test_disabled_phylogenetic_tree_allows_disabled_distance_matrix() -> None:
    resolved = _resolve(
        {
            "distance_matrix": {"enabled": False},
            "phylogenetic_tree": {"enabled": False},
        }
    )

    assert resolved.distance_matrix.enabled is False
    assert resolved.phylogenetic_tree.enabled is False


def test_cli_dot_notation_overrides_phylogenetic_tree_section() -> None:
    base = AnalysisConfigInput(samples=["sample-a.fa", "sample-b.fa"])
    overridden = apply_config_overrides(
        base_config=base,
        overrides=parse_cli_overrides(
            (
                "--phylogenetic_tree.enabled=false",
                "--phylogenetic_tree.method=neighbor_joining",
                "--phylogenetic_tree.rooting=midpoint",
            )
        ),
    )
    resolved = resolve_analysis_config(overridden).config

    assert resolved.phylogenetic_tree.enabled is False
    assert resolved.phylogenetic_tree.method == "neighbor_joining"
    assert resolved.phylogenetic_tree.rooting == "midpoint"
