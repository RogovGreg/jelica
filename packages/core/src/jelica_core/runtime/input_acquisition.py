from __future__ import annotations

import hashlib
import json
import shutil
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from jelica_core.input_sources import (
    SUPPORTED_INPUT_EXTENSIONS,
    InputSourceClassification,
    InputSourceKind,
    classify_input_source,
    looks_like_local_path,
    looks_like_ncbi_accession,
    normalize_inline_sequence,
    normalize_ncbi_accession,
    normalized_input_extension,
)
from jelica_core.tasks.storage import write_text_atomically
from jelica_core.tasks.timestamps import serialize_utc_datetime, utc_now

from .pipeline import ProgressReporter, StageContext, StageRunResult

INPUT_SOURCE_UNSUPPORTED_EVENT = "INPUT_SOURCE_UNSUPPORTED"
INPUT_PATH_NOT_FOUND_EVENT = "INPUT_PATH_NOT_FOUND"
INPUT_FILE_TYPE_UNSUPPORTED_EVENT = "INPUT_FILE_TYPE_UNSUPPORTED"
INPUT_FILE_UNREADABLE_EVENT = "INPUT_FILE_UNREADABLE"
INPUT_FILE_EMPTY_EVENT = "INPUT_FILE_EMPTY"
INPUT_DIRECTORY_EMPTY_EVENT = "INPUT_DIRECTORY_EMPTY"
INPUT_DIRECTORY_NO_SUPPORTED_FILES_EVENT = "INPUT_DIRECTORY_NO_SUPPORTED_FILES"
INPUT_NO_DATA_ACQUIRED_EVENT = "INPUT_NO_DATA_ACQUIRED"
INPUT_UNSUPPORTED_FILES_SKIPPED_EVENT = "INPUT_UNSUPPORTED_FILES_SKIPPED"
INPUT_SYMLINK_UNSUPPORTED_EVENT = "INPUT_SYMLINK_UNSUPPORTED"
INPUT_SYMLINKS_SKIPPED_EVENT = "INPUT_SYMLINKS_SKIPPED"
INPUT_DIRECTORY_DEPTH_LIMIT_REACHED_EVENT = "INPUT_DIRECTORY_DEPTH_LIMIT_REACHED"
INPUT_DUPLICATES_SKIPPED_EVENT = "INPUT_DUPLICATES_SKIPPED"
INPUT_ACQUISITION_COMPLETED_EVENT = "INPUT_ACQUISITION_COMPLETED"
INPUT_COPY_FAILED_EVENT = "INPUT_COPY_FAILED"
INLINE_SEQUENCE_INVALID_EVENT = "INLINE_SEQUENCE_INVALID"
NCBI_URL_UNSUPPORTED_EVENT = "NCBI_URL_UNSUPPORTED"
NCBI_ACCESSION_INVALID_EVENT = "NCBI_ACCESSION_INVALID"
NCBI_RECORD_NOT_FOUND_EVENT = "NCBI_RECORD_NOT_FOUND"
NCBI_REQUEST_FAILED_EVENT = "NCBI_REQUEST_FAILED"
NCBI_REQUEST_TIMEOUT_EVENT = "NCBI_REQUEST_TIMEOUT"
NCBI_RESPONSE_EMPTY_EVENT = "NCBI_RESPONSE_EMPTY"
NCBI_RESPONSE_INVALID_EVENT = "NCBI_RESPONSE_INVALID"
NCBI_PARTIAL_RESPONSE_EVENT = "NCBI_PARTIAL_RESPONSE"

_INPUT_MANIFEST_SCHEMA_VERSION = 1
_INPUT_FILES_DIRECTORY = "inputs/files"
_INPUT_MANIFEST_PATH = "inputs/input_manifest.json"
_INLINE_HEADER_PREFIX = "jelica_inline_sequence"
_MAX_AGGREGATED_INLINE_ITEMS = 10
_MAX_AGGREGATED_COLLAPSED_ITEMS = 5
_NCBI_BATCH_SIZE = 100
_NCBI_REQUEST_TIMEOUT_SECONDS = 20.0
_NCBI_TOOL_NAME = "JELICA"
_NCBI_CONTACT_EMAIL = "rogovgreg@gmail.com"


class InputAcquisitionError(RuntimeError):
    def __init__(
        self,
        *,
        event_name: str,
        detail: str,
        context: dict[str, object] | None = None,
    ) -> None:
        self.event_name = event_name
        self.detail = detail
        self.context = context or {}
        super().__init__(detail)


@dataclass(frozen=True, slots=True)
class ParsedGenBankRecord:
    resolved_accession: str
    payload: str


class NCBINucleotideClient(Protocol):
    def fetch_nucleotide_genbank(
        self,
        *,
        accessions: tuple[str, ...],
        api_key: str,
        max_retries: int,
        timeout_seconds: float,
    ) -> str: ...


