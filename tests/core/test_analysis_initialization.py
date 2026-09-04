from __future__ import annotations

import json
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import ValidationError

from jelica_core.analysis import (
    InitializeAnalysisTaskRequest,
    initialize_analysis_task,
)
from jelica_core.config import (
    AnalysisConfigInput,
    ConfigSchemaValidationError,
    MissingSamplesError,
    parse_cli_overrides,
    resolve_analysis_config,
)
from jelica_core.system_config import CoreConfigService, CoreNotInitializedError
from jelica_core.tasks import InitializedAnalysisTask


def _initialize_task(
    jelica_home: Path,
    *,
    trace_id: UUID | None = None,
    config_json: str | None = None,
    raw_overrides: tuple[str, ...] = (),
    positional_sources: tuple[str, ...] = (),
    initialize_core: bool = True,
) -> InitializedAnalysisTask:
    config_service = CoreConfigService(jelica_home=jelica_home)
    if initialize_core:
        config_service.initialize_system_config(force=True)

    request = InitializeAnalysisTaskRequest(
        trace_id=trace_id,
        config_json=config_json,
        overrides=tuple(parse_cli_overrides(raw_overrides)),
        positional_sources=positional_sources,
    )
    return initialize_analysis_task(
        request=request,
        core_config_service=config_service,
    )


def test_initialize_from_json_config_samples_only(tmp_path: Path) -> None:
    task = _initialize_task(
        tmp_path,
        config_json='{"samples":["json-a","json-b"]}',
    )

    assert task.config.samples == ["json-a", "json-b"]


def test_analysis_config_input_accepts_strings_and_none_sources() -> None:
    config_input = AnalysisConfigInput(samples=["A.fasta", None, "B.fasta"])

    assert config_input.samples == ["A.fasta", None, "B.fasta"]


def test_analysis_config_input_preserves_none_order_in_samples() -> None:
    config_input = AnalysisConfigInput(samples=[None, "A.fasta", None, "B.fasta"])

    assert config_input.samples == [None, "A.fasta", None, "B.fasta"]


def test_analysis_config_input_trims_sources_around_none_values() -> None:
    config_input = AnalysisConfigInput(samples=["  A.fasta  ", None, " B.fasta "])

    assert config_input.samples == ["A.fasta", None, "B.fasta"]


def test_analysis_config_input_rejects_empty_string_sample() -> None:
    with pytest.raises(ValidationError):
        AnalysisConfigInput(samples=["A.fasta", ""])


def test_analysis_config_input_rejects_whitespace_only_sample() -> None:
    with pytest.raises(ValidationError):
        AnalysisConfigInput(samples=["A.fasta", "   "])


def test_analysis_config_input_rejects_numeric_sample_item() -> None:
    with pytest.raises(ValidationError):
        AnalysisConfigInput(samples=["A.fasta", 1])  # type: ignore[list-item]


def test_analysis_config_input_rejects_object_sample_item() -> None:
    with pytest.raises(ValidationError):
        AnalysisConfigInput(samples=["A.fasta", {"path": "B.fasta"}])  # type: ignore[list-item]


def test_analysis_config_input_does_not_mutate_source_list() -> None:
    source_samples: list[str | None] = ["  A.fasta  ", None, "B.fasta"]
    source_snapshot = list(source_samples)

    config_input = AnalysisConfigInput(samples=source_samples)

    assert source_samples == source_snapshot
    assert config_input.samples == ["A.fasta", None, "B.fasta"]


def test_initialize_from_positional_sources_only(tmp_path: Path) -> None:
    task = _initialize_task(
        tmp_path,
        positional_sources=("pos-a.fasta", "pos-b.fasta"),
    )

    assert task.config.samples == ["pos-a.fasta", "pos-b.fasta"]


def test_initialize_from_dynamic_samples_only(tmp_path: Path) -> None:
    task = _initialize_task(
        tmp_path,
        raw_overrides=('--samples=["dyn-a","dyn-b"]',),
    )

    assert task.config.samples == ["dyn-a", "dyn-b"]


def test_positional_sources_replace_json_samples(tmp_path: Path) -> None:
    task = _initialize_task(
        tmp_path,
        config_json='{"samples":["json-a"]}',
        positional_sources=("pos-a.fasta",),
    )

    assert task.config.samples == ["pos-a.fasta"]


