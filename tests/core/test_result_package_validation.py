from __future__ import annotations

import hashlib
import json
import stat
import zipfile
from pathlib import Path
from typing import Any

import pytest

from jelica_core.result_package import (
    JELICA_PACKAGE_CONFIGURATION_PATH,
    JELICA_PACKAGE_FORMAT,
    JELICA_PACKAGE_FORMAT_VERSION,
    JELICA_PACKAGE_INPUT_MANIFEST_PATH,
    JELICA_PACKAGE_MANIFEST_PATH,
    JELICA_PACKAGE_NORMALIZED_FASTA_PATH,
    JELICA_PACKAGE_NOTES_PATH,
    JELICA_PACKAGE_TASK_PATH,
    JelicaPackageManifest,
    JelicaPackageReader,
    JelicaPackageValidationIssueCode,
    JelicaPackageValidationResult,
    JelicaPackageValidator,
    ResultPackageArtifactInfo,
    ResultPackageProducerInfo,
    ResultPackageStageInfo,
    ResultPackageTaskInfo,
    ResultPackageTaskStatus,
    compute_content_id,
    infer_media_type,
    serialize_stable_json,
)


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _base_payloads(*, large_fasta: bool = False) -> dict[str, bytes]:
    fasta_sequence = b"A" * (1024 * 256 if large_fasta else 4)
    fasta_payload = b">sample-a\n" + fasta_sequence + b"\n"
    return {
        JELICA_PACKAGE_TASK_PATH: (
            b'{"task_id":"task-1","status":"completed","created_at":"2026-08-01T00:00:00Z",'
            b'"completed_at":"2026-08-01T00:00:01Z"}\n'
        ),
        JELICA_PACKAGE_CONFIGURATION_PATH: b'{"alignment":{"mode":"none"}}\n',
        JELICA_PACKAGE_INPUT_MANIFEST_PATH: b'{"sources":[]}\n',
        JELICA_PACKAGE_NORMALIZED_FASTA_PATH: fasta_payload,
        "results/comparative_analysis/manifest.json": b'{"status":"completed"}\n',
    }


def _artifact_stage(path: str) -> str | None:
    if path == JELICA_PACKAGE_INPUT_MANIFEST_PATH:
        return "input_acquisition"
    if path == JELICA_PACKAGE_NORMALIZED_FASTA_PATH:
        return "input_processing"
    if path.startswith("results/comparative_analysis/"):
        return "comparative_analysis"
    return None


def _build_manifest(
    *,
    payloads: dict[str, bytes],
    media_type_overrides: dict[str, str] | None = None,
    task_status: ResultPackageTaskStatus = ResultPackageTaskStatus.COMPLETED,
) -> JelicaPackageManifest:
    media_type_overrides = media_type_overrides or {}
    artifacts = tuple(
        ResultPackageArtifactInfo(
            path=path,
            stage=_artifact_stage(path),
            media_type=media_type_overrides.get(path, infer_media_type(path)),
            size=len(payload),
            sha256=_sha256(payload),
        )
        for path, payload in sorted(payloads.items())
    )
    content_id = compute_content_id(artifacts=artifacts)
    return JelicaPackageManifest(
        format=JELICA_PACKAGE_FORMAT,
        format_version=JELICA_PACKAGE_FORMAT_VERSION,
        content_id=content_id,
        producer=ResultPackageProducerInfo(version="1.0.0-test"),
        package_created_at="2026-08-01T00:00:02Z",
        task=ResultPackageTaskInfo(
            task_id="task-1",
            status=task_status,
            created_at="2026-08-01T00:00:00Z",
            completed_at="2026-08-01T00:00:01Z",
        ),
        stages=(
            ResultPackageStageInfo(
                name="input_acquisition",
                status="completed",
                artifacts=(JELICA_PACKAGE_INPUT_MANIFEST_PATH,),
            ),
            ResultPackageStageInfo(
                name="input_processing",
                status="completed",
                artifacts=(JELICA_PACKAGE_NORMALIZED_FASTA_PATH,),
            ),
            ResultPackageStageInfo(
                name="comparative_analysis",
                status="completed",
                artifacts=tuple(
                    sorted(
                        path
                        for path in payloads
                        if path.startswith("results/comparative_analysis/")
                    )
                ),
            ),
        ),
        artifacts=artifacts,
    )


