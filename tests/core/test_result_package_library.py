from __future__ import annotations

import hashlib
import os
import zipfile
from pathlib import Path

import pytest

import jelica_core.result_package.artifacts as result_package_artifacts_module
from jelica_core.result_package import (
    JELICA_PACKAGE_CONFIGURATION_PATH,
    JELICA_PACKAGE_FORMAT,
    JELICA_PACKAGE_FORMAT_VERSION,
    JELICA_PACKAGE_INPUT_MANIFEST_PATH,
    JELICA_PACKAGE_MANIFEST_PATH,
    JELICA_PACKAGE_NORMALIZED_FASTA_PATH,
    JELICA_PACKAGE_NOTES_PATH,
    JELICA_PACKAGE_TASK_PATH,
    RESULT_PACKAGE_DIRECTORY_NAME,
    RESULT_PACKAGE_LINK_FILENAME,
    JelicaPackageManifest,
    ResultPackageArtifactInfo,
    ResultPackageLibraryError,
    ResultPackageLibraryErrorCode,
    ResultPackageLink,
    ResultPackageProducerInfo,
    ResultPackageStageInfo,
    ResultPackageTaskInfo,
    ResultPackageTaskStatus,
    compute_content_id,
    content_digest_from_content_id,
    import_result_package,
    infer_media_type,
    list_result_packages,
    relative_package_path_from_task,
    resolve_result_package_path,
    serialize_stable_json,
    write_result_package_link,
)
from jelica_core.system_config import CoreConfigService
from jelica_core.tasks import AnalyticalTaskRegistryService


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _base_payloads() -> dict[str, bytes]:
    return {
        JELICA_PACKAGE_TASK_PATH: (
            b'{"task_id":"task-1","status":"completed","created_at":"2026-08-01T00:00:00Z",'
            b'"completed_at":"2026-08-01T00:00:01Z"}\n'
        ),
        JELICA_PACKAGE_CONFIGURATION_PATH: b'{"alignment":{"mode":"none"}}\n',
        JELICA_PACKAGE_INPUT_MANIFEST_PATH: b'{"sources":[]}\n',
        JELICA_PACKAGE_NORMALIZED_FASTA_PATH: b">sample-a\nACGT\n",
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


def _build_manifest(*, payloads: dict[str, bytes]) -> JelicaPackageManifest:
    artifacts = tuple(
        ResultPackageArtifactInfo(
            path=path,
            stage=_artifact_stage(path),
            media_type=infer_media_type(path),
            size=len(payload),
            sha256=_sha256(payload),
        )
        for path, payload in sorted(payloads.items())
    )
    return JelicaPackageManifest(
        format=JELICA_PACKAGE_FORMAT,
        format_version=JELICA_PACKAGE_FORMAT_VERSION,
        content_id=compute_content_id(artifacts=artifacts),
        producer=ResultPackageProducerInfo(version="1.0.0-test"),
        package_created_at="2026-08-01T00:00:02Z",
        task=ResultPackageTaskInfo(
            task_id="task-1",
            status=ResultPackageTaskStatus.COMPLETED,
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
                artifacts=("results/comparative_analysis/manifest.json",),
            ),
        ),
        artifacts=artifacts,
    )


def _write_package(
    *,
    package_path: Path,
    payloads: dict[str, bytes],
    manifest: JelicaPackageManifest,
    notes: bytes | None = None,
    compression: int = zipfile.ZIP_DEFLATED,
) -> None:
    package_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(package_path, mode="w", compression=compression) as archive:
        for path, payload in sorted(payloads.items()):
            archive.writestr(path, payload)
        if notes is not None:
            archive.writestr(JELICA_PACKAGE_NOTES_PATH, notes)
        archive.writestr(
            JELICA_PACKAGE_MANIFEST_PATH,
            serialize_stable_json(manifest.model_dump(mode="json")).encode("utf-8"),
        )