class HttpNCBINucleotideClient:
    _BASE_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"

    def fetch_nucleotide_genbank(
        self,
        *,
        accessions: tuple[str, ...],
        api_key: str,
        max_retries: int,
        timeout_seconds: float,
    ) -> str:
        if len(accessions) == 0:
            return ""

        query: dict[str, str] = {
            "db": "nuccore",
            "id": ",".join(accessions),
            "rettype": "gbwithparts",
            "retmode": "text",
            "tool": _NCBI_TOOL_NAME,
            "email": _NCBI_CONTACT_EMAIL,
        }
        if api_key.strip() != "":
            query["api_key"] = api_key.strip()
        request_url = f"{self._BASE_URL}?{urlencode(query)}"
        request = Request(request_url, headers={"Accept": "text/plain"})

        attempts = max_retries + 1
        for attempt in range(attempts):
            last_attempt = attempt == attempts - 1
            try:
                with urlopen(request, timeout=timeout_seconds) as response:
                    status_code = response.getcode()
                    payload = response.read().decode("utf-8", errors="replace")
                if status_code == 429 or 500 <= status_code <= 599:
                    if last_attempt:
                        raise InputAcquisitionError(
                            event_name=NCBI_REQUEST_FAILED_EVENT,
                            detail=f"NCBI request failed with HTTP status {status_code}.",
                            context={"http_status": status_code},
                        )
                    continue
                if status_code >= 400:
                    raise InputAcquisitionError(
                        event_name=NCBI_REQUEST_FAILED_EVENT,
                        detail=f"NCBI request failed with HTTP status {status_code}.",
                        context={"http_status": status_code},
                    )
                return payload
            except InputAcquisitionError:
                raise
            except HTTPError as error:
                if _is_transient_http_error(error.code):
                    if last_attempt:
                        raise InputAcquisitionError(
                            event_name=NCBI_REQUEST_FAILED_EVENT,
                            detail=f"NCBI request failed with HTTP status {error.code}.",
                            context={"http_status": error.code},
                        ) from error
                    continue
                raise InputAcquisitionError(
                    event_name=NCBI_REQUEST_FAILED_EVENT,
                    detail=f"NCBI request failed with HTTP status {error.code}.",
                    context={"http_status": error.code},
                ) from error
            except TimeoutError as error:
                if last_attempt:
                    raise InputAcquisitionError(
                        event_name=NCBI_REQUEST_TIMEOUT_EVENT,
                        detail="NCBI request timed out.",
                    ) from error
                continue
            except URLError as error:
                if _is_timeout_error(error.reason):
                    if last_attempt:
                        raise InputAcquisitionError(
                            event_name=NCBI_REQUEST_TIMEOUT_EVENT,
                            detail="NCBI request timed out.",
                        ) from error
                    continue
                if _is_transient_network_error(error.reason):
                    if last_attempt:
                        raise InputAcquisitionError(
                            event_name=NCBI_REQUEST_FAILED_EVENT,
                            detail="NCBI request failed due to a transient network error.",
                        ) from error
                    continue
                raise InputAcquisitionError(
                    event_name=NCBI_REQUEST_FAILED_EVENT,
                    detail=f"NCBI request failed: {error.reason}",
                ) from error

        raise InputAcquisitionError(
            event_name=NCBI_REQUEST_FAILED_EVENT,
            detail="NCBI request failed after retries.",
        )


@dataclass(frozen=True, slots=True)
class InputAcquisitionStage:
    stage_id: str = "input_acquisition"
    weight: float = 1.0
    ncbi_client: NCBINucleotideClient | None = None

    def preflight(self, context: StageContext) -> None:
        context.stage_staging_directory.mkdir(parents=True, exist_ok=True)

    def run(self, context: StageContext, progress_reporter: ProgressReporter) -> StageRunResult:
        config_document = _read_json_object(context.launch_spec.config_revision_path)
        samples = _extract_samples(config_document)
        input_directory_max_depth = _extract_non_negative_int(
            config_document=config_document,
            key="input_directory_max_depth",
            default_value=3,
        )
        ncbi_max_retries = _extract_non_negative_int(
            config_document=config_document,
            key="ncbi_max_retries",
            default_value=3,
        )
        session = _InputAcquisitionSession(
            context=context,
            progress_reporter=progress_reporter,
            samples=samples,
            input_directory_max_depth=input_directory_max_depth,
            ncbi_max_retries=ncbi_max_retries,
            ncbi_api_key=context.launch_spec.ncbi_api_key.strip(),
            ncbi_client=self.ncbi_client or HttpNCBINucleotideClient(),
        )
        session.execute()
        return StageRunResult(artifacts=(_INPUT_MANIFEST_PATH,))


@dataclass(slots=True)
class _MaterializedFile:
    relative_path: str
    source_type: str
    source_reference: str
    format_hint: str
    size_bytes: int
    sha256: str
    source_path: str | None = None
    requested_accession: str | None = None
    resolved_accession: str | None = None
    inline_length: int | None = None


@dataclass(slots=True)
class _DirectoryScanResult:
    encountered_any_entries: bool
    supported_files: list[tuple[str, Path]]
    unsupported_files: list[str]
    symlinks: list[str]
    depth_limited_directories: list[str]


@dataclass(slots=True)
class _NcbiRequestItem:
    source_kind: InputSourceKind
    source_reference: str
    accession: str