def _write_package(
    *,
    path: Path,
    payloads: dict[str, bytes],
    manifest_payload: dict[str, object] | None = None,
    manifest_bytes: bytes | None = None,
    include_notes: bytes | None = None,
    extra_entries: list[tuple[str | zipfile.ZipInfo, bytes]] | None = None,
    skip_payload_paths: set[str] | None = None,
) -> None:
    skip_payload_paths = skip_payload_paths or set()
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        for entry_path, payload in sorted(payloads.items()):
            if entry_path in skip_payload_paths:
                continue
            archive.writestr(entry_path, payload)
        if include_notes is not None:
            archive.writestr(JELICA_PACKAGE_NOTES_PATH, include_notes)
        for entry_path, payload in extra_entries or []:
            archive.writestr(entry_path, payload)
        if manifest_bytes is not None:
            archive.writestr(JELICA_PACKAGE_MANIFEST_PATH, manifest_bytes)
        elif manifest_payload is not None:
            archive.writestr(
                JELICA_PACKAGE_MANIFEST_PATH,
                serialize_stable_json(manifest_payload).encode("utf-8"),
            )
        else:
            raise AssertionError("manifest payload must be provided")


def _create_valid_package(
    tmp_path: Path,
    *,
    large_fasta: bool = False,
    payload_overrides: dict[str, bytes] | None = None,
    media_type_overrides: dict[str, str] | None = None,
    include_notes: bytes | None = None,
) -> tuple[Path, dict[str, object], dict[str, bytes]]:
    payloads = _base_payloads(large_fasta=large_fasta)
    if payload_overrides:
        payloads.update(payload_overrides)
    manifest = _build_manifest(
        payloads=payloads,
        media_type_overrides=media_type_overrides,
    )
    package_path = tmp_path / "valid.jelica"
    manifest_payload = manifest.model_dump(mode="json")
    _write_package(
        path=package_path,
        payloads=payloads,
        manifest_payload=manifest_payload,
        include_notes=include_notes,
    )
    return package_path, manifest_payload, payloads


def _validation_codes(result: JelicaPackageValidationResult) -> list[str]:
    return [issue.code.value for issue in result.errors]


def _mutate_manifest(
    *,
    manifest_payload: dict[str, object],
    mutate: Any,
) -> dict[str, object]:
    cloned = json.loads(json.dumps(manifest_payload))
    mutate(cloned)
    return cloned


def test_validator_accepts_valid_package(tmp_path: Path) -> None:
    package_path, _manifest_payload, _payloads = _create_valid_package(tmp_path)
    result = JelicaPackageValidator().validate(package_path)
    assert result.valid is True
    assert result.format == JELICA_PACKAGE_FORMAT
    assert result.format_version == JELICA_PACKAGE_FORMAT_VERSION
    assert result.content_id is not None
    assert result.errors == tuple()


def test_reader_reads_manifest_and_streams_large_artifact(tmp_path: Path) -> None:
    package_path, _manifest_payload, _payloads = _create_valid_package(
        tmp_path, large_fasta=True
    )
    with JelicaPackageReader(path=package_path) as reader:
        manifest = reader.read_manifest()
        assert manifest.format_version == "1.0"
        with reader.open_artifact(path=JELICA_PACKAGE_NORMALIZED_FASTA_PATH) as stream:
            chunk = stream.read(64)
            assert chunk.startswith(b">sample-a")


def test_reader_does_not_extract_files_to_disk(tmp_path: Path) -> None:
    package_path, _manifest_payload, _payloads = _create_valid_package(tmp_path)
    before = sorted(path.relative_to(tmp_path).as_posix() for path in tmp_path.rglob("*"))
    with JelicaPackageReader(path=package_path) as reader:
        _ = reader.read_manifest()
    after = sorted(path.relative_to(tmp_path).as_posix() for path in tmp_path.rglob("*"))
    assert before == after


def test_validator_reports_missing_external_file(tmp_path: Path) -> None:
    result = JelicaPackageValidator().validate(tmp_path / "missing.jelica")
    assert result.valid is False
    assert _validation_codes(result) == [JelicaPackageValidationIssueCode.FILE_NOT_FOUND.value]


def test_validator_reports_path_not_a_file(tmp_path: Path) -> None:
    directory = tmp_path / "folder"
    directory.mkdir(parents=True)
    result = JelicaPackageValidator().validate(directory)
    assert result.valid is False
    assert _validation_codes(result) == [JelicaPackageValidationIssueCode.NOT_A_FILE.value]