def test_positional_sources_replace_dynamic_samples(tmp_path: Path) -> None:
    task = _initialize_task(
        tmp_path,
        raw_overrides=('--samples=["dyn-a"]',),
        positional_sources=("pos-a.fasta", "pos-b.fasta"),
    )

    assert task.config.samples == ["pos-a.fasta", "pos-b.fasta"]


def test_dynamic_samples_replace_json_samples(tmp_path: Path) -> None:
    task = _initialize_task(
        tmp_path,
        config_json='{"samples":["json-a"]}',
        raw_overrides=('--samples=["dyn-a"]',),
    )

    assert task.config.samples == ["dyn-a"]


def test_resolved_config_keeps_sparse_samples_with_none_items() -> None:
    resolution = resolve_analysis_config(AnalysisConfigInput(samples=["A.fasta", None, "B.fasta"]))

    assert resolution.config.samples == ["A.fasta", None, "B.fasta"]


def test_unknown_fields_generate_stable_warnings(tmp_path: Path) -> None:
    task = _initialize_task(
        tmp_path,
        config_json='{"samples":["sample-a"],"zeta":1}',
        raw_overrides=("--alpha=true",),
    )

    assert task.warnings == (
        "Ignoring unknown analysis config field 'alpha'.",
        "Ignoring unknown analysis config field 'zeta'.",
    )


def test_unknown_fields_are_absent_from_resolved_config(
    tmp_path: Path,
    default_resolved_alignment_block: dict[str, object],
    default_resolved_comparative_analysis_block: dict[str, object],
    default_resolved_distance_matrix_block: dict[str, object],
    default_resolved_phylogenetic_tree_block: dict[str, object],
    default_resolved_clade_detection_block: dict[str, object],
) -> None:
    task = _initialize_task(
        tmp_path,
        config_json='{"samples":["sample-a"],"unknown":1}',
    )

    resolved_config = task.config.model_dump(mode="json")
    trace_id = resolved_config.pop("trace_id")
    assert UUID(str(trace_id)).version == 4
    assert resolved_config == {
        "alignment": default_resolved_alignment_block,
        "comparative_analysis": default_resolved_comparative_analysis_block,
        "distance_matrix": default_resolved_distance_matrix_block,
        "execution": {"from_phase": "auto", "target": "full_analysis"},
        "phylogenetic_tree": default_resolved_phylogenetic_tree_block,
        "clade_detection": default_resolved_clade_detection_block,
        "priority": 1,
        "reference": None,
        "schema_version": 1,
        "samples": ["sample-a"],
        "statistics": {"kmer_strand": "forward", "kmers": []},
    }
    assert "unknown" not in resolved_config


def test_initialize_uses_default_priority_when_not_provided(tmp_path: Path) -> None:
    task = _initialize_task(tmp_path, positional_sources=("sample-a.fasta",))

    assert task.config.priority == 1


def test_initialize_persists_default_execution_selection(tmp_path: Path) -> None:
    task = _initialize_task(tmp_path, positional_sources=("sample-a.fasta",))

    assert task.config.execution.target == "full_analysis"
    assert task.config.execution.from_phase == "auto"
    saved_config = json.loads(task.config_path.read_text(encoding="utf-8"))
    assert saved_config["execution"] == {
        "target": "full_analysis",
        "from_phase": "auto",
    }


def test_execution_cli_override_wins_and_is_normalized(tmp_path: Path) -> None:
    task = _initialize_task(
        tmp_path,
        config_json=('{"samples":["sample-a.fasta"],"execution":{"target":"alignment"}}'),
        raw_overrides=("--execution.target=VALIDATION",),
    )

    assert task.config.execution.target == "validation"
    assert task.config.execution.from_phase == "auto"


def test_initialize_uses_explicit_priority_from_config_json(tmp_path: Path) -> None:
    task = _initialize_task(
        tmp_path,
        config_json='{"samples":["sample-a.fasta"],"priority":4}',
    )

    assert task.config.priority == 4


def test_initialize_uses_priority_from_cli_override(tmp_path: Path) -> None:
    task = _initialize_task(
        tmp_path,
        positional_sources=("sample-a.fasta",),
        raw_overrides=("--priority=7",),
    )

    assert task.config.priority == 7


