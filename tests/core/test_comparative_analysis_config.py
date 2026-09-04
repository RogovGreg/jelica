from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from jelica_core.config import (
    AnalysisConfigInput,
    AnalysisConfigValidationCode,
    ConfigParser,
    ConfigSchemaValidationError,
    ResolvedAnalysisConfig,
    ResolvedComparativePairwiseConfig,
    apply_config_overrides,
    parse_cli_overrides,
    resolve_analysis_config,
)


def _resolve(document: dict[str, object]) -> ResolvedAnalysisConfig:
    config_document: dict[str, object] = {
        "samples": ["input-a", "input-b"],
        **document,
    }
    parsed = ConfigParser().parse(json.dumps(config_document))
    return resolve_analysis_config(parsed).config


def _statistics_only(
    *,
    pairwise: dict[str, object] | None = None,
    reference: dict[str, object] | None = None,
) -> dict[str, object]:
    comparative: dict[str, object] = {
        "enabled": True,
        "statistics": {"enabled": True},
        "sequence_differences": {"enabled": False},
    }
    if pairwise is not None:
        comparative["pairwise"] = pairwise
    if reference is not None:
        comparative["reference"] = reference
    return comparative


def _assert_semantic_error(
    document: dict[str, object],
    *,
    code: AnalysisConfigValidationCode,
    field_path: str,
) -> None:
    with pytest.raises(ConfigSchemaValidationError) as error_info:
        _resolve(document)

    error = error_info.value
    assert error.code is code
    assert error.field_path == field_path


def test_missing_comparative_analysis_defaults_to_explicit_enabled_config() -> None:
    parsed = ConfigParser().parse('{"samples":["input-a"]}')
    resolved = resolve_analysis_config(parsed).config
    explicitly_enabled = _resolve(
        {"comparative_analysis": {"enabled": True}}
    ).comparative_analysis

    assert parsed.comparative_analysis is None
    assert resolved.comparative_analysis.enabled is True
    assert resolved.comparative_analysis == explicitly_enabled
    assert (
        ResolvedAnalysisConfig.model_validate_json(resolved.model_dump_json())
        .comparative_analysis
        == explicitly_enabled
    )


def test_legacy_resolved_config_without_comparative_block_still_loads() -> None:
    payload = _resolve({}).model_dump(mode="json")
    del payload["comparative_analysis"]

    restored = ResolvedAnalysisConfig.model_validate(payload)

    assert restored.comparative_analysis.enabled is True


def test_explicitly_disabled_comparative_analysis_remains_disabled() -> None:
    resolved = _resolve({"comparative_analysis": {"enabled": False}})

    assert resolved.comparative_analysis.enabled is False


def test_cli_override_can_disable_default_comparative_analysis() -> None:
    base_config = ConfigParser().parse('{"samples":["input-a"]}')
    overrides = parse_cli_overrides(
        ("--comparative_analysis.enabled=false",)
    )

    overridden = apply_config_overrides(
        base_config=base_config,
        overrides=overrides,
    )
    resolved = resolve_analysis_config(overridden).config

    assert resolved.comparative_analysis.enabled is False


def test_enabled_comparative_analysis_rejects_no_enabled_analytics() -> None:
    _assert_semantic_error(
        {
            "comparative_analysis": {
                "enabled": True,
                "statistics": {"enabled": False},
                "sequence_differences": {"enabled": False},
            }
        },
        code=AnalysisConfigValidationCode.COMPARATIVE_ANALYSIS_EMPTY,
        field_path="comparative_analysis",
    )


def test_enabled_sequence_differences_rejects_no_enabled_category() -> None:
    _assert_semantic_error(
        {
            "comparative_analysis": {
                "enabled": True,
                "statistics": {"enabled": False},
                "sequence_differences": {
                    "enabled": True,
                    "substitutions": False,
                    "insertions": False,
                    "deletions": False,
                },
            }
        },
        code=AnalysisConfigValidationCode.SEQUENCE_DIFFERENCES_EMPTY,
        field_path="comparative_analysis.sequence_differences",
    )


