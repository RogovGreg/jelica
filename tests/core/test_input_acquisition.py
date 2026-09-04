from __future__ import annotations

import json
import socket
from dataclasses import dataclass, field
from email.message import Message
from pathlib import Path
from typing import Any, Literal, cast
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse

import pytest

from jelica_core.runtime.input_acquisition import (
    INPUT_DIRECTORY_DEPTH_LIMIT_REACHED_EVENT,
    INPUT_DIRECTORY_EMPTY_EVENT,
    INPUT_DIRECTORY_NO_SUPPORTED_FILES_EVENT,
    INPUT_DUPLICATES_SKIPPED_EVENT,
    INPUT_FILE_EMPTY_EVENT,
    INPUT_FILE_TYPE_UNSUPPORTED_EVENT,
    INPUT_NO_DATA_ACQUIRED_EVENT,
    INPUT_PATH_NOT_FOUND_EVENT,
    INPUT_SYMLINK_UNSUPPORTED_EVENT,
    INPUT_SYMLINKS_SKIPPED_EVENT,
    INPUT_UNSUPPORTED_FILES_SKIPPED_EVENT,
    NCBI_PARTIAL_RESPONSE_EVENT,
    NCBI_RECORD_NOT_FOUND_EVENT,
    NCBI_REQUEST_FAILED_EVENT,
    HttpNCBINucleotideClient,
    InputAcquisitionError,
    InputAcquisitionStage,
)
from jelica_core.runtime.models import (
    DEFAULT_PIPELINE_NAME,
    DEFAULT_PIPELINE_VERSION,
    RuntimeStateCheckpoint,
    WorkerLaunchSpec,
)
from jelica_core.runtime.pipeline import StageContext
from jelica_core.runtime.progress import NullProgressReporter
from jelica_core.tasks.storage import compute_config_hash


@dataclass(slots=True)
class _FakeNcbiClient:
    responses: dict[tuple[str, ...], str] = field(default_factory=dict)
    calls: list[tuple[str, ...]] = field(default_factory=list)
    api_keys: list[str] = field(default_factory=list)

    def fetch_nucleotide_genbank(
        self,
        *,
        accessions: tuple[str, ...],
        api_key: str,
        max_retries: int,
        timeout_seconds: float,
    ) -> str:
        del max_retries, timeout_seconds
        self.calls.append(accessions)
        self.api_keys.append(api_key)
        return self.responses.get(accessions, "")