def test_initialize_rejects_priority_below_minimum(tmp_path: Path) -> None:
    with pytest.raises(ConfigSchemaValidationError):
        _initialize_task(
            tmp_path,
            config_json='{"samples":["sample-a.fasta"],"priority":0}',
        )


def test_initialize_fails_when_sources_are_missing(tmp_path: Path) -> None:
    with pytest.raises(MissingSamplesError):
        _initialize_task(tmp_path)


def test_initialize_fails_when_core_is_not_initialized(tmp_path: Path) -> None:
    with pytest.raises(CoreNotInitializedError):
        _initialize_task(
            tmp_path,
            positional_sources=("sample-a.fasta",),
            initialize_core=False,
        )


def test_initialize_fails_for_explicit_empty_samples_list(tmp_path: Path) -> None:
    with pytest.raises(MissingSamplesError):
        _initialize_task(tmp_path, config_json='{"samples":[]}')


def test_initialize_fails_when_samples_contain_only_null(tmp_path: Path) -> None:
    with pytest.raises(MissingSamplesError):
        _initialize_task(tmp_path, config_json='{"samples":[null]}')


def test_initialize_fails_when_samples_contain_only_null_values(tmp_path: Path) -> None:
    with pytest.raises(MissingSamplesError):
        _initialize_task(tmp_path, config_json='{"samples":[null,null]}')


def test_initialize_accepts_null_and_real_sample(tmp_path: Path) -> None:
    task = _initialize_task(tmp_path, config_json='{"samples":[null,"A.fasta"]}')

    assert task.config.samples == [None, "A.fasta"]


def test_initialize_accepts_sparse_samples_with_two_real_sources(tmp_path: Path) -> None:
    task = _initialize_task(tmp_path, config_json='{"samples":["A.fasta",null,"B.fasta"]}')

    assert task.config.samples == ["A.fasta", None, "B.fasta"]


def test_positional_sources_replace_sparse_json_samples(tmp_path: Path) -> None:
    task = _initialize_task(
        tmp_path,
        config_json='{"samples":["json-a",null,"json-b"]}',
        positional_sources=("pos-a.fasta",),
    )

    assert task.config.samples == ["pos-a.fasta"]


def test_positional_sources_replace_sparse_dynamic_samples(tmp_path: Path) -> None:
    task = _initialize_task(
        tmp_path,
        raw_overrides=('--samples=["dyn-a"]', "--samples.2=dyn-b"),
        positional_sources=("pos-a.fasta", "pos-b.fasta"),
    )

    assert task.config.samples == ["pos-a.fasta", "pos-b.fasta"]


def test_resolved_config_serialization_is_json_compatible(
    tmp_path: Path,
    default_resolved_alignment_block: dict[str, object],
    default_resolved_comparative_analysis_block: dict[str, object],
    default_resolved_distance_matrix_block: dict[str, object],
    default_resolved_phylogenetic_tree_block: dict[str, object],
    default_resolved_clade_detection_block: dict[str, object],
) -> None:
    task = _initialize_task(tmp_path, positional_sources=("sample-a.fasta",))

    serialized = task.config.model_dump(mode="json")
    json.dumps(serialized)
    trace_id = serialized.pop("trace_id")
    assert UUID(str(trace_id)).version == 4
    assert serialized == {
        "alignment": default_resolved_alignment_block,
        "comparative_analysis": default_resolved_comparative_analysis_block,
        "distance_matrix": default_resolved_distance_matrix_block,
        "execution": {"from_phase": "auto", "target": "full_analysis"},
        "phylogenetic_tree": default_resolved_phylogenetic_tree_block,
        "clade_detection": default_resolved_clade_detection_block,
        "priority": 1,
        "reference": None,
        "schema_version": 1,
        "samples": ["sample-a.fasta"],
        "statistics": {"kmer_strand": "forward", "kmers": []},
    }


def test_initialize_uses_alignment_mode_default_compute(tmp_path: Path) -> None:
    task = _initialize_task(tmp_path, positional_sources=("sample-a.fasta",))

    assert task.config.alignment.mode == "compute"