def test_validator_reports_invalid_zip(tmp_path: Path) -> None:
    package_path = tmp_path / "broken.jelica"
    package_path.write_bytes(b"not-a-zip")
    result = JelicaPackageValidator().validate(package_path)
    assert result.valid is False
    assert _validation_codes(result) == [JelicaPackageValidationIssueCode.INVALID_ZIP.value]


def test_validator_rejects_duplicate_zip_entry(tmp_path: Path) -> None:
    package_path, manifest_payload, payloads = _create_valid_package(tmp_path)
    duplicate_path = JELICA_PACKAGE_CONFIGURATION_PATH
    _write_package(
        path=package_path,
        payloads=payloads,
        manifest_payload=manifest_payload,
        extra_entries=[(duplicate_path, b'{"duplicated":true}\n')],
    )
    result = JelicaPackageValidator().validate(package_path)
    assert JelicaPackageValidationIssueCode.DUPLICATE_ENTRY.value in _validation_codes(result)


def test_validator_rejects_path_traversal_entry(tmp_path: Path) -> None:
    package_path, manifest_payload, payloads = _create_valid_package(tmp_path)
    _write_package(
        path=package_path,
        payloads=payloads,
        manifest_payload=manifest_payload,
        extra_entries=[("../escape.txt", b"1")],
    )
    result = JelicaPackageValidator().validate(package_path)
    assert JelicaPackageValidationIssueCode.UNSAFE_PATH.value in _validation_codes(result)


def test_validator_rejects_absolute_entry_path(tmp_path: Path) -> None:
    package_path, manifest_payload, payloads = _create_valid_package(tmp_path)
    _write_package(
        path=package_path,
        payloads=payloads,
        manifest_payload=manifest_payload,
        extra_entries=[("/absolute.txt", b"1")],
    )
    result = JelicaPackageValidator().validate(package_path)
    assert JelicaPackageValidationIssueCode.UNSAFE_PATH.value in _validation_codes(result)


def test_validator_rejects_backslash_path(tmp_path: Path) -> None:
    package_path, manifest_payload, payloads = _create_valid_package(tmp_path)
    _write_package(
        path=package_path,
        payloads=payloads,
        manifest_payload=manifest_payload,
        extra_entries=[("input\\bad.json", b"{}")],
    )
    result = JelicaPackageValidator().validate(package_path)
    assert JelicaPackageValidationIssueCode.UNSAFE_PATH.value in _validation_codes(result)


def test_validator_rejects_symlink_entry(tmp_path: Path) -> None:
    package_path, manifest_payload, payloads = _create_valid_package(tmp_path)
    symlink_info = zipfile.ZipInfo("results/symlink-entry")
    symlink_info.compress_type = zipfile.ZIP_DEFLATED
    symlink_info.external_attr = (stat.S_IFLNK | 0o777) << 16
    _write_package(
        path=package_path,
        payloads=payloads,
        manifest_payload=manifest_payload,
        extra_entries=[(symlink_info, b"target")],
    )
    result = JelicaPackageValidator().validate(package_path)
    assert JelicaPackageValidationIssueCode.NON_REGULAR_ENTRY.value in _validation_codes(result)


def test_validator_rejects_encrypted_entry_flag(tmp_path: Path) -> None:
    package_path, manifest_payload, payloads = _create_valid_package(tmp_path)
    encrypted_info = zipfile.ZipInfo("results/encrypted-entry")
    encrypted_info.compress_type = zipfile.ZIP_DEFLATED
    encrypted_info.flag_bits |= 0x1
    _write_package(
        path=package_path,
        payloads=payloads,
        manifest_payload=manifest_payload,
        extra_entries=[(encrypted_info, b"payload")],
    )
    with zipfile.ZipFile(package_path, mode="r") as archive:
        info = archive.getinfo("results/encrypted-entry")
        if (info.flag_bits & 0x1) == 0:
            pytest.skip("stdlib zipfile writer did not persist encryption flag")
    result = JelicaPackageValidator().validate(package_path)
    assert JelicaPackageValidationIssueCode.ENCRYPTED_ENTRY.value in _validation_codes(result)


def test_validator_reports_missing_required_file(tmp_path: Path) -> None:
    package_path, manifest_payload, payloads = _create_valid_package(tmp_path)
    _write_package(
        path=package_path,
        payloads=payloads,
        manifest_payload=manifest_payload,
        skip_payload_paths={JELICA_PACKAGE_TASK_PATH},
    )
    result = JelicaPackageValidator().validate(package_path)
    assert JelicaPackageValidationIssueCode.MISSING_REQUIRED_FILE.value in _validation_codes(result)