class _InputAcquisitionSession:
    def __init__(
        self,
        *,
        context: StageContext,
        progress_reporter: ProgressReporter,
        samples: list[str],
        input_directory_max_depth: int,
        ncbi_max_retries: int,
        ncbi_api_key: str,
        ncbi_client: NCBINucleotideClient,
    ) -> None:
        self._context = context
        self._progress_reporter = progress_reporter
        self._samples = samples
        self._input_directory_max_depth = input_directory_max_depth
        self._ncbi_max_retries = ncbi_max_retries
        self._ncbi_api_key = ncbi_api_key
        self._ncbi_client = ncbi_client
        self._inputs_root = context.stage_staging_directory / "inputs"
        self._files_root = self._inputs_root / "files"
        self._materialized_files: list[_MaterializedFile] = []
        self._manifest_sources: list[dict[str, object]] = []
        self._duplicate_items: list[str] = []
        self._seen_local_files: set[str] = set()
        self._seen_local_directories: set[str] = set()
        self._seen_accessions: set[str] = set()
        self._ncbi_requests: list[_NcbiRequestItem] = []
        self._inline_sequences: list[InputSourceClassification] = []
        self._non_fatal_source_errors: list[dict[str, object]] = []

    def execute(self) -> None:
        self._files_root.mkdir(parents=True, exist_ok=True)
        self._progress_reporter(0.05)

        for source_index, source in enumerate(self._samples):
            self._process_source(source_index=source_index, source=source)

        self._progress_reporter(0.45)
        self._materialize_ncbi_records()
        self._progress_reporter(0.8)
        self._materialize_inline_sequences()
        self._progress_reporter(0.9)

        if len(self._duplicate_items) > 0:
            sorted_duplicates = sorted(set(self._duplicate_items), key=str.casefold)
            self._emit_aggregated_warning(
                event_name=INPUT_DUPLICATES_SKIPPED_EVENT,
                source_directory=None,
                items=sorted_duplicates,
                item_label="duplicate sources",
            )

        if len(self._materialized_files) == 0:
            raise InputAcquisitionError(
                event_name=INPUT_NO_DATA_ACQUIRED_EVENT,
                detail="No input data were acquired from provided sources.",
                context={"sources_count": len(self._samples)},
            )

        manifest_path = self._write_manifest()
        self._emit_acquisition_completed(manifest_path=manifest_path)
        self._progress_reporter(1.0)

    def _process_source(self, *, source_index: int, source: str) -> None:
        classification = classify_input_source(source)
        source_payload: dict[str, object] = {
            "index": source_index,
            "source_type": classification.kind.value,
        }

        if classification.kind is InputSourceKind.LOCAL_PATH:
            assert classification.local_path is not None
            source_payload["source"] = str(classification.local_path)
            self._process_local_path(classification=classification, source_payload=source_payload)
            return

        if classification.kind is InputSourceKind.NCBI_NUCLEOTIDE_URL:
            if classification.accession is None:
                raise InputAcquisitionError(
                    event_name=NCBI_URL_UNSUPPORTED_EVENT,
                    detail=f"Unsupported NCBI URL source: {source}",
                    context={"source": source},
                )
            source_payload["source"] = classification.normalized
            source_payload["requested_accession"] = classification.accession
            self._register_ncbi_source(
                source_kind=classification.kind,
                source_reference=classification.normalized,
                accession=classification.accession,
                source_payload=source_payload,
            )
            return

        if classification.kind is InputSourceKind.NCBI_NUCLEOTIDE_ACCESSION:
            accession = classification.accession
            if accession is None:
                raise InputAcquisitionError(
                    event_name=NCBI_ACCESSION_INVALID_EVENT,
                    detail=f"NCBI accession is invalid: {source}",
                    context={"source": source},
                )
            source_payload["source"] = classification.normalized
            source_payload["requested_accession"] = accession
            self._register_ncbi_source(
                source_kind=classification.kind,
                source_reference=classification.normalized,
                accession=accession,
                source_payload=source_payload,
            )
            return

        if classification.kind is InputSourceKind.INLINE_SEQUENCE:
            if classification.inline_sequence is None or classification.inline_length is None:
                raise InputAcquisitionError(
                    event_name=INLINE_SEQUENCE_INVALID_EVENT,
                    detail="Inline sequence source is invalid.",
                )
            source_payload["source"] = {
                "type": "inline_sequence",
                "length": classification.inline_length,
                "sha256": _sha256_text(classification.inline_sequence),
            }
            self._manifest_sources.append(source_payload)
            self._inline_sequences.append(classification)
            return

        if classification.kind is not InputSourceKind.UNSUPPORTED:
            raise InputAcquisitionError(
                event_name=INPUT_SOURCE_UNSUPPORTED_EVENT,
                detail=f"Unsupported input source: {source}",
                context={"source": source},
            )

        if looks_like_local_path(source):
            raise InputAcquisitionError(
                event_name=INPUT_PATH_NOT_FOUND_EVENT,
                detail=f"Input path was not found: {source}",
                context={"source": source},
            )
        if source.startswith(("http://", "https://")):
            raise InputAcquisitionError(
                event_name=NCBI_URL_UNSUPPORTED_EVENT,
                detail=f"Unsupported NCBI URL source: {source}",
                context={"source": source},
            )
        if looks_like_ncbi_accession(source):
            raise InputAcquisitionError(
                event_name=NCBI_ACCESSION_INVALID_EVENT,
                detail=f"NCBI accession is invalid: {source}",
                context={"source": source},
            )
        if normalize_ncbi_accession(source) is None:
            inline_candidate = normalize_inline_sequence(source)
            if inline_candidate is None:
                raise InputAcquisitionError(
                    event_name=INPUT_SOURCE_UNSUPPORTED_EVENT,
                    detail=f"Unsupported input source: {source}",
                    context={"source": source},
                )
        raise InputAcquisitionError(
            event_name=INPUT_SOURCE_UNSUPPORTED_EVENT,
            detail=f"Unsupported input source: {source}",
            context={"source": source},
        )

    def _process_local_path(
        self,
        *,
        classification: InputSourceClassification,
        source_payload: dict[str, object],
    ) -> None:
        local_path = classification.local_path
        if local_path is None:
            raise InputAcquisitionError(
                event_name=INPUT_PATH_NOT_FOUND_EVENT,
                detail=f"Input path was not found: {classification.original}",
            )
        if not local_path.exists():
            raise InputAcquisitionError(
                event_name=INPUT_PATH_NOT_FOUND_EVENT,
                detail=f"Input path was not found: {classification.original}",
                context={"source": classification.original},
            )
        if local_path.is_symlink():
            raise InputAcquisitionError(
                event_name=INPUT_SYMLINK_UNSUPPORTED_EVENT,
                detail=f"Symbolic links are not supported as explicit input sources: {local_path}",
                context={"source": str(local_path)},
            )
        if local_path.is_file():
            canonical_file = str(local_path.resolve(strict=True))
            if canonical_file in self._seen_local_files:
                self._duplicate_items.append(str(local_path))
                return
            self._seen_local_files.add(canonical_file)
            self._materialize_local_file(
                source_file=local_path,
                source_kind="local_file",
                source_reference=str(local_path),
                source_path=str(local_path),
            )
            self._manifest_sources.append(source_payload)
            return
        if local_path.is_dir():
            canonical_directory = str(local_path.resolve(strict=True))
            if canonical_directory in self._seen_local_directories:
                self._duplicate_items.append(str(local_path))
                return
            self._seen_local_directories.add(canonical_directory)
            self._manifest_sources.append(source_payload)
            self._process_directory(local_path)
            return
        raise InputAcquisitionError(
            event_name=INPUT_FILE_UNREADABLE_EVENT,
            detail=f"Input source is not a regular file or directory: {local_path}",
            context={"source": str(local_path)},
        )

    def _process_directory(self, directory: Path) -> None:
        scan = self._scan_directory(directory)
        if len(scan.symlinks) > 0:
            self._emit_aggregated_warning(
                event_name=INPUT_SYMLINKS_SKIPPED_EVENT,
                source_directory=directory,
                items=sorted(scan.symlinks, key=str.casefold),
                item_label="symbolic links",
            )
        if len(scan.depth_limited_directories) > 0:
            message = (
                "Directory depth limit was reached while scanning input directory. "
                "Pass nested directories explicitly or increase input_directory_max_depth."
            )
            self._emit_aggregated_warning(
                event_name=INPUT_DIRECTORY_DEPTH_LIMIT_REACHED_EVENT,
                source_directory=directory,
                items=sorted(scan.depth_limited_directories, key=str.casefold),
                item_label="directories",
                prefix=message,
            )

        if not scan.encountered_any_entries:
            message = f"Input directory is empty and provided no input files: {directory}."
            self._record_non_fatal_source_error(
                event_name=INPUT_DIRECTORY_EMPTY_EVENT,
                detail=message,
                source=str(directory),
            )
            return

        if len(scan.supported_files) == 0:
            supported = ", ".join(SUPPORTED_INPUT_EXTENSIONS)
            message = (
                f"Input directory contains no supported input files: {directory}. "
                f"Supported extensions: {supported}."
            )
            self._record_non_fatal_source_error(
                event_name=INPUT_DIRECTORY_NO_SUPPORTED_FILES_EVENT,
                detail=message,
                source=str(directory),
            )
            return

        if len(scan.unsupported_files) > 0:
            self._emit_aggregated_warning(
                event_name=INPUT_UNSUPPORTED_FILES_SKIPPED_EVENT,
                source_directory=directory,
                items=sorted(scan.unsupported_files, key=str.casefold),
                item_label="unsupported files",
            )

        for relative_path, file_path in scan.supported_files:
            canonical_file = str(file_path.resolve(strict=True))
            if canonical_file in self._seen_local_files:
                self._duplicate_items.append(relative_path)
                continue
            self._seen_local_files.add(canonical_file)
            self._materialize_local_file(
                source_file=file_path,
                source_kind="local_directory_file",
                source_reference=relative_path,
                source_path=str(file_path),
            )

    def _scan_directory(self, directory: Path) -> _DirectoryScanResult:
        supported_files: list[tuple[str, Path]] = []
        unsupported_files: list[str] = []
        symlinks: list[str] = []
        depth_limited_directories: list[str] = []
        encountered_any_entries = False

        def _walk(current: Path, *, depth: int) -> None:
            nonlocal encountered_any_entries
            entries = sorted(current.iterdir(), key=lambda item: item.name.casefold())
            if len(entries) > 0:
                encountered_any_entries = True
            for entry in entries:
                relative_path = entry.relative_to(directory).as_posix()
                if entry.is_symlink():
                    symlinks.append(relative_path)
                    continue
                if entry.is_dir():
                    if depth >= self._input_directory_max_depth:
                        depth_limited_directories.append(relative_path)
                        continue
                    _walk(entry, depth=depth + 1)
                    continue
                if not entry.is_file():
                    unsupported_files.append(relative_path)
                    continue
                extension = normalized_input_extension(entry)
                if extension is None:
                    unsupported_files.append(relative_path)
                    continue
                supported_files.append((relative_path, entry))

        _walk(directory, depth=0)
        supported_files.sort(key=lambda item: item[0].casefold())
        return _DirectoryScanResult(
            encountered_any_entries=encountered_any_entries,
            supported_files=supported_files,
            unsupported_files=unsupported_files,
            symlinks=symlinks,
            depth_limited_directories=depth_limited_directories,
        )

    def _register_ncbi_source(
        self,
        *,
        source_kind: InputSourceKind,
        source_reference: str,
        accession: str,
        source_payload: dict[str, object],
    ) -> None:
        if accession in self._seen_accessions:
            self._duplicate_items.append(source_reference)
            return
        self._seen_accessions.add(accession)
        self._manifest_sources.append(source_payload)
        self._ncbi_requests.append(
            _NcbiRequestItem(
                source_kind=source_kind,
                source_reference=source_reference,
                accession=accession,
            )
        )

    def _materialize_ncbi_records(self) -> None:
        if len(self._ncbi_requests) == 0:
            return
        ordered_accessions = [item.accession for item in self._ncbi_requests]
        for offset in range(0, len(ordered_accessions), _NCBI_BATCH_SIZE):
            batch = tuple(ordered_accessions[offset : offset + _NCBI_BATCH_SIZE])
            payload = self._ncbi_client.fetch_nucleotide_genbank(
                accessions=batch,
                api_key=self._ncbi_api_key,
                max_retries=self._ncbi_max_retries,
                timeout_seconds=_NCBI_REQUEST_TIMEOUT_SECONDS,
            )
            records = _parse_genbank_records(payload)
            self._materialize_ncbi_batch(batch=batch, records=records)

    def _materialize_ncbi_batch(
        self,
        *,
        batch: tuple[str, ...],
        records: list[ParsedGenBankRecord],
    ) -> None:
        if len(records) == 0:
            if len(batch) == 1:
                raise InputAcquisitionError(
                    event_name=NCBI_RECORD_NOT_FOUND_EVENT,
                    detail=f"NCBI record was not found for accession '{batch[0]}'.",
                    context={"accession": batch[0]},
                )
            raise InputAcquisitionError(
                event_name=NCBI_RESPONSE_EMPTY_EVENT,
                detail="NCBI response is empty for requested accession batch.",
                context={"requested_accessions": list(batch)},
            )

        remaining_records = list(records)
        missing: list[str] = []
        for accession in batch:
            matched = _pop_matching_record(remaining_records=remaining_records, accession=accession)
            if matched is None:
                missing.append(accession)
                continue
            filename = f"{matched.resolved_accession}.gb"
            destination = self._next_materialized_path(filename)
            payload = matched.payload if matched.payload.endswith("\n") else f"{matched.payload}\n"
            write_text_atomically(path=destination, payload=payload)
            size_bytes = destination.stat().st_size
            if size_bytes <= 0:
                raise InputAcquisitionError(
                    event_name=NCBI_RESPONSE_INVALID_EVENT,
                    detail=f"NCBI response for accession '{accession}' produced an empty record.",
                    context={"accession": accession},
                )
            sha256 = _sha256_file(destination)
            self._materialized_files.append(
                _MaterializedFile(
                    relative_path=destination.relative_to(
                        self._context.stage_staging_directory
                    ).as_posix(),
                    source_type="ncbi_nucleotide_record",
                    source_reference=accession,
                    format_hint=".gb",
                    size_bytes=size_bytes,
                    sha256=sha256,
                    requested_accession=accession,
                    resolved_accession=matched.resolved_accession,
                )
            )

        if len(missing) > 0:
            if len(batch) == 1:
                raise InputAcquisitionError(
                    event_name=NCBI_RECORD_NOT_FOUND_EVENT,
                    detail=f"NCBI record was not found for accession '{missing[0]}'.",
                    context={"accession": missing[0]},
                )
            raise InputAcquisitionError(
                event_name=NCBI_PARTIAL_RESPONSE_EVENT,
                detail=(
                    "NCBI response is partial and did not include all requested accessions: "
                    f"{', '.join(sorted(missing, key=str.casefold))}."
                ),
                context={
                    "requested_accessions": list(batch),
                    "missing_accessions": sorted(missing, key=str.casefold),
                },
            )

    def _materialize_inline_sequences(self) -> None:
        for inline_index, classification in enumerate(self._inline_sequences, start=1):
            if classification.inline_sequence is None:
                raise InputAcquisitionError(
                    event_name=INLINE_SEQUENCE_INVALID_EVENT,
                    detail="Inline sequence source is invalid.",
                )
            normalized_inline = normalize_inline_sequence(classification.inline_sequence)
            if normalized_inline is None:
                raise InputAcquisitionError(
                    event_name=INLINE_SEQUENCE_INVALID_EVENT,
                    detail="Inline sequence source is invalid.",
                )
            destination = self._next_materialized_path("inline_sequence.fasta")
            header = f">{_INLINE_HEADER_PREFIX}_{inline_index:04d}"
            payload = f"{header}\n{normalized_inline}\n"
            write_text_atomically(path=destination, payload=payload)
            size_bytes = destination.stat().st_size
            if size_bytes <= 0:
                raise InputAcquisitionError(
                    event_name=INPUT_COPY_FAILED_EVENT,
                    detail="Failed to materialize inline sequence FASTA file.",
                )
            self._materialized_files.append(
                _MaterializedFile(
                    relative_path=destination.relative_to(
                        self._context.stage_staging_directory
                    ).as_posix(),
                    source_type="inline_sequence",
                    source_reference="inline_sequence",
                    format_hint=".fasta",
                    size_bytes=size_bytes,
                    sha256=_sha256_file(destination),
                    inline_length=len(normalized_inline),
                )
            )

    def _materialize_local_file(
        self,
        *,
        source_file: Path,
        source_kind: str,
        source_reference: str,
        source_path: str,
    ) -> None:
        if source_file.is_symlink():
            raise InputAcquisitionError(
                event_name=INPUT_SYMLINK_UNSUPPORTED_EVENT,
                detail=f"Symbolic links are not supported as input files: {source_file}",
                context={"source": str(source_file)},
            )
        if not source_file.is_file():
            raise InputAcquisitionError(
                event_name=INPUT_FILE_UNREADABLE_EVENT,
                detail=f"Input source is not a regular file: {source_file}",
                context={"source": str(source_file)},
            )
        extension = normalized_input_extension(source_file)
        if extension is None:
            raise InputAcquisitionError(
                event_name=INPUT_FILE_TYPE_UNSUPPORTED_EVENT,
                detail=f"Unsupported input file extension: {source_file.name}",
                context={"source": str(source_file)},
            )
        try:
            source_size = source_file.stat().st_size
        except OSError as error:
            raise InputAcquisitionError(
                event_name=INPUT_FILE_UNREADABLE_EVENT,
                detail=f"Input file is unreadable: {source_file} ({error})",
                context={"source": str(source_file)},
            ) from error
        if source_size <= 0:
            raise InputAcquisitionError(
                event_name=INPUT_FILE_EMPTY_EVENT,
                detail=f"Input file is empty: {source_file}",
                context={"source": str(source_file)},
            )
        try:
            with source_file.open("rb") as sample_file:
                sample_file.read(1)
        except OSError as error:
            raise InputAcquisitionError(
                event_name=INPUT_FILE_UNREADABLE_EVENT,
                detail=f"Input file is unreadable: {source_file} ({error})",
                context={"source": str(source_file)},
            ) from error

        source_sha256 = _sha256_file(source_file)
        destination = self._next_materialized_path(source_file.name)
        try:
            shutil.copyfile(source_file, destination)
        except OSError as error:
            raise InputAcquisitionError(
                event_name=INPUT_COPY_FAILED_EVENT,
                detail=f"Failed to copy input file '{source_file}' to working directory: {error}",
                context={"source": str(source_file)},
            ) from error

        destination_size = destination.stat().st_size
        destination_sha256 = _sha256_file(destination)
        if destination_size != source_size or destination_sha256 != source_sha256:
            raise InputAcquisitionError(
                event_name=INPUT_COPY_FAILED_EVENT,
                detail=(
                    "Copied file integrity check failed: "
                    f"source='{source_file}', destination='{destination}'."
                ),
                context={"source": str(source_file)},
            )

        self._materialized_files.append(
            _MaterializedFile(
                relative_path=destination.relative_to(
                    self._context.stage_staging_directory
                ).as_posix(),
                source_type=source_kind,
                source_reference=source_reference,
                source_path=source_path,
                format_hint=extension,
                size_bytes=destination_size,
                sha256=destination_sha256,
            )
        )

    def _next_materialized_path(self, source_name: str) -> Path:
        sequence = len(self._materialized_files) + 1
        sanitized_name = _sanitize_filename(source_name)
        filename = f"{sequence:04d}_{sanitized_name}"
        return self._files_root / filename

    def _record_non_fatal_source_error(self, *, event_name: str, detail: str, source: str) -> None:
        self._non_fatal_source_errors.append(
            {"event_name": event_name, "detail": detail, "source": source}
        )
        self._context.emit_event(event_name, {"detail": detail, "source": source})

    def _emit_aggregated_warning(
        self,
        *,
        event_name: str,
        source_directory: Path | None,
        items: list[str],
        item_label: str,
        prefix: str | None = None,
    ) -> None:
        if len(items) == 0:
            return
        shown_items, hidden_count = _aggregated_items(items)
        detail = _format_aggregated_message(
            total_count=len(items),
            shown_items=shown_items,
            hidden_count=hidden_count,
            item_label=item_label,
            prefix=prefix,
        )
        context: dict[str, object] = {
            "detail": detail,
            "total_count": len(items),
            "shown_relative_paths": shown_items,
            "hidden_count": hidden_count,
            "source_directory": str(source_directory) if source_directory is not None else None,
        }
        self._context.emit_event(event_name, context)

    def _emit_acquisition_completed(self, *, manifest_path: Path) -> None:
        materialized_paths = [item.relative_path for item in self._materialized_files]
        shown_paths: list[str]
        hidden_count: int
        if len(materialized_paths) <= _MAX_AGGREGATED_INLINE_ITEMS:
            shown_paths = materialized_paths
            hidden_count = 0
        else:
            shown_paths = materialized_paths[:_MAX_AGGREGATED_COLLAPSED_ITEMS]
            hidden_count = len(materialized_paths) - len(shown_paths)

        duplicates_skipped_count = len(set(self._duplicate_items))
        local_files_count = sum(
            1
            for item in self._materialized_files
            if item.source_type in {"local_file", "local_directory_file"}
        )
        ncbi_records_count = sum(
            1 for item in self._materialized_files if item.source_type == "ncbi_nucleotide_record"
        )
        inline_sequences_count = sum(
            1 for item in self._materialized_files if item.source_type == "inline_sequence"
        )
        materialized_files_count = len(self._materialized_files)
        context: dict[str, object] = {
            "task_id": self._context.launch_spec.task_id,
            "job_id": self._context.launch_spec.job_id,
            "stage_id": "input_acquisition",
            "provided_sources_count": len(self._samples),
            "unique_sources_count": len(self._manifest_sources),
            "materialized_files_count": materialized_files_count,
            "local_files_count": local_files_count,
            "ncbi_records_count": ncbi_records_count,
            "inline_sequences_count": inline_sequences_count,
            "duplicates_skipped_count": duplicates_skipped_count,
            "manifest_path": manifest_path.relative_to(
                self._context.stage_staging_directory
            ).as_posix(),
            "materialized_paths": shown_paths,
            "hidden_count": hidden_count,
        }
        duplicates_detail = ""
        if duplicates_skipped_count > 0:
            duplicates_noun = "source was" if duplicates_skipped_count == 1 else "sources were"
            duplicates_detail = f"; {duplicates_skipped_count} duplicate {duplicates_noun} skipped"
        files_noun = "file was" if materialized_files_count == 1 else "files were"
        context["detail"] = (
            "Input acquisition completed: "
            f"{materialized_files_count} input {files_noun} materialized"
            f"{duplicates_detail}."
        )
        self._context.emit_event(INPUT_ACQUISITION_COMPLETED_EVENT, context)

    def _write_manifest(self) -> Path:
        manifest_path = self._context.stage_staging_directory / _INPUT_MANIFEST_PATH
        manifest_payload = {
            "schema_version": _INPUT_MANIFEST_SCHEMA_VERSION,
            "task_id": self._context.launch_spec.task_id,
            "job_id": self._context.launch_spec.job_id,
            "config_revision_path": str(self._context.launch_spec.config_revision_path),
            "config_hash": self._context.launch_spec.config_hash,
            "generated_at": serialize_utc_datetime(utc_now()),
            "sources": self._manifest_sources,
            "materialized_files": [
                self._materialized_file_payload(item) for item in self._materialized_files
            ],
            "skipped_duplicates": sorted(set(self._duplicate_items), key=str.casefold),
            "source_errors": self._non_fatal_source_errors,
        }
        write_text_atomically(
            path=manifest_path,
            payload=json.dumps(
                manifest_payload,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n",
        )
        return manifest_path

    @staticmethod
    def _materialized_file_payload(item: _MaterializedFile) -> dict[str, object]:
        payload: dict[str, object] = {
            "relative_path": item.relative_path,
            "source_type": item.source_type,
            "source_reference": item.source_reference,
            "format_hint": item.format_hint,
            "size_bytes": item.size_bytes,
            "sha256": item.sha256,
        }
        if item.source_path is not None:
            payload["source_path"] = item.source_path
        if item.requested_accession is not None:
            payload["requested_accession"] = item.requested_accession
        if item.resolved_accession is not None:
            payload["resolved_accession"] = item.resolved_accession
        if item.inline_length is not None:
            payload["inline_length"] = item.inline_length
        return payload


def _read_json_object(path: Path) -> dict[str, object]:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"cannot read JSON config '{path}': {error}") from error
    if not isinstance(loaded, dict):
        raise RuntimeError(f"config '{path}' must be a JSON object")
    return {str(key): value for key, value in loaded.items()}