def test_none_alignment_rejects_sequence_differences() -> None:
    _assert_semantic_error(
        {
            "alignment": {"mode": "none"},
            "comparative_analysis": {
                "enabled": True,
                "statistics": {"enabled": False},
                "sequence_differences": {"enabled": True},
            },
        },
        code=AnalysisConfigValidationCode.SEQUENCE_DIFFERENCES_REQUIRES_ALIGNMENT,
        field_path="comparative_analysis.sequence_differences.enabled",
    )


def test_none_alignment_accepts_statistics_only() -> None:
    resolved = _resolve(
        {
            "alignment": {"mode": "none"},
            "comparative_analysis": _statistics_only(),
            "distance_matrix": {"enabled": False},
            "phylogenetic_tree": {"enabled": False},
        }
    )

    assert resolved.alignment.mode == "none"
    assert resolved.comparative_analysis.statistics.enabled is True
    assert resolved.comparative_analysis.sequence_differences.enabled is False


@pytest.mark.parametrize("mode", ("auto", "enabled", "disabled"))
def test_supported_reference_modes_are_accepted(mode: str) -> None:
    resolved = _resolve(
        {
            "reference": "record-a",
            "comparative_analysis": _statistics_only(reference={"mode": mode}),
        }
    )

    assert resolved.comparative_analysis.reference.mode == mode


def test_unknown_reference_mode_is_rejected() -> None:
    with pytest.raises(ConfigSchemaValidationError):
        _resolve(
            {
                "comparative_analysis": _statistics_only(
                    reference={"mode": "required"}
                )
            }
        )


def test_uracil_thymine_equivalent_defaults_to_false() -> None:
    resolved = _resolve(
        {
            "comparative_analysis": {
                "enabled": True,
                "statistics": {"enabled": False},
                "sequence_differences": {"enabled": True},
            }
        }
    )

    policy = resolved.comparative_analysis.sequence_differences.symbol_policy
    assert policy.uracil_thymine_equivalent is False


def test_enabled_pairwise_without_scope_normalizes_to_all() -> None:
    resolved = _resolve(
        {
            "comparative_analysis": _statistics_only(
                pairwise={"enabled": True}
            )
        }
    )

    pairwise = resolved.comparative_analysis.pairwise
    assert pairwise.enabled is True
    assert pairwise.all is True
    assert pairwise.pairs_orientation == "directed"
    assert pairwise.groups == []
    assert pairwise.pairs == []


def test_enabled_pairwise_rejects_explicit_false_all_without_scope() -> None:
    _assert_semantic_error(
        {
            "comparative_analysis": _statistics_only(
                pairwise={"enabled": True, "all": False}
            )
        },
        code=AnalysisConfigValidationCode.PAIRWISE_SELECTION_EMPTY,
        field_path="comparative_analysis.pairwise",
    )


def test_pairwise_all_rejects_explicit_group() -> None:
    _assert_semantic_error(
        {
            "comparative_analysis": _statistics_only(
                pairwise={
                    "enabled": True,
                    "all": True,
                    "groups": [["record-a", "record-b"]],
                }
            )
        },
        code=AnalysisConfigValidationCode.PAIRWISE_ALL_WITH_EXPLICIT_SELECTION,
        field_path="comparative_analysis.pairwise.all",
    )


def test_pairwise_all_rejects_explicit_pair() -> None:
    _assert_semantic_error(
        {
            "comparative_analysis": _statistics_only(
                pairwise={
                    "enabled": True,
                    "all": True,
                    "pairs": [["record-a", "record-b"]],
                }
            )
        },
        code=AnalysisConfigValidationCode.PAIRWISE_ALL_WITH_EXPLICIT_SELECTION,
        field_path="comparative_analysis.pairwise.all",
    )