def test_validator_reports_unexpected_file(tmp_path: Path) -> None:
    package_path, manifest_payload, payloads = _create_valid_package(tmp_path)
    _write_package(
        path=package_path,
        payloads=payloads,
        manifest_payload=manifest_payload,
        extra_entries=[("attachments/file.bin", b"1")],
    )
    result = JelicaPackageValidator().validate(package_path)
    assert JelicaPackageValidationIssueCode.UNEXPECTED_FILE.value in _validation_codes(result)


def test_validator_accepts_optional_root_notes(tmp_path: Path) -> None:
    package_path, _manifest_payload, _payloads = _create_valid_package(
        tmp_path,
        include_notes=b"user notes\n",
    )
    result = JelicaPackageValidator().validate(package_path)
    assert result.valid is True


def test_validator_rejects_invalid_utf8_notes(tmp_path: Path) -> None:
    package_path, _manifest_payload, _payloads = _create_valid_package(
        tmp_path,
        include_notes=b"\xff\xfe",
    )
    result = JelicaPackageValidator().validate(package_path)
    assert JelicaPackageValidationIssueCode.INVALID_NOTES.value in _validation_codes(result)


def test_validator_rejects_wrong_notes_filename(tmp_path: Path) -> None:
    package_path, manifest_payload, payloads = _create_valid_package(tmp_path)
    _write_package(
        path=package_path,
        payloads=payloads,
        manifest_payload=manifest_payload,
        extra_entries=[("notes.txt", b"wrong case")],
    )
    result = JelicaPackageValidator().validate(package_path)
    assert JelicaPackageValidationIssueCode.UNEXPECTED_FILE.value in _validation_codes(result)


def test_validator_rejects_notes_declared_in_manifest_artifacts(tmp_path: Path) -> None:
    package_path, manifest_payload, payloads = _create_valid_package(tmp_path)
    notes_payload = b"notes\n"
    manifest_with_notes = _mutate_manifest(
        manifest_payload=manifest_payload,
        mutate=lambda payload: payload["artifacts"].append(  # type: ignore[index]
            {
                "path": JELICA_PACKAGE_NOTES_PATH,
                "stage": None,
                "media_type": "text/plain",
                "size": len(notes_payload),
                "sha256": _sha256(notes_payload),
            }
        ),
    )
    _write_package(
        path=package_path,
        payloads=payloads,
        manifest_payload=manifest_with_notes,
        include_notes=notes_payload,
    )
    result = JelicaPackageValidator().validate(package_path)
    assert JelicaPackageValidationIssueCode.INVALID_NOTES.value in _validation_codes(result)


def test_validator_detects_checksum_mismatch_after_payload_change(tmp_path: Path) -> None:
    package_path, manifest_payload, payloads = _create_valid_package(tmp_path)
    mutated_payloads = dict(payloads)
    mutated_payloads[JELICA_PACKAGE_CONFIGURATION_PATH] = b'{"alignment":{"mode":"xxxx"}}\n'
    _write_package(
        path=package_path,
        payloads=mutated_payloads,
        manifest_payload=manifest_payload,
    )
    result = JelicaPackageValidator().validate(package_path)
    assert JelicaPackageValidationIssueCode.CHECKSUM_MISMATCH.value in _validation_codes(result)


def test_validator_detects_size_mismatch(tmp_path: Path) -> None:
    package_path, manifest_payload, payloads = _create_valid_package(tmp_path)

    def mutate(payload: dict[str, object]) -> None:
        for item in payload["artifacts"]:  # type: ignore[index]
            if item["path"] == JELICA_PACKAGE_CONFIGURATION_PATH:
                item["size"] = int(item["size"]) + 1

    mutated_manifest = _mutate_manifest(manifest_payload=manifest_payload, mutate=mutate)
    _write_package(
        path=package_path,
        payloads=payloads,
        manifest_payload=mutated_manifest,
    )
    result = JelicaPackageValidator().validate(package_path)
    assert JelicaPackageValidationIssueCode.SIZE_MISMATCH.value in _validation_codes(result)