def _extract_samples(config_document: dict[str, object]) -> list[str]:
    raw_samples = config_document.get("samples")
    if not isinstance(raw_samples, list):
        raise RuntimeError("task config revision must contain 'samples' array")
    samples: list[str] = []
    for value in raw_samples:
        if value is None:
            continue
        if not isinstance(value, str):
            raise RuntimeError("task config revision samples must contain strings or nulls")
        normalized = value.strip()
        if normalized == "":
            continue
        samples.append(normalized)
    return samples


def _extract_non_negative_int(
    *,
    config_document: dict[str, object],
    key: str,
    default_value: int,
) -> int:
    raw_value = config_document.get(key, default_value)
    if type(raw_value) is not int:
        raise RuntimeError(f"task config revision field '{key}' must be an integer")
    if raw_value < 0:
        raise RuntimeError(f"task config revision field '{key}' must be >= 0")
    return raw_value


def _sha256_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sanitize_filename(filename: str) -> str:
    base_name = Path(filename).name.strip()
    if base_name == "":
        base_name = "input.dat"
    allowed = []
    for character in base_name:
        if character.isalnum() or character in {"-", "_", "."}:
            allowed.append(character)
        else:
            allowed.append("_")
    sanitized = "".join(allowed)
    if sanitized == "":
        return "input.dat"
    return sanitized