def test_initialize_pins_alignment_mode_in_saved_config_revision(
    tmp_path: Path,
    default_resolved_alignment_block: dict[str, object],
) -> None:
    task = _initialize_task(tmp_path, positional_sources=("sample-a.fasta",))
    saved_config = json.loads(task.config_path.read_text(encoding="utf-8"))

    assert saved_config["alignment"] == default_resolved_alignment_block


def test_initialize_applies_explicit_alignment_mode_override(tmp_path: Path) -> None:
    task = _initialize_task(
        tmp_path,
        positional_sources=("sample-a.fasta",),
        raw_overrides=("--alignment.mode=prealigned",),
    )

    assert task.config.alignment.mode == "prealigned"


def test_explicit_alignment_mode_has_priority_over_system_default(tmp_path: Path) -> None:
    config_service = CoreConfigService(jelica_home=tmp_path)
    config_service.initialize_system_config(force=True)
    config_service.set_parameter(parameter="default_alignment_mode", value="none")

    request = InitializeAnalysisTaskRequest(
        config_json=None,
        overrides=tuple(parse_cli_overrides(("--alignment.mode=prealigned",))),
        positional_sources=("sample-a.fasta",),
    )
    task = initialize_analysis_task(
        request=request,
        core_config_service=config_service,
    )

    assert task.config.alignment.mode == "prealigned"


@pytest.mark.parametrize(
    "reference_selector",
    ("NC_045512.2", "data/alignment.afa::NC_045512.2"),
)
def test_initialize_preserves_optional_reference_selector(
    tmp_path: Path, reference_selector: str
) -> None:
    task = _initialize_task(
        tmp_path,
        positional_sources=("sample-a.fasta",),
        raw_overrides=(f"--reference={reference_selector}",),
    )

    assert task.config.reference == reference_selector
    assert task.config.samples == ["sample-a.fasta"]


def test_initialize_defaults_statistics_fields(tmp_path: Path) -> None:
    task = _initialize_task(tmp_path, positional_sources=("sample-a.fasta",))

    assert task.config.statistics.kmers == []
    assert task.config.statistics.kmer_strand == "forward"


def test_initialize_rejects_invalid_kmer_strand(tmp_path: Path) -> None:
    with pytest.raises(ConfigSchemaValidationError):
        _initialize_task(
            tmp_path,
            positional_sources=("sample-a.fasta",),
            raw_overrides=("--statistics.kmer_strand=invalid",),
        )


def test_initialized_task_contains_uuid4_identifier(tmp_path: Path) -> None:
    task = _initialize_task(tmp_path, positional_sources=("sample-a.fasta",))

    parsed_uuid = UUID(task.task_id)
    assert parsed_uuid.version == 4


def test_initialize_generates_and_persists_trace_id(tmp_path: Path) -> None:
    task = _initialize_task(tmp_path, positional_sources=("sample-a.fasta",))

    assert task.config.trace_id is not None
    assert task.config.trace_id.version == 4
    saved_config = json.loads(task.config_path.read_text(encoding="utf-8"))
    revision_path = task.task_dir / task.current_config_relative_path
    saved_revision = json.loads(revision_path.read_text(encoding="utf-8"))
    assert saved_config["trace_id"] == str(task.config.trace_id)
    assert saved_revision["trace_id"] == str(task.config.trace_id)


def test_initialize_preserves_explicit_trace_id(tmp_path: Path) -> None:
    trace_id = UUID("8b1c9d4e-1c33-4ab9-81b6-21408cc92cc4")

    task = _initialize_task(
        tmp_path,
        trace_id=trace_id,
        positional_sources=("sample-a.fasta",),
    )

    assert task.config.trace_id == trace_id
    saved_config = json.loads(task.config_path.read_text(encoding="utf-8"))
    assert saved_config["trace_id"] == str(trace_id)


def test_initialize_uses_tasks_dir_from_system_config(tmp_path: Path) -> None:
    jelica_home = tmp_path / "home"
    custom_data_dir = tmp_path / "external-data"
    config_service = CoreConfigService(jelica_home=jelica_home)
    config_service.initialize_system_config(data_directory=str(custom_data_dir))

    request = InitializeAnalysisTaskRequest(positional_sources=("sample-a.fasta",))
    task = initialize_analysis_task(
        request=request,
        core_config_service=config_service,
    )

    assert task.task_dir.parent == custom_data_dir / "tasks"