@pytest.mark.parametrize("orientation", ("directed", "bidirectional"))
def test_supported_pairwise_orientations_are_accepted(orientation: str) -> None:
    resolved = _resolve(
        {
            "comparative_analysis": _statistics_only(
                pairwise={
                    "enabled": True,
                    "all": False,
                    "pairs_orientation": orientation,
                    "pairs": [["record-a", "record-b"]],
                }
            )
        }
    )

    assert resolved.comparative_analysis.pairwise.pairs_orientation == orientation


def test_unknown_pairwise_orientation_is_rejected() -> None:
    with pytest.raises(ConfigSchemaValidationError):
        _resolve(
            {
                "comparative_analysis": _statistics_only(
                    pairwise={
                        "enabled": True,
                        "pairs_orientation": "unordered",
                        "pairs": [["record-a", "record-b"]],
                    }
                )
            }
        )


def test_pairwise_self_pair_is_rejected() -> None:
    _assert_semantic_error(
        {
            "comparative_analysis": _statistics_only(
                pairwise={
                    "enabled": True,
                    "all": False,
                    "pairs": [["record-a", "record-a"]],
                }
            )
        },
        code=AnalysisConfigValidationCode.PAIRWISE_SELF_PAIR,
        field_path="comparative_analysis.pairwise.pairs[0]",
    )


def test_pairwise_pair_requires_exactly_two_selectors() -> None:
    _assert_semantic_error(
        {
            "comparative_analysis": _statistics_only(
                pairwise={
                    "enabled": True,
                    "all": False,
                    "pairs": [["record-a"]],
                }
            )
        },
        code=AnalysisConfigValidationCode.PAIRWISE_PAIR_INVALID,
        field_path="comparative_analysis.pairwise.pairs[0]",
    )


def test_pairwise_selector_must_not_be_empty_after_trimming() -> None:
    _assert_semantic_error(
        {
            "comparative_analysis": _statistics_only(
                pairwise={
                    "enabled": True,
                    "all": False,
                    "pairs": [["record-a", "   "]],
                }
            )
        },
        code=AnalysisConfigValidationCode.PAIRWISE_SELECTOR_INVALID,
        field_path="comparative_analysis.pairwise.pairs[0][1]",
    )


def test_duplicate_pairwise_pairs_are_preserved() -> None:
    pairs = [["record-a", "record-b"], ["record-a", "record-b"]]
    resolved = _resolve(
        {
            "comparative_analysis": _statistics_only(
                pairwise={"enabled": True, "all": False, "pairs": pairs}
            )
        }
    )

    assert resolved.comparative_analysis.pairwise.pairs == pairs


def test_opposite_pairwise_pairs_are_accepted_together() -> None:
    pairs = [["record-a", "record-b"], ["record-b", "record-a"]]
    resolved = _resolve(
        {
            "comparative_analysis": _statistics_only(
                pairwise={"enabled": True, "all": False, "pairs": pairs}
            )
        }
    )

    assert resolved.comparative_analysis.pairwise.pairs == pairs


def test_pairwise_group_with_duplicates_and_two_unique_members_is_accepted() -> None:
    groups = [["record-a", "record-a", "record-b", "record-c"]]
    resolved = _resolve(
        {
            "comparative_analysis": _statistics_only(
                pairwise={"enabled": True, "all": False, "groups": groups}
            )
        }
    )

    assert resolved.comparative_analysis.pairwise.groups == groups


def test_pairwise_group_with_fewer_than_two_unique_members_is_rejected() -> None:
    _assert_semantic_error(
        {
            "comparative_analysis": _statistics_only(
                pairwise={
                    "enabled": True,
                    "all": False,
                    "groups": [["record-a", "record-a"]],
                }
            )
        },
        code=AnalysisConfigValidationCode.PAIRWISE_GROUP_TOO_SMALL,
        field_path="comparative_analysis.pairwise.groups[0]",
    )