def _aggregated_items(items: list[str]) -> tuple[list[str], int]:
    if len(items) <= _MAX_AGGREGATED_INLINE_ITEMS:
        return items, 0
    shown = items[:_MAX_AGGREGATED_COLLAPSED_ITEMS]
    hidden_count = len(items) - len(shown)
    return shown, hidden_count


def _format_aggregated_message(
    *,
    total_count: int,
    shown_items: list[str],
    hidden_count: int,
    item_label: str,
    prefix: str | None,
) -> str:
    shown_text = ", ".join(shown_items)
    if hidden_count <= 0:
        message = f"{total_count} {item_label} were skipped: {shown_text}."
    else:
        message = (
            f"{total_count} {item_label} were skipped: {shown_text}, and {hidden_count} more files."
        )
    if prefix is None:
        return message
    return f"{prefix} {message}"


def _parse_genbank_records(payload: str) -> list[ParsedGenBankRecord]:
    if payload.strip() == "":
        return []
    lines = payload.splitlines()
    current: list[str] = []
    records: list[str] = []
    for line in lines:
        current.append(line)
        if line.strip() == "//":
            records.append("\n".join(current) + "\n")
            current = []
    if any(line.strip() != "" for line in current):
        raise InputAcquisitionError(
            event_name=NCBI_RESPONSE_INVALID_EVENT,
            detail="NCBI response contains an unterminated GenBank record.",
        )

    parsed: list[ParsedGenBankRecord] = []
    for record_payload in records:
        resolved_accession = _extract_resolved_accession_from_genbank(record_payload)
        if resolved_accession is None:
            raise InputAcquisitionError(
                event_name=NCBI_RESPONSE_INVALID_EVENT,
                detail="NCBI response contains a record without ACCESSION/VERSION fields.",
            )
        parsed.append(
            ParsedGenBankRecord(
                resolved_accession=resolved_accession,
                payload=record_payload,
            )
        )
    return parsed