def _build_stage_context(
    tmp_path: Path,
    *,
    samples: list[str],
    input_directory_max_depth: int = 3,
    ncbi_max_retries: int = 3,
    ncbi_api_key: str = "",
    events: list[tuple[str, dict[str, object]]] | None = None,
) -> StageContext:
    task_dir = tmp_path / "task"
    job_dir = task_dir / "jobs" / "job-1"
    config_revision_path = task_dir / "configs" / "000001.json"
    config_revision_path.parent.mkdir(parents=True, exist_ok=True)
    config_document = {
        "schema_version": 1,
        "priority": 1,
        "samples": samples,
        "input_directory_max_depth": input_directory_max_depth,
        "ncbi_max_retries": ncbi_max_retries,
    }
    config_revision_path.write_text(
        json.dumps(config_document, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    launch_spec = WorkerLaunchSpec(
        task_id="task-1",
        job_id="job-1",
        worker_instance_id="worker-1",
        lease_token="lease-1",
        database_path=tmp_path / "jelica.db",
        task_dir=task_dir,
        job_dir=job_dir,
        config_revision_path=config_revision_path,
        config_hash=compute_config_hash(config_document),
        runtime_state_json=RuntimeStateCheckpoint.new(
            pipeline_version=DEFAULT_PIPELINE_VERSION
        ).to_runtime_state_json(),
        pipeline_name=DEFAULT_PIPELINE_NAME,
        pipeline_version=DEFAULT_PIPELINE_VERSION,
        ncbi_api_key=ncbi_api_key,
    )

    def _event_reporter(event_name: str, context: dict[str, object]) -> None:
        if events is None:
            return
        events.append((event_name, context))

    return StageContext(
        launch_spec=launch_spec,
        stage_index=1,
        stage_staging_directory=job_dir / "staging" / "input_acquisition" / "worker-1",
        event_reporter=_event_reporter if events is not None else None,
    )


def _run_stage(
    tmp_path: Path,
    *,
    samples: list[str],
    ncbi_client: _FakeNcbiClient | None = None,
    input_directory_max_depth: int = 3,
    ncbi_max_retries: int = 3,
    ncbi_api_key: str = "",
) -> tuple[StageContext, list[tuple[str, dict[str, object]]]]:
    events: list[tuple[str, dict[str, object]]] = []
    context = _build_stage_context(
        tmp_path,
        samples=samples,
        input_directory_max_depth=input_directory_max_depth,
        ncbi_max_retries=ncbi_max_retries,
        ncbi_api_key=ncbi_api_key,
        events=events,
    )
    stage = InputAcquisitionStage(ncbi_client=ncbi_client)
    stage.preflight(context)
    stage.run(context, NullProgressReporter())
    return context, events


def _manifest(context: StageContext) -> dict[str, object]:
    manifest_path = context.stage_staging_directory / "inputs" / "input_manifest.json"
    return json.loads(manifest_path.read_text(encoding="utf-8"))


def _manifest_materialized_files(manifest: dict[str, object]) -> list[dict[str, object]]:
    raw_value = manifest["materialized_files"]
    assert isinstance(raw_value, list)
    return [cast(dict[str, object], item) for item in raw_value]


def _manifest_sources(manifest: dict[str, object]) -> list[dict[str, object]]:
    raw_value = manifest["sources"]
    assert isinstance(raw_value, list)
    return [cast(dict[str, object], item) for item in raw_value]


def _genbank_record(accession: str) -> str:
    return (
        "LOCUS       TESTSEQ                 12 bp    DNA     linear   UNA 01-JAN-1980\n"
        f"ACCESSION   {accession.split('.', maxsplit=1)[0]}\n"
        f"VERSION     {accession}\n"
        "ORIGIN\n"
        "        1 acgtacgtacgt\n"
        "//\n"
    )


def _genbank_record_with_url(accession: str) -> str:
    return (
        "LOCUS       TESTSEQ                 12 bp    DNA     linear   UNA 01-JAN-1980\n"
        f"ACCESSION   {accession.split('.', maxsplit=1)[0]}\n"
        f"VERSION     {accession}\n"
        "COMMENT     External reference https://example.org/resource//keep-boundary\n"
        "ORIGIN\n"
        "        1 acgtacgtacgt\n"
        "//\n"
    )


@pytest.mark.parametrize(
    "suffix",
    [".fasta", ".gbk", ".txt", ".seq", ".mfa", ".afa", ".FA"],
)
def test_stage_materializes_supported_file_extensions(tmp_path: Path, suffix: str) -> None:
    sample = tmp_path / f"sample{suffix}"
    sample.write_text(">s\nACGT\n", encoding="utf-8")

    context, _events = _run_stage(tmp_path, samples=[str(sample)])
    manifest = _manifest(context)
    materialized = manifest["materialized_files"]

    assert isinstance(materialized, list)
    assert len(materialized) == 1
    first_item = materialized[0]
    assert isinstance(first_item, dict)
    assert first_item["format_hint"] == suffix.lower()
    assert int(first_item["size_bytes"]) == sample.stat().st_size


def test_stage_keeps_multi_fasta_as_single_materialized_file(tmp_path: Path) -> None:
    sample = tmp_path / "multi.fasta"
    sample.write_text(">a\nACGT\n>b\nTGCA\n", encoding="utf-8")

    context, _events = _run_stage(tmp_path, samples=[str(sample)])
    manifest = _manifest(context)
    materialized = manifest["materialized_files"]

    assert isinstance(materialized, list)
    assert len(materialized) == 1
    copied_relative_path = str(materialized[0]["relative_path"])
    copied_path = context.stage_staging_directory / copied_relative_path
    assert copied_path.read_text(encoding="utf-8") == sample.read_text(encoding="utf-8")


def test_stage_rejects_missing_local_path(tmp_path: Path) -> None:
    missing_path = tmp_path / "missing.fasta"
    context = _build_stage_context(tmp_path, samples=[str(missing_path)], events=[])
    stage = InputAcquisitionStage()
    stage.preflight(context)

    with pytest.raises(InputAcquisitionError) as error_info:
        stage.run(context, NullProgressReporter())

    assert error_info.value.event_name == INPUT_PATH_NOT_FOUND_EVENT


def test_stage_rejects_empty_file(tmp_path: Path) -> None:
    sample = tmp_path / "empty.fasta"
    sample.write_text("", encoding="utf-8")
    context = _build_stage_context(tmp_path, samples=[str(sample)], events=[])
    stage = InputAcquisitionStage()
    stage.preflight(context)

    with pytest.raises(InputAcquisitionError) as error_info:
        stage.run(context, NullProgressReporter())

    assert error_info.value.event_name == INPUT_FILE_EMPTY_EVENT


def test_stage_rejects_explicit_unsupported_extension(tmp_path: Path) -> None:
    sample = tmp_path / "sample.csv"
    sample.write_text("A,C,G,T\n", encoding="utf-8")
    context = _build_stage_context(tmp_path, samples=[str(sample)], events=[])
    stage = InputAcquisitionStage()
    stage.preflight(context)

    with pytest.raises(InputAcquisitionError) as error_info:
        stage.run(context, NullProgressReporter())

    assert error_info.value.event_name == INPUT_FILE_TYPE_UNSUPPORTED_EVENT


def test_stage_rejects_explicit_symlink_source(tmp_path: Path) -> None:
    target = tmp_path / "target.fasta"
    target.write_text(">s\nACGT\n", encoding="utf-8")
    symlink = tmp_path / "linked.fasta"
    symlink.symlink_to(target)
    context = _build_stage_context(tmp_path, samples=[str(symlink)], events=[])
    stage = InputAcquisitionStage()
    stage.preflight(context)

    with pytest.raises(InputAcquisitionError) as error_info:
        stage.run(context, NullProgressReporter())

    assert error_info.value.event_name == INPUT_SYMLINK_UNSUPPORTED_EVENT


def test_empty_directory_is_non_fatal_source_error_but_fails_without_data(tmp_path: Path) -> None:
    directory = tmp_path / "inputs"
    directory.mkdir()
    events: list[tuple[str, dict[str, object]]] = []
    context = _build_stage_context(tmp_path, samples=[str(directory)], events=events)
    stage = InputAcquisitionStage()
    stage.preflight(context)

    with pytest.raises(InputAcquisitionError) as error_info:
        stage.run(context, NullProgressReporter())

    assert error_info.value.event_name == INPUT_NO_DATA_ACQUIRED_EVENT
    assert any(name == INPUT_DIRECTORY_EMPTY_EVENT for name, _ in events)


def test_empty_directory_with_other_valid_source_succeeds(tmp_path: Path) -> None:
    directory = tmp_path / "inputs"
    directory.mkdir()
    sample = tmp_path / "sample.fasta"
    sample.write_text(">s\nACGT\n", encoding="utf-8")

    context, events = _run_stage(tmp_path, samples=[str(directory), str(sample)])
    manifest = _manifest(context)
    materialized = _manifest_materialized_files(manifest)

    assert any(name == INPUT_DIRECTORY_EMPTY_EVENT for name, _ in events)
    assert len(materialized) == 1


def test_directory_without_supported_files_is_non_fatal_when_other_data_exists(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "inputs"
    directory.mkdir()
    (directory / "readme.md").write_text("notes", encoding="utf-8")
    sample = tmp_path / "sample.fasta"
    sample.write_text(">s\nACGT\n", encoding="utf-8")

    context, events = _run_stage(tmp_path, samples=[str(directory), str(sample)])
    _ = _manifest(context)

    matched = [event for event in events if event[0] == INPUT_DIRECTORY_NO_SUPPORTED_FILES_EVENT]
    assert len(matched) == 1
    assert ".fasta" in str(matched[0][1]["detail"])


def test_directory_with_supported_and_unsupported_files_emits_aggregated_warning(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "mixed"
    directory.mkdir()
    (directory / "a.fasta").write_text(">a\nACGT\n", encoding="utf-8")
    (directory / "b.gbk").write_text("LOCUS demo\n", encoding="utf-8")
    (directory / "notes.md").write_text("x", encoding="utf-8")
    (directory / "table.csv").write_text("x", encoding="utf-8")
    (directory / "plot.png").write_text("x", encoding="utf-8")

    context, events = _run_stage(tmp_path, samples=[str(directory)])
    manifest = _manifest(context)
    materialized = _manifest_materialized_files(manifest)

    assert len(materialized) == 2
    warnings = [event for event in events if event[0] == INPUT_UNSUPPORTED_FILES_SKIPPED_EVENT]
    assert len(warnings) == 1
    warning_context = warnings[0][1]
    assert warning_context["total_count"] == 3
    assert warning_context["hidden_count"] == 0
    assert warning_context["shown_relative_paths"] == ["notes.md", "plot.png", "table.csv"]


def test_aggregated_message_shows_all_items_when_count_is_ten(tmp_path: Path) -> None:
    directory = tmp_path / "many"
    directory.mkdir()
    (directory / "sample.fasta").write_text(">a\nACGT\n", encoding="utf-8")
    for index in range(10):
        (directory / f"{index:02d}.csv").write_text("x", encoding="utf-8")

    _context, events = _run_stage(tmp_path, samples=[str(directory)])
    warning = next(event for event in events if event[0] == INPUT_UNSUPPORTED_FILES_SKIPPED_EVENT)

    assert warning[1]["total_count"] == 10
    assert warning[1]["hidden_count"] == 0
    shown = warning[1]["shown_relative_paths"]
    assert isinstance(shown, list)
    assert len(shown) == 10


def test_aggregated_message_collapses_after_ten_items(tmp_path: Path) -> None:
    directory = tmp_path / "many"
    directory.mkdir()
    (directory / "sample.fasta").write_text(">a\nACGT\n", encoding="utf-8")
    for index in range(11):
        (directory / f"{index:02d}.csv").write_text("x", encoding="utf-8")

    _context, events = _run_stage(tmp_path, samples=[str(directory)])
    warning = next(event for event in events if event[0] == INPUT_UNSUPPORTED_FILES_SKIPPED_EVENT)

    assert warning[1]["total_count"] == 11
    assert warning[1]["hidden_count"] == 6
    shown = warning[1]["shown_relative_paths"]
    assert isinstance(shown, list)
    assert len(shown) == 5
    assert "and 6 more files." in str(warning[1]["detail"])


def test_directory_depth_limit_warning_is_emitted(tmp_path: Path) -> None:
    directory = tmp_path / "root"
    nested = directory / "nested"
    nested.mkdir(parents=True)
    (nested / "sample.fasta").write_text(">s\nACGT\n", encoding="utf-8")
    events: list[tuple[str, dict[str, object]]] = []
    context = _build_stage_context(
        tmp_path,
        samples=[str(directory)],
        input_directory_max_depth=0,
        events=events,
    )
    stage = InputAcquisitionStage()
    stage.preflight(context)

    with pytest.raises(InputAcquisitionError) as error_info:
        stage.run(context, NullProgressReporter())

    assert error_info.value.event_name == INPUT_NO_DATA_ACQUIRED_EVENT
    warnings = [event for event in events if event[0] == INPUT_DIRECTORY_DEPTH_LIMIT_REACHED_EVENT]
    assert len(warnings) == 1
    assert "increase input_directory_max_depth" in str(warnings[0][1]["detail"])


def test_symlink_inside_directory_is_skipped_with_warning(tmp_path: Path) -> None:
    directory = tmp_path / "root"
    directory.mkdir()
    target = directory / "target.fasta"
    target.write_text(">s\nACGT\n", encoding="utf-8")
    (directory / "linked.fasta").symlink_to(target)

    _context, events = _run_stage(tmp_path, samples=[str(directory)])
    warning = next(event for event in events if event[0] == INPUT_SYMLINKS_SKIPPED_EVENT)

    assert warning[1]["total_count"] == 1
    assert warning[1]["shown_relative_paths"] == ["linked.fasta"]


def test_duplicate_sources_are_skipped_and_reported(tmp_path: Path) -> None:
    sample = tmp_path / "sample.fasta"
    sample.write_text(">s\nACGT\n", encoding="utf-8")
    absolute = str(sample.resolve())

    context, events = _run_stage(tmp_path, samples=[str(sample), absolute, str(sample)])
    manifest = _manifest(context)
    materialized = _manifest_materialized_files(manifest)

    assert len(materialized) == 1
    warnings = [event for event in events if event[0] == INPUT_DUPLICATES_SKIPPED_EVENT]
    assert len(warnings) == 1
    assert warnings[0][1]["total_count"] == 1


def test_ncbi_single_accession_materialization(tmp_path: Path) -> None:
    accession = "NC_000001.1"
    client = _FakeNcbiClient(responses={(accession,): _genbank_record(accession)})

    context, _events = _run_stage(tmp_path, samples=[accession], ncbi_client=client)
    manifest = _manifest(context)
    materialized = _manifest_materialized_files(manifest)

    assert client.calls == [(accession,)]
    assert len(materialized) == 1
    first_item = materialized[0]
    assert first_item["requested_accession"] == accession
    assert first_item["resolved_accession"] == accession
    assert first_item["source_type"] == "ncbi_nucleotide_record"


def test_ncbi_accession_and_url_are_deduplicated_before_fetch(tmp_path: Path) -> None:
    accession = "NC_000913.3"
    url = "https://www.ncbi.nlm.nih.gov/nuccore/NC_000913.3/"
    client = _FakeNcbiClient(responses={(accession,): _genbank_record(accession)})

    context, events = _run_stage(tmp_path, samples=[accession, url], ncbi_client=client)
    manifest = _manifest(context)
    materialized = _manifest_materialized_files(manifest)

    assert client.calls == [(accession,)]
    assert len(materialized) == 1
    duplicate_warnings = [event for event in events if event[0] == INPUT_DUPLICATES_SKIPPED_EVENT]
    assert len(duplicate_warnings) == 1


def test_ncbi_api_key_is_forwarded_to_client(tmp_path: Path) -> None:
    accession = "NC_000913.3"
    client = _FakeNcbiClient(responses={(accession,): _genbank_record(accession)})

    _run_stage(
        tmp_path,
        samples=[accession],
        ncbi_client=client,
        ncbi_api_key="configured-key",
    )

    assert client.api_keys == ["configured-key"]


def test_ncbi_record_with_embedded_https_url_is_not_split(tmp_path: Path) -> None:
    accession = "NC_000001.1"
    client = _FakeNcbiClient(
        responses={(accession,): _genbank_record_with_url(accession)},
    )

    context, _events = _run_stage(tmp_path, samples=[accession], ncbi_client=client)
    manifest = _manifest(context)
    materialized = _manifest_materialized_files(manifest)

    copied_relative_path = str(materialized[0]["relative_path"])
    copied_path = context.stage_staging_directory / copied_relative_path
    copied_payload = copied_path.read_text(encoding="utf-8")
    assert "https://example.org/resource//keep-boundary" in copied_payload
    assert copied_payload.rstrip().endswith("//")


def test_ncbi_multi_record_payload_splits_only_on_terminator_lines(tmp_path: Path) -> None:
    first = "NC_000001.1"
    second = "NC_000002.1"
    batch = (first, second)
    payload = _genbank_record_with_url(first) + "\n" + _genbank_record_with_url(second) + "\n\n"
    client = _FakeNcbiClient(responses={batch: payload})

    context, _events = _run_stage(tmp_path, samples=[first, second], ncbi_client=client)
    manifest = _manifest(context)
    materialized = _manifest_materialized_files(manifest)

    assert len(materialized) == 2
    assert {str(item["resolved_accession"]) for item in materialized} == {first, second}


def test_ncbi_accessions_above_batch_limit_are_split_sequentially(tmp_path: Path) -> None:
    accessions = [f"AB{index:06d}.1" for index in range(1, 102)]
    first_batch = tuple(accessions[:100])
    second_batch = tuple(accessions[100:])
    client = _FakeNcbiClient(
        responses={
            first_batch: "".join(_genbank_record(accession) for accession in first_batch),
            second_batch: "".join(_genbank_record(accession) for accession in second_batch),
        }
    )

    context, _events = _run_stage(tmp_path, samples=accessions, ncbi_client=client)
    manifest = _manifest(context)
    materialized = _manifest_materialized_files(manifest)

    assert client.calls == [first_batch, second_batch]
    assert len(materialized) == 101


def test_ncbi_partial_response_is_fatal(tmp_path: Path) -> None:
    first = "NC_000001.1"
    second = "NC_000002.1"
    client = _FakeNcbiClient(responses={(first, second): _genbank_record(first)})
    context = _build_stage_context(tmp_path, samples=[first, second], events=[])
    stage = InputAcquisitionStage(ncbi_client=client)
    stage.preflight(context)

    with pytest.raises(InputAcquisitionError) as error_info:
        stage.run(context, NullProgressReporter())

    assert error_info.value.event_name == NCBI_PARTIAL_RESPONSE_EVENT


def test_ncbi_record_not_found_for_single_accession(tmp_path: Path) -> None:
    accession = "NC_000001.1"
    client = _FakeNcbiClient(responses={(accession,): ""})
    context = _build_stage_context(tmp_path, samples=[accession], events=[])
    stage = InputAcquisitionStage(ncbi_client=client)
    stage.preflight(context)

    with pytest.raises(InputAcquisitionError) as error_info:
        stage.run(context, NullProgressReporter())

    assert error_info.value.event_name == NCBI_RECORD_NOT_FOUND_EVENT


def test_inline_sequence_materialization_is_redacted_in_manifest(tmp_path: Path) -> None:
    context, _events = _run_stage(tmp_path, samples=["acgt ACGT"])
    manifest = _manifest(context)
    sources = _manifest_sources(manifest)
    materialized = _manifest_materialized_files(manifest)

    assert sources[0]["source_type"] == "inline_sequence"
    source_payload = sources[0]["source"]
    assert isinstance(source_payload, dict)
    assert source_payload["length"] == 8
    assert "ACGT" not in json.dumps(source_payload)

    copied_path = context.stage_staging_directory / str(materialized[0]["relative_path"])
    copied_payload = copied_path.read_text(encoding="utf-8")
    assert copied_payload.startswith(">jelica_inline_sequence_0001\n")
    assert "ACGTACGT\n" in copied_payload


def test_long_inline_sequence_is_allowed_via_config_sources(tmp_path: Path) -> None:
    long_sequence = "A" * 512

    context, _events = _run_stage(tmp_path, samples=[long_sequence])
    manifest = _manifest(context)
    materialized = _manifest_materialized_files(manifest)

    assert len(materialized) == 1
    assert materialized[0]["inline_length"] == 512


def test_manifest_contains_required_metadata_fields(tmp_path: Path) -> None:
    sample = tmp_path / "sample.fasta"
    sample.write_text(">s\nACGT\n", encoding="utf-8")

    context, _events = _run_stage(tmp_path, samples=[str(sample)])
    manifest = _manifest(context)

    assert manifest["schema_version"] == 1
    assert manifest["task_id"] == "task-1"
    assert manifest["job_id"] == "job-1"
    assert "config_revision_path" in manifest
    assert "config_hash" in manifest
    assert "generated_at" in manifest
    assert "sources" in manifest
    assert "materialized_files" in manifest
    assert "skipped_duplicates" in manifest
    assert "source_errors" in manifest


@pytest.mark.parametrize(
    ("api_key", "expected_api_key"),
    [("", None), (" configured-key ", "configured-key")],
)
def test_http_ncbi_client_requests_gbwithparts(
    monkeypatch: pytest.MonkeyPatch,
    api_key: str,
    expected_api_key: str | None,
) -> None:
    payload = _genbank_record("NC_000001.1")

    class _Response:
        def __init__(self, text: str) -> None:
            self._text = text

        def __enter__(self) -> _Response:
            return self

        def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> Literal[False]:
            del exc_type, exc, tb
            return False

        def getcode(self) -> int:
            return 200

        def read(self) -> bytes:
            return self._text.encode("utf-8")

    def _fake_urlopen(request: Any, timeout: float) -> _Response:
        del timeout
        parsed = urlparse(request.full_url)
        query = parse_qs(parsed.query)
        assert query["rettype"] == ["gbwithparts"]
        assert query["retmode"] == ["text"]
        assert query["tool"] == ["JELICA"]
        assert query["email"] == ["rogovgreg@gmail.com"]
        if expected_api_key is None:
            assert "api_key" not in query
        else:
            assert query["api_key"] == [expected_api_key]
        return _Response(payload)

    monkeypatch.setattr("jelica_core.runtime.input_acquisition.urlopen", _fake_urlopen)
    client = HttpNCBINucleotideClient()

    result = client.fetch_nucleotide_genbank(
        accessions=("NC_000001.1",),
        api_key=api_key,
        max_retries=0,
        timeout_seconds=1.0,
    )

    assert "VERSION     NC_000001.1" in result


def test_http_ncbi_client_retries_timeout_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = _genbank_record("NC_000001.1")
    attempts = {"count": 0}

    class _Response:
        def __init__(self, text: str) -> None:
            self._text = text

        def __enter__(self) -> _Response:
            return self

        def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> Literal[False]:
            del exc_type, exc, tb
            return False

        def getcode(self) -> int:
            return 200

        def read(self) -> bytes:
            return self._text.encode("utf-8")

    def _fake_urlopen(request: Any, timeout: float) -> _Response:
        del request, timeout
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise URLError(socket.timeout("timeout"))
        return _Response(payload)

    monkeypatch.setattr("jelica_core.runtime.input_acquisition.urlopen", _fake_urlopen)
    client = HttpNCBINucleotideClient()

    result = client.fetch_nucleotide_genbank(
        accessions=("NC_000001.1",),
        api_key="",
        max_retries=2,
        timeout_seconds=1.0,
    )

    assert attempts["count"] == 2
    assert "VERSION     NC_000001.1" in result


def test_http_ncbi_client_does_not_retry_permanent_http_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = {"count": 0}

    def _fake_urlopen(request: Any, timeout: float) -> Any:
        del request, timeout
        attempts["count"] += 1
        raise HTTPError(
            url="https://example.test",
            code=400,
            msg="bad request",
            hdrs=Message(),
            fp=None,
        )

    monkeypatch.setattr("jelica_core.runtime.input_acquisition.urlopen", _fake_urlopen)
    client = HttpNCBINucleotideClient()

    with pytest.raises(InputAcquisitionError) as error_info:
        client.fetch_nucleotide_genbank(
            accessions=("NC_000001.1",),
            api_key="",
            max_retries=3,
            timeout_seconds=1.0,
        )

    assert attempts["count"] == 1
    assert error_info.value.event_name == NCBI_REQUEST_FAILED_EVENT