def test_validator_detects_manifest_sha_mismatch(tmp_path: Path) -> None:
    package_path, manifest_payload, payloads = _create_valid_package(tmp_path)

    def mutate(payload: dict[str, object]) -> None:
        for item in payload["artifacts"]:  # type: ignore[index]
            if item["path"] == JELICA_PACKAGE_CONFIGURATION_PATH:
                item["sha256"] = "a" * 64

    mutated_manifest = _mutate_manifest(manifest_payload=manifest_payload, mutate=mutate)
    _write_package(
        path=package_path,
        payloads=payloads,
        manifest_payload=mutated_manifest,
    )
    result = JelicaPackageValidator().validate(package_path)
    assert JelicaPackageValidationIssueCode.CHECKSUM_MISMATCH.value in _validation_codes(result)


def test_validator_detects_content_id_mismatch(tmp_path: Path) -> None:
    package_path, manifest_payload, payloads = _create_valid_package(tmp_path)
    mutated_manifest = _mutate_manifest(
        manifest_payload=manifest_payload,
        mutate=lambda payload: payload.__setitem__("content_id", "sha256:" + ("0" * 64)),
    )
    _write_package(
        path=package_path,
        payloads=payloads,
        manifest_payload=mutated_manifest,
    )
    result = JelicaPackageValidator().validate(package_path)
    assert JelicaPackageValidationIssueCode.CONTENT_ID_MISMATCH.value in _validation_codes(result)


def test_validator_rejects_unsupported_format_version(tmp_path: Path) -> None:
    package_path, manifest_payload, payloads = _create_valid_package(tmp_path)
    mutated_manifest = _mutate_manifest(
        manifest_payload=manifest_payload,
        mutate=lambda payload: payload.__setitem__("format_version", "2.0"),
    )
    _write_package(
        path=package_path,
        payloads=payloads,
        manifest_payload=mutated_manifest,
    )
    result = JelicaPackageValidator().validate(package_path)
    assert _validation_codes(result) == [
        JelicaPackageValidationIssueCode.UNSUPPORTED_FORMAT_VERSION.value
    ]


def test_validator_rejects_invalid_manifest_json(tmp_path: Path) -> None:
    package_path, _manifest_payload, payloads = _create_valid_package(tmp_path)
    _write_package(
        path=package_path,
        payloads=payloads,
        manifest_bytes=b"{invalid-json",
    )
    result = JelicaPackageValidator().validate(package_path)
    assert _validation_codes(result) == [
        JelicaPackageValidationIssueCode.INVALID_MANIFEST_JSON.value
    ]


def test_validator_rejects_manifest_schema_violation(tmp_path: Path) -> None:
    package_path, manifest_payload, payloads = _create_valid_package(tmp_path)
    mutated_manifest = _mutate_manifest(
        manifest_payload=manifest_payload,
        mutate=lambda payload: payload.pop("producer"),
    )
    _write_package(
        path=package_path,
        payloads=payloads,
        manifest_payload=mutated_manifest,
    )
    result = JelicaPackageValidator().validate(package_path)
    assert _validation_codes(result) == [
        JelicaPackageValidationIssueCode.INVALID_MANIFEST_SCHEMA.value
    ]


def test_validator_reports_stage_artifacts_missing_from_global_artifacts_as_schema_error(
    tmp_path: Path,
) -> None:
    package_path, manifest_payload, payloads = _create_valid_package(tmp_path)

    def mutate(payload: dict[str, object]) -> None:
        stages = payload["stages"]  # type: ignore[index]
        stages[0]["artifacts"].append("results/input_acquisition/missing.json")

    mutated_manifest = _mutate_manifest(manifest_payload=manifest_payload, mutate=mutate)
    _write_package(
        path=package_path,
        payloads=payloads,
        manifest_payload=mutated_manifest,
    )
    result = JelicaPackageValidator().validate(package_path)
    assert _validation_codes(result) == [
        JelicaPackageValidationIssueCode.INVALID_MANIFEST_SCHEMA.value
    ]


def test_validator_reports_artifact_stage_mismatch(tmp_path: Path) -> None:
    package_path, manifest_payload, payloads = _create_valid_package(tmp_path)

    def mutate(payload: dict[str, object]) -> None:
        for item in payload["artifacts"]:  # type: ignore[index]
            if item["path"] == "results/comparative_analysis/manifest.json":
                item["stage"] = "distance_matrix"

    mutated_manifest = _mutate_manifest(manifest_payload=manifest_payload, mutate=mutate)
    _write_package(
        path=package_path,
        payloads=payloads,
        manifest_payload=mutated_manifest,
    )
    result = JelicaPackageValidator().validate(package_path)
    assert JelicaPackageValidationIssueCode.STAGE_ARTIFACT_MISMATCH.value in _validation_codes(
        result
    )