def _build_package(
    *,
    package_path: Path,
    notes: bytes | None = None,
    compression: int = zipfile.ZIP_DEFLATED,
    payload_overrides: dict[str, bytes] | None = None,
) -> tuple[str, dict[str, bytes]]:
    payloads = _base_payloads()
    if payload_overrides:
        payloads.update(payload_overrides)
    manifest = _build_manifest(payloads=payloads)
    _write_package(
        package_path=package_path,
        payloads=payloads,
        manifest=manifest,
        notes=notes,
        compression=compression,
    )
    return manifest.content_id, payloads


def _make_core_service(tmp_path: Path) -> CoreConfigService:
    core_service = CoreConfigService(jelica_home=tmp_path / "home")
    core_service.initialize_system_config()
    return core_service


def _result_packages_dir(core_service: CoreConfigService) -> Path:
    return core_service.get_jelica_home() / RESULT_PACKAGE_DIRECTORY_NAME


def _register_task(
    *,
    core_service: CoreConfigService,
    task_id: str,
    name: str | None = None,
) -> Path:
    resolved = core_service.require_initialized_config()
    task_dir = resolved.tasks_dir / task_id
    task_dir.mkdir(parents=True, exist_ok=True)
    registry = AnalyticalTaskRegistryService(database_path=resolved.database_path)
    registry.register_task(
        task_id=task_id,
        name=name,
        task_dir_relative_path=task_id,
        current_config_relative_path="configs/000001.json",
        current_config_hash="a" * 64,
    )
    return task_dir


def _write_task_link(
    *,
    core_service: CoreConfigService,
    task_dir: Path,
    content_id: str,
    relative_path: str | None = None,
) -> Path:
    if relative_path is None:
        digest = content_digest_from_content_id(content_id)
        package_path = _result_packages_dir(core_service) / f"{digest}.jelica"
        relative_path = relative_package_path_from_task(
            task_dir=task_dir,
            package_path=package_path,
        )
    link = ResultPackageLink(
        content_id=content_id,
        path=relative_path,
        format_version=JELICA_PACKAGE_FORMAT_VERSION,
    )
    return write_result_package_link(task_dir=task_dir, link=link)


def test_import_result_package_imports_new_file_and_keeps_source(tmp_path: Path) -> None:
    core_service = _make_core_service(tmp_path)
    source_path = tmp_path / "input" / "package.jelica"
    content_id, _payloads = _build_package(package_path=source_path)
    source_bytes = source_path.read_bytes()

    imported = import_result_package(source_path=source_path, core_config_service=core_service)

    assert imported.already_exists is False
    assert imported.content_id == content_id
    assert imported.path.is_file()
    assert imported.path.name == f"{content_digest_from_content_id(content_id)}.jelica"
    assert source_path.read_bytes() == source_bytes
    assert source_path.is_file()
    assert list(_result_packages_dir(core_service).glob("*.tmp")) == []