def test_disabled_pairwise_normalizes_inactive_selection_to_canonical_form() -> None:
    resolved = _resolve(
        {
            "comparative_analysis": _statistics_only(
                pairwise={
                    "enabled": False,
                    "all": True,
                    "pairs_orientation": "bidirectional",
                    "groups": [["record-a", "record-b"]],
                }
            )
        }
    )

    assert resolved.comparative_analysis.pairwise.model_dump(mode="json") == {
        "enabled": False,
        "all": False,
        "pairs_orientation": "directed",
        "groups": [],
        "pairs": [],
    }


def test_pairwise_selectors_are_trimmed_without_deduplication() -> None:
    resolved = _resolve(
        {
            "comparative_analysis": _statistics_only(
                pairwise={
                    "enabled": True,
                    "all": False,
                    "pairs": [[" record-a ", " data/input-b.fasta::record-b "]],
                }
            )
        }
    )

    assert resolved.comparative_analysis.pairwise.pairs == [
        ["record-a", "data/input-b.fasta::record-b"]
    ]


@pytest.mark.parametrize(
    "payload",
    (
        {"enabled": True, "all": False, "pairs": [["record-a"]]},
        {"enabled": True, "all": False, "pairs": [["record-a", "record-a"]]},
        {"enabled": True, "all": False, "groups": [["record-a", "record-a"]]},
    ),
)
def test_resolved_pairwise_contract_revalidates_selection_structure(
    payload: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        ResolvedComparativePairwiseConfig.model_validate(payload)


def test_enabled_reference_mode_requires_top_level_reference_selector() -> None:
    _assert_semantic_error(
        {
            "comparative_analysis": _statistics_only(
                reference={"mode": "enabled"}
            )
        },
        code=AnalysisConfigValidationCode.COMPARATIVE_REFERENCE_REQUIRED,
        field_path="comparative_analysis.reference.mode",
    )


def test_resolved_comparative_analysis_json_round_trip_is_canonical() -> None:
    groups = [["record-a", "record-b", "record-c"]]
    pairs = [["record-a", "data/input-d.fasta::record-d"]]
    resolved = _resolve(
        {
            "alignment": {"mode": "prealigned"},
            "reference": "record-a",
            "comparative_analysis": {
                "enabled": True,
                "statistics": {"enabled": True},
                "sequence_differences": {
                    "enabled": True,
                    "substitutions": True,
                    "insertions": True,
                    "deletions": True,
                    "symbol_policy": {"uracil_thymine_equivalent": False},
                },
                "reference": {"mode": "enabled"},
                "pairwise": {
                    "enabled": True,
                    "all": False,
                    "pairs_orientation": "directed",
                    "groups": groups,
                    "pairs": pairs,
                },
            },
        }
    )

    payload = resolved.model_dump(mode="json")
    encoded = json.dumps(payload, ensure_ascii=False)
    restored = ResolvedAnalysisConfig.model_validate(json.loads(encoded))

    assert payload["comparative_analysis"] == {
        "enabled": True,
        "statistics": {"enabled": True},
        "sequence_differences": {
            "enabled": True,
            "substitutions": True,
            "insertions": True,
            "deletions": True,
            "symbol_policy": {"uracil_thymine_equivalent": False},
        },
        "reference": {"mode": "enabled"},
        "pairwise": {
            "enabled": True,
            "all": False,
            "pairs_orientation": "directed",
            "groups": groups,
            "pairs": pairs,
        },
    }
    assert restored.model_dump(mode="json") == payload


def test_override_validation_error_does_not_expose_sample_content() -> None:
    synthetic_sequence = "ACGTAC"
    base_config = AnalysisConfigInput(samples=[synthetic_sequence])
    overrides = parse_cli_overrides(
        ("--alignment.construction=reference_guided",)
    )

    with pytest.raises(ConfigSchemaValidationError) as error_info:
        apply_config_overrides(base_config=base_config, overrides=overrides)

    assert synthetic_sequence not in str(error_info.value)