def test_validator_reports_task_metadata_mismatch(tmp_path: Path) -> None:
    package_path, manifest_payload, payloads = _create_valid_package(tmp_path)
    mutated_payloads = dict(payloads)
    mutated_payloads[JELICA_PACKAGE_TASK_PATH] = (
        b'{"task_id":"task-1","status":"completed_with_warnings","created_at":"2026-08-01T00:00:00Z",'
        b'"completed_at":"2026-08-01T00:00:01Z"}\n'
    )
    manifest_for_mutated = _build_manifest(payloads=mutated_payloads).model_dump(mode="json")
    manifest_for_mutated["task"]["status"] = "completed"
    _write_package(
        path=package_path,
        payloads=mutated_payloads,
        manifest_payload=manifest_for_mutated,
    )
    result = JelicaPackageValidator().validate(package_path)
    assert JelicaPackageValidationIssueCode.TASK_METADATA_MISMATCH.value in _validation_codes(
        result
    )


def test_validator_reports_trace_id_mismatch_with_configuration(tmp_path: Path) -> None:
    task_trace_id = "8b1c9d4e-1c33-4ab9-81b6-21408cc92cc4"
    config_trace_id = "7f209239-3104-48f6-b634-2a72f7b035de"
    payloads = _base_payloads()
    payloads[JELICA_PACKAGE_TASK_PATH] = (
        b'{"task_id":"task-1","trace_id":"'
        + task_trace_id.encode("ascii")
        + b'","status":"completed","created_at":"2026-08-01T00:00:00Z",'
        b'"completed_at":"2026-08-01T00:00:01Z"}\n'
    )
    payloads[JELICA_PACKAGE_CONFIGURATION_PATH] = json.dumps(
        {"trace_id": config_trace_id, "alignment": {"mode": "none"}}
    ).encode("utf-8")
    manifest_payload = _build_manifest(payloads=payloads).model_dump(mode="json")
    manifest_payload["task"]["trace_id"] = task_trace_id
    package_path = tmp_path / "trace-mismatch.jelica"
    _write_package(
        path=package_path,
        payloads=payloads,
        manifest_payload=manifest_payload,
    )

    result = JelicaPackageValidator().validate(package_path)

    assert JelicaPackageValidationIssueCode.TASK_METADATA_MISMATCH.value in _validation_codes(
        result
    )


def test_validator_reports_invalid_json_protected_artifact(tmp_path: Path) -> None:
    payloads = _base_payloads()
    payloads[JELICA_PACKAGE_CONFIGURATION_PATH] = b'{"alignment":'
    manifest_payload = _build_manifest(payloads=payloads).model_dump(mode="json")
    package_path = tmp_path / "invalid-json-artifact.jelica"
    _write_package(path=package_path, payloads=payloads, manifest_payload=manifest_payload)
    result = JelicaPackageValidator().validate(package_path)
    assert JelicaPackageValidationIssueCode.INVALID_JSON.value in _validation_codes(result)


def test_validator_reports_invalid_jsonl_line(tmp_path: Path) -> None:
    payloads = _base_payloads()
    payloads["results/comparative_analysis/records.jsonl"] = b'{"ok":1}\n{broken}\n'
    manifest_payload = _build_manifest(payloads=payloads).model_dump(mode="json")
    package_path = tmp_path / "invalid-jsonl.jelica"
    _write_package(path=package_path, payloads=payloads, manifest_payload=manifest_payload)
    result = JelicaPackageValidator().validate(package_path)
    assert JelicaPackageValidationIssueCode.INVALID_JSON.value in _validation_codes(result)


def test_validator_error_order_is_deterministic(tmp_path: Path) -> None:
    package_path, manifest_payload, payloads = _create_valid_package(tmp_path)
    _write_package(
        path=package_path,
        payloads=payloads,
        manifest_payload=manifest_payload,
        skip_payload_paths={JELICA_PACKAGE_TASK_PATH, JELICA_PACKAGE_CONFIGURATION_PATH},
        extra_entries=[("unexpected/file.bin", b"1")],
    )
    validator = JelicaPackageValidator()
    first = validator.validate(package_path)
    second = validator.validate(package_path)
    assert [issue.code for issue in first.errors] == [issue.code for issue in second.errors]
    assert [issue.path for issue in first.errors] == [issue.path for issue in second.errors]