def test_import_result_package_is_idempotent_and_skips_copy_on_repeat(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    core_service = _make_core_service(tmp_path)
    source_path = tmp_path / "input" / "package.jelica"
    _build_package(package_path=source_path)
    first = import_result_package(source_path=source_path, core_config_service=core_service)
    assert first.already_exists is False

    def _unexpected_copy(*, source_path: Path, target_path: Path) -> None:
        _ = (source_path, target_path)
        raise AssertionError("copy should not run for idempotent import")

    monkeypatch.setattr(result_package_artifacts_module, "_copy_file_atomically", _unexpected_copy)
    second = import_result_package(source_path=source_path, core_config_service=core_service)
    assert second.already_exists is True


def test_import_result_package_accepts_same_content_with_different_zip_metadata(
    tmp_path: Path,
) -> None:
    core_service = _make_core_service(tmp_path)
    source_a = tmp_path / "a.jelica"
    source_b = tmp_path / "b.jelica"
    content_a, _ = _build_package(package_path=source_a, compression=zipfile.ZIP_DEFLATED)
    content_b, _ = _build_package(package_path=source_b, compression=zipfile.ZIP_STORED)
    assert content_a == content_b

    first = import_result_package(source_path=source_a, core_config_service=core_service)
    second = import_result_package(source_path=source_b, core_config_service=core_service)

    assert first.already_exists is False
    assert second.already_exists is True


@pytest.mark.parametrize(
    ("first_notes", "second_notes", "expect_conflict"),
    (
        (None, None, False),
        (b"notes\n", b"notes\n", False),
        (b"notes-a\n", b"notes-b\n", True),
        (None, b"notes\n", True),
        (b"", None, True),
    ),
)
def test_import_result_package_handles_notes_compatibility(
    tmp_path: Path,
    first_notes: bytes | None,
    second_notes: bytes | None,
    expect_conflict: bool,
) -> None:
    core_service = _make_core_service(tmp_path)
    source_a = tmp_path / "a.jelica"
    source_b = tmp_path / "b.jelica"
    _build_package(package_path=source_a, notes=first_notes)
    _build_package(package_path=source_b, notes=second_notes)
    first = import_result_package(source_path=source_a, core_config_service=core_service)
    assert first.already_exists is False

    if not expect_conflict:
        second = import_result_package(source_path=source_b, core_config_service=core_service)
        assert second.already_exists is True
        return

    with pytest.raises(ResultPackageLibraryError) as error_info:
        import_result_package(source_path=source_b, core_config_service=core_service)
    assert error_info.value.code is ResultPackageLibraryErrorCode.NOTES_CONFLICT


def test_import_result_package_rejects_invalid_source(tmp_path: Path) -> None:
    core_service = _make_core_service(tmp_path)
    source_path = tmp_path / "invalid.jelica"
    source_path.write_bytes(b"not-a-zip")

    with pytest.raises(ResultPackageLibraryError) as error_info:
        import_result_package(source_path=source_path, core_config_service=core_service)
    assert error_info.value.code is ResultPackageLibraryErrorCode.INVALID_SOURCE_PACKAGE


def test_import_result_package_does_not_overwrite_invalid_existing_target(tmp_path: Path) -> None:
    core_service = _make_core_service(tmp_path)
    source_path = tmp_path / "valid.jelica"
    content_id, _ = _build_package(package_path=source_path)
    target_path = _result_packages_dir(core_service) / (
        f"{content_digest_from_content_id(content_id)}.jelica"
    )
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_bytes(b"broken-existing")
    existing_bytes = target_path.read_bytes()

    with pytest.raises(ResultPackageLibraryError) as error_info:
        import_result_package(source_path=source_path, core_config_service=core_service)

    assert error_info.value.code is ResultPackageLibraryErrorCode.INVALID_EXISTING_PACKAGE
    assert target_path.read_bytes() == existing_bytes


def test_import_result_package_cleans_temporary_files_after_publish_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    core_service = _make_core_service(tmp_path)
    source_path = tmp_path / "valid.jelica"
    content_id, _ = _build_package(package_path=source_path)
    digest = content_digest_from_content_id(content_id)
    target_path = _result_packages_dir(core_service) / f"{digest}.jelica"
    original_replace = os.replace

    def _failing_replace(src: Path | str, dst: Path | str) -> None:
        if Path(dst) == target_path and str(src).endswith(".import.tmp"):
            raise OSError("replace failed")
        original_replace(src, dst)

    monkeypatch.setattr(result_package_artifacts_module.os, "replace", _failing_replace)
    with pytest.raises(ResultPackageLibraryError) as error_info:
        import_result_package(source_path=source_path, core_config_service=core_service)
    assert error_info.value.code is ResultPackageLibraryErrorCode.IMPORT_IO_ERROR
    assert not target_path.exists()
    assert list(_result_packages_dir(core_service).glob("*.import.tmp")) == []


def test_import_result_package_handles_source_already_in_store(tmp_path: Path) -> None:
    core_service = _make_core_service(tmp_path)
    source_path = tmp_path / "valid.jelica"
    _build_package(package_path=source_path)
    first = import_result_package(source_path=source_path, core_config_service=core_service)

    second = import_result_package(source_path=first.path, core_config_service=core_service)
    assert second.already_exists is True
    assert second.path == first.path


def test_list_result_packages_empty_when_store_missing(tmp_path: Path) -> None:
    core_service = _make_core_service(tmp_path)
    listing = list_result_packages(core_config_service=core_service)
    assert listing.packages == tuple()
    assert listing.has_invalid_entries is False


def test_list_result_packages_returns_sorted_entries_and_fields(tmp_path: Path) -> None:
    core_service = _make_core_service(tmp_path)
    source_a = tmp_path / "a.jelica"
    source_b = tmp_path / "b.jelica"
    content_a, _ = _build_package(package_path=source_a)
    content_b, _ = _build_package(
        package_path=source_b,
        payload_overrides={
            JELICA_PACKAGE_CONFIGURATION_PATH: b'{"alignment":{"mode":"compute"}}\n'
        },
    )
    import_result_package(source_path=source_a, core_config_service=core_service)
    import_result_package(source_path=source_b, core_config_service=core_service)

    listing = list_result_packages(core_config_service=core_service)
    listed_content_ids = [
        item.content_id for item in listing.packages if item.content_id is not None
    ]
    assert listed_content_ids == sorted(listed_content_ids)
    assert set(listed_content_ids) == {content_a, content_b}
    assert all(item.task_id == "task-1" for item in listing.packages if item.valid)
    assert all(
        item.status in {"completed", "completed_with_warnings"}
        for item in listing.packages
        if item.valid
    )
    assert all(item.format_version == "1.0" for item in listing.packages if item.valid)


def test_list_result_packages_ignores_non_jelica_and_subdirectories(tmp_path: Path) -> None:
    core_service = _make_core_service(tmp_path)
    source_path = tmp_path / "valid.jelica"
    _build_package(package_path=source_path)
    import_result_package(source_path=source_path, core_config_service=core_service)
    store_dir = _result_packages_dir(core_service)
    (store_dir / "ignored.txt").write_text("x", encoding="utf-8")
    nested = store_dir / "nested"
    nested.mkdir(parents=True, exist_ok=True)
    (nested / "nested.jelica").write_bytes(b"ignored")

    listing = list_result_packages(core_config_service=core_service)
    assert len(listing.packages) == 1


def test_list_result_packages_marks_invalid_entries_without_failing(tmp_path: Path) -> None:
    core_service = _make_core_service(tmp_path)
    source_path = tmp_path / "valid.jelica"
    _build_package(package_path=source_path)
    import_result_package(source_path=source_path, core_config_service=core_service)
    broken_path = _result_packages_dir(core_service) / "broken.jelica"
    broken_path.write_bytes(b"not-a-zip")

    listing = list_result_packages(core_config_service=core_service)

    assert listing.has_invalid_entries is True
    invalid = [item for item in listing.packages if not item.valid]
    assert len(invalid) == 1
    assert invalid[0].file_name == "broken.jelica"


def test_list_result_packages_does_not_hash_all_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    core_service = _make_core_service(tmp_path)
    source_path = tmp_path / "valid.jelica"
    _build_package(package_path=source_path)
    import_result_package(source_path=source_path, core_config_service=core_service)

    def _unexpected_hash(*, archive: zipfile.ZipFile, entry_path: str) -> str:
        _ = (archive, entry_path)
        raise AssertionError("list must not hash all artifacts")

    monkeypatch.setattr(result_package_artifacts_module, "_sha256_for_zip_entry", _unexpected_hash)
    listing = list_result_packages(core_config_service=core_service)
    assert len(listing.packages) == 1
    assert listing.packages[0].valid is True


def test_resolve_result_package_path_by_content_id_and_digest(tmp_path: Path) -> None:
    core_service = _make_core_service(tmp_path)
    source_path = tmp_path / "valid.jelica"
    _build_package(package_path=source_path)
    imported = import_result_package(source_path=source_path, core_config_service=core_service)
    digest = content_digest_from_content_id(imported.content_id)

    by_content_id = resolve_result_package_path(
        task_or_content_ref=imported.content_id,
        core_config_service=core_service,
    )
    by_digest = resolve_result_package_path(
        task_or_content_ref=digest,
        core_config_service=core_service,
    )

    assert by_content_id.path == imported.path
    assert by_digest.path == imported.path


def test_resolve_result_package_path_rejects_missing_content_id(tmp_path: Path) -> None:
    core_service = _make_core_service(tmp_path)

    with pytest.raises(ResultPackageLibraryError) as error_info:
        resolve_result_package_path(
            task_or_content_ref="sha256:" + ("a" * 64),
            core_config_service=core_service,
        )
    assert error_info.value.code is ResultPackageLibraryErrorCode.PACKAGE_NOT_FOUND


def test_resolve_result_package_path_by_task_id(tmp_path: Path) -> None:
    core_service = _make_core_service(tmp_path)
    source_path = tmp_path / "valid.jelica"
    _build_package(package_path=source_path)
    imported = import_result_package(source_path=source_path, core_config_service=core_service)
    task_dir = _register_task(core_service=core_service, task_id="task-xyz")
    _write_task_link(core_service=core_service, task_dir=task_dir, content_id=imported.content_id)

    resolved = resolve_result_package_path(
        task_or_content_ref="task-xyz",
        core_config_service=core_service,
    )

    assert resolved.path == imported.path
    assert resolved.content_id == imported.content_id


def test_resolve_result_package_path_by_case_insensitive_task_name(tmp_path: Path) -> None:
    core_service = _make_core_service(tmp_path)
    source_path = tmp_path / "valid.jelica"
    _build_package(package_path=source_path)
    imported = import_result_package(source_path=source_path, core_config_service=core_service)
    task_dir = _register_task(
        core_service=core_service,
        task_id="00000000-0000-4000-8000-000000000001",
        name="Named-Result",
    )
    _write_task_link(core_service=core_service, task_dir=task_dir, content_id=imported.content_id)

    resolved = resolve_result_package_path(
        task_or_content_ref="nAmEd-ReSuLt",
        core_config_service=core_service,
    )

    assert resolved.path == imported.path
    assert resolved.content_id == imported.content_id


def test_resolve_bare_digest_prefers_task_name_but_prefixed_digest_prefers_content(
    tmp_path: Path,
) -> None:
    core_service = _make_core_service(tmp_path)
    content_source = tmp_path / "content.jelica"
    task_source = tmp_path / "task.jelica"
    _build_package(package_path=content_source)
    _build_package(
        package_path=task_source,
        payload_overrides={JELICA_PACKAGE_NORMALIZED_FASTA_PATH: b">sample-b\nTGCA\n"},
    )
    content_package = import_result_package(
        source_path=content_source,
        core_config_service=core_service,
    )
    task_package = import_result_package(
        source_path=task_source,
        core_config_service=core_service,
    )
    content_digest = content_digest_from_content_id(content_package.content_id)
    task_dir = _register_task(
        core_service=core_service,
        task_id="00000000-0000-4000-8000-000000000002",
        name=content_digest,
    )
    _write_task_link(
        core_service=core_service,
        task_dir=task_dir,
        content_id=task_package.content_id,
    )

    by_name = resolve_result_package_path(
        task_or_content_ref=content_digest,
        core_config_service=core_service,
    )
    by_explicit_content_id = resolve_result_package_path(
        task_or_content_ref=content_package.content_id,
        core_config_service=core_service,
    )

    assert by_name.path == task_package.path
    assert by_explicit_content_id.path == content_package.path


def test_resolve_result_package_path_rejects_unknown_task(tmp_path: Path) -> None:
    core_service = _make_core_service(tmp_path)
    with pytest.raises(ResultPackageLibraryError) as error_info:
        resolve_result_package_path(
            task_or_content_ref="missing-task",
            core_config_service=core_service,
        )
    assert error_info.value.code is ResultPackageLibraryErrorCode.TASK_NOT_FOUND


def test_resolve_result_package_path_rejects_task_without_link(tmp_path: Path) -> None:
    core_service = _make_core_service(tmp_path)
    _register_task(core_service=core_service, task_id="task-1")
    with pytest.raises(ResultPackageLibraryError) as error_info:
        resolve_result_package_path(
            task_or_content_ref="task-1",
            core_config_service=core_service,
        )
    assert error_info.value.code is ResultPackageLibraryErrorCode.TASK_HAS_NO_RESULT_PACKAGE


def test_resolve_result_package_path_rejects_invalid_link_json(tmp_path: Path) -> None:
    core_service = _make_core_service(tmp_path)
    task_dir = _register_task(core_service=core_service, task_id="task-1")
    (task_dir / RESULT_PACKAGE_LINK_FILENAME).write_text("{invalid", encoding="utf-8")
    with pytest.raises(ResultPackageLibraryError) as error_info:
        resolve_result_package_path(
            task_or_content_ref="task-1",
            core_config_service=core_service,
        )
    assert error_info.value.code is ResultPackageLibraryErrorCode.INVALID_RESULT_PACKAGE_LINK


def test_resolve_result_package_path_rejects_unsafe_link(tmp_path: Path) -> None:
    core_service = _make_core_service(tmp_path)
    source_path = tmp_path / "valid.jelica"
    content_id, _ = _build_package(package_path=source_path)
    task_dir = _register_task(core_service=core_service, task_id="task-1")
    _write_task_link(
        core_service=core_service,
        task_dir=task_dir,
        content_id=content_id,
        relative_path="../../outside/result.jelica",
    )
    with pytest.raises(ResultPackageLibraryError) as error_info:
        resolve_result_package_path(
            task_or_content_ref="task-1",
            core_config_service=core_service,
        )
    assert error_info.value.code is ResultPackageLibraryErrorCode.UNSAFE_RESULT_PACKAGE_LINK


def test_resolve_result_package_path_rejects_missing_link_target(tmp_path: Path) -> None:
    core_service = _make_core_service(tmp_path)
    source_path = tmp_path / "valid.jelica"
    content_id, _ = _build_package(package_path=source_path)
    task_dir = _register_task(core_service=core_service, task_id="task-1")
    _write_task_link(core_service=core_service, task_dir=task_dir, content_id=content_id)

    with pytest.raises(ResultPackageLibraryError) as error_info:
        resolve_result_package_path(
            task_or_content_ref="task-1",
            core_config_service=core_service,
        )
    assert error_info.value.code is ResultPackageLibraryErrorCode.PACKAGE_NOT_FOUND


def test_import_result_package_does_not_silently_overwrite_on_concurrent_file_exists(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    core_service = _make_core_service(tmp_path)
    source_path = tmp_path / "source.jelica"
    content_id, payloads = _build_package(package_path=source_path, notes=b"a\n")
    digest = content_digest_from_content_id(content_id)
    target_path = _result_packages_dir(core_service) / f"{digest}.jelica"
    manifest = _build_manifest(payloads=payloads)
    original_replace = os.replace
    replaced = {"done": False}

    def _race_replace(src: Path | str, dst: Path | str) -> None:
        if Path(dst) == target_path and str(src).endswith(".import.tmp") and not replaced["done"]:
            _write_package(
                package_path=target_path,
                payloads=payloads,
                manifest=manifest,
                notes=b"b\n",
            )
            replaced["done"] = True
            raise FileExistsError(str(dst))
        original_replace(src, dst)

    monkeypatch.setattr(result_package_artifacts_module.os, "replace", _race_replace)

    with pytest.raises(ResultPackageLibraryError) as error_info:
        import_result_package(source_path=source_path, core_config_service=core_service)

    assert error_info.value.code is ResultPackageLibraryErrorCode.NOTES_CONFLICT
    with zipfile.ZipFile(target_path, mode="r") as archive:
        assert archive.read(JELICA_PACKAGE_NOTES_PATH) == b"b\n"