def _extract_resolved_accession_from_genbank(payload: str) -> str | None:
    accession_fallback: str | None = None
    for line in payload.splitlines():
        if line.startswith("FEATURES") or line.startswith("ORIGIN"):
            break
        if line.startswith("VERSION"):
            tokens = line.split()
            if len(tokens) >= 2:
                candidate = normalize_ncbi_accession(tokens[1])
                if candidate is not None:
                    return candidate
        if line.startswith("ACCESSION"):
            tokens = line.split()
            if len(tokens) >= 2:
                candidate = normalize_ncbi_accession(tokens[1])
                if candidate is not None:
                    accession_fallback = candidate
    return accession_fallback


def _pop_matching_record(
    *,
    remaining_records: list[ParsedGenBankRecord],
    accession: str,
) -> ParsedGenBankRecord | None:
    requested = normalize_ncbi_accession(accession)
    if requested is None:
        return None
    requested_base = requested.split(".", maxsplit=1)[0]
    for index, record in enumerate(remaining_records):
        resolved = record.resolved_accession
        resolved_base = resolved.split(".", maxsplit=1)[0]
        if requested == resolved or requested_base == resolved_base:
            return remaining_records.pop(index)
    return None


def _is_timeout_error(reason: object) -> bool:
    return isinstance(reason, (socket.timeout, TimeoutError))


def _is_transient_http_error(status_code: int) -> bool:
    return status_code == 429 or 500 <= status_code <= 599


def _is_transient_network_error(reason: object) -> bool:
    if isinstance(reason, ConnectionError):
        return True
    if isinstance(reason, OSError):
        return True
    return False
