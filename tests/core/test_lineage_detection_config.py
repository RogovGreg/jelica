from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from jelica_core.analysis import InitializeAnalysisTaskRequest, initialize_analysis_task
from jelica_core.config import (
    AnalysisConfigInput,
    ConfigSchemaValidationError,
    LineageDetectionMethod,
    resolve_analysis_config,
)
from jelica_core.system_config import CoreConfigService


def _base_payload() -> dict[str, object]:
    return {
        "samples": ["sample.fa"],
        "alignment": {"mode": "none"},
        "comparative_analysis": {"enabled": False},
        "distance_matrix": {"enabled": False},
        "phylogenetic_tree": {"enabled": False},
        "clade_detection": {"enabled": False},
    }


def test_lineage_detection_defaults_to_disabled() -> None:
    config = resolve_analysis_config(AnalysisConfigInput.model_validate(_base_payload())).config

    assert config.lineage_detection.enabled is False
    assert config.lineage_detection.method is LineageDetectionMethod.NEXTCLADE_PANGO
    assert config.lineage_detection.dataset is None


def test_enabled_lineage_detection_requires_a_dataset() -> None:
    payload = _base_payload()
    payload["lineage_detection"] = {"enabled": True}

    with pytest.raises(
        ConfigSchemaValidationError,
        match="requires dataset when enabled",
    ):
        resolve_analysis_config(AnalysisConfigInput.model_validate(payload))


def test_lineage_detection_accepts_only_nextclade_pango() -> None:
    payload = _base_payload()
    payload["lineage_detection"] = {
        "enabled": True,
        "dataset": "/datasets/sars",
        "method": "pangolin",
    }

    with pytest.raises(ValidationError):
        AnalysisConfigInput.model_validate(payload)


def test_submission_binds_relative_lineage_dataset_path(tmp_path: Path) -> None:
    dataset = tmp_path / "dataset"
    dataset.mkdir()
    config_service = CoreConfigService(jelica_home=tmp_path / "home")
    config_service.initialize_system_config(force=True)
    task = initialize_analysis_task(
        request=InitializeAnalysisTaskRequest(
            positional_sources=("sample.fa",),
            config_json='{"lineage_detection":{"enabled":true,"dataset":"dataset"},'
            '"alignment":{"mode":"none"},"comparative_analysis":{"enabled":false},'
            '"distance_matrix":{"enabled":false},"phylogenetic_tree":{"enabled":false},'
            '"clade_detection":{"enabled":false}}',
            submission_base_dir=tmp_path,
        ),
        core_config_service=config_service,
    )

    assert task.config.lineage_detection.dataset == str(dataset)


@pytest.mark.parametrize("value", ["", "   "])
def test_lineage_detection_rejects_empty_dataset(value: str) -> None:
    payload = _base_payload()
    payload["lineage_detection"] = {"enabled": True, "dataset": value}

    with pytest.raises(ValidationError):
        AnalysisConfigInput.model_validate(payload)
