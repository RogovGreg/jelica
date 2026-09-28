from __future__ import annotations

import csv
import io
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from jelica_core.alignment.mafft import terminate_process_tree

NEXTCLADE_EXECUTABLE_NAME = "nextclade"
NEXTCLADE_PANGO_COLUMN = "Nextclade_pango"
NEXTCLADE_SEQUENCE_NAME_COLUMN = "seqName"
_VERSION_PROBE_TIMEOUT_SECONDS = 10.0
_PROCESS_POLL_INTERVAL_SECONDS = 0.1
_MAX_DIAGNOSTIC_BYTES = 64 * 1024
_MAX_RECORD_DIAGNOSTIC_LENGTH = 512


class NextcladeError(RuntimeError):
    def __init__(
        self,
        *,
        code: str,
        detail: str,
        exit_code: int | None = None,
        diagnostics: str | None = None,
    ) -> None:
        self.code = code
        self.detail = detail
        self.exit_code = exit_code
        self.diagnostics = diagnostics
        super().__init__(detail)


@dataclass(frozen=True, slots=True)
class NextcladeAvailability:
    available: bool
    executable: Path | None
    version: str | None
    source: str
    error_code: str | None = None
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class NextcladeRunResult:
    output_tsv_path: Path
    diagnostics: str


@dataclass(frozen=True, slots=True)
class NextcladePangoRecord:
    sequence_name: str
    lineage: str | None
    diagnostics: tuple[str, ...] = ()


class NextcladeRunner:
    @property
    def name(self) -> str:
        return "nextclade"

    def probe(self) -> NextcladeAvailability:
        found = shutil.which(NEXTCLADE_EXECUTABLE_NAME)
        if found is None:
            return NextcladeAvailability(
                available=False,
                executable=None,
                version=None,
                source="PATH",
                error_code="nextclade_not_found",
                reason="Command 'nextclade' was not found in PATH.",
            )
        executable = Path(found).resolve(strict=False)
        if not executable.is_file() or not os.access(executable, os.X_OK):
            return NextcladeAvailability(
                available=False,
                executable=None,
                version=None,
                source="PATH",
                error_code="nextclade_not_executable",
                reason="Command 'nextclade' from PATH is not executable.",
            )
        try:
            completed = subprocess.run(
                [str(executable), "--version"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=_VERSION_PROBE_TIMEOUT_SECONDS,
                shell=False,
            )
        except (OSError, subprocess.SubprocessError) as error:
            return NextcladeAvailability(
                available=False,
                executable=executable,
                version=None,
                source="PATH",
                error_code="nextclade_version_probe_failed",
                reason=f"Nextclade version probe could not be executed ({type(error).__name__}).",
            )
        if completed.returncode != 0:
            return NextcladeAvailability(
                available=False,
                executable=executable,
                version=None,
                source="PATH",
                error_code="nextclade_version_probe_failed",
                reason=f"Nextclade version probe exited with code {completed.returncode}.",
            )
        version = _decode_limited(completed.stdout + b"\n" + completed.stderr).strip()
        if version == "":
            return NextcladeAvailability(
                available=False,
                executable=executable,
                version=None,
                source="PATH",
                error_code="nextclade_version_unrecognized",
                reason="Nextclade version output was empty.",
            )
        return NextcladeAvailability(
            available=True,
            executable=executable,
            version=version.splitlines()[0].strip(),
            source="PATH",
        )

    def run(
        self,
        *,
        availability: NextcladeAvailability,
        dataset: Path,
        input_fasta: Path,
        output_tsv: Path,
        working_directory: Path,
        control_check: Callable[[], None] | None = None,
        process_started: Callable[[int], None] | None = None,
        process_stopped: Callable[[int], None] | None = None,
    ) -> NextcladeRunResult:
        if not availability.available or availability.executable is None:
            raise NextcladeError(
                code="nextclade_unavailable",
                detail=availability.reason or "Nextclade is unavailable.",
            )
        if not input_fasta.is_file() or input_fasta.is_symlink():
            raise NextcladeError(
                code="nextclade_input_missing",
                detail="Nextclade input FASTA is missing or invalid.",
            )
        working_directory.mkdir(parents=True, exist_ok=True)
        output_tsv.parent.mkdir(parents=True, exist_ok=True)
        stderr_path = working_directory / "nextclade.stderr.raw"
        stdout_path = working_directory / "nextclade.stdout.raw"
        argv = build_nextclade_argv(
            executable=availability.executable,
            dataset=dataset,
            output_tsv=output_tsv,
            input_fasta=input_fasta,
        )
        process: subprocess.Popen[bytes] | None = None
        registered_process_id: int | None = None
        try:
            with stdout_path.open("wb") as stdout_handle, stderr_path.open("wb") as stderr_handle:
                popen_options: dict[str, object] = {
                    "stdin": subprocess.DEVNULL,
                    "stdout": stdout_handle,
                    "stderr": stderr_handle,
                    "cwd": working_directory,
                    "shell": False,
                }
                if os.name == "nt":
                    popen_options["creationflags"] = getattr(
                        subprocess, "CREATE_NEW_PROCESS_GROUP", 0
                    )
                else:
                    popen_options["start_new_session"] = True
                process = subprocess.Popen(argv, **popen_options)
                registered_process_id = process.pid
                if process_started is not None:
                    process_started(registered_process_id)
                while True:
                    try:
                        return_code = process.wait(timeout=_PROCESS_POLL_INTERVAL_SECONDS)
                        break
                    except subprocess.TimeoutExpired:
                        if control_check is not None:
                            control_check()
        except BaseException as error:
            if process is not None and process.poll() is None:
                terminate_process_tree(process)
            if isinstance(error, (OSError, subprocess.SubprocessError)):
                raise NextcladeError(
                    code="nextclade_launch_failed",
                    detail=f"Nextclade process could not be started ({type(error).__name__}).",
                ) from error
            raise
        finally:
            if registered_process_id is not None and process_stopped is not None:
                process_stopped(registered_process_id)

        diagnostics = _read_limited_file(stderr_path)
        if return_code != 0:
            raise NextcladeError(
                code="nextclade_nonzero_exit",
                detail=f"Nextclade exited with code {return_code}.",
                exit_code=return_code,
                diagnostics=diagnostics,
            )
        if not output_tsv.is_file() or output_tsv.is_symlink() or output_tsv.stat().st_size == 0:
            raise NextcladeError(
                code="nextclade_output_missing",
                detail="Nextclade completed without producing a non-empty TSV result.",
                diagnostics=diagnostics,
            )
        return NextcladeRunResult(output_tsv_path=output_tsv, diagnostics=diagnostics)


def build_nextclade_argv(
    *,
    executable: Path,
    dataset: Path,
    output_tsv: Path,
    input_fasta: Path,
) -> tuple[str, ...]:
    return (
        str(executable),
        "run",
        "--input-dataset",
        str(dataset),
        "--output-tsv",
        str(output_tsv),
        str(input_fasta),
    )


def parse_nextclade_pango_tsv(payload: str) -> tuple[NextcladePangoRecord, ...]:
    reader = csv.DictReader(io.StringIO(payload, newline=""), delimiter="\t")
    headers = reader.fieldnames
    if headers is None:
        raise NextcladeError(
            code="nextclade_tsv_malformed",
            detail="Nextclade TSV is missing a header row.",
        )
    if len(headers) != len(set(headers)):
        raise NextcladeError(
            code="nextclade_tsv_malformed",
            detail="Nextclade TSV contains duplicate header columns.",
        )
    required = {NEXTCLADE_SEQUENCE_NAME_COLUMN, NEXTCLADE_PANGO_COLUMN}
    if not required.issubset(headers):
        raise NextcladeError(
            code="nextclade_pango_column_missing",
            detail="Nextclade TSV does not provide the required Nextclade_pango column.",
        )
    records: list[NextcladePangoRecord] = []
    for row in reader:
        if row is None or None in row:
            raise NextcladeError(
                code="nextclade_tsv_malformed",
                detail="Nextclade TSV contains an invalid row.",
            )
        raw_name = row.get(NEXTCLADE_SEQUENCE_NAME_COLUMN)
        if not isinstance(raw_name, str) or raw_name.strip() == "":
            raise NextcladeError(
                code="nextclade_tsv_malformed",
                detail="Nextclade TSV contains a row without seqName.",
            )
        raw_lineage = row.get(NEXTCLADE_PANGO_COLUMN)
        lineage = raw_lineage.strip() if isinstance(raw_lineage, str) else ""
        records.append(
            NextcladePangoRecord(
                sequence_name=raw_name.strip(),
                lineage=lineage or None,
                diagnostics=_record_diagnostics(row),
            )
        )
    return tuple(records)


def _record_diagnostics(row: dict[str | None, str | None]) -> tuple[str, ...]:
    diagnostics: list[str] = []
    for field_name in ("errors", "warnings"):
        value = row.get(field_name)
        if not isinstance(value, str):
            continue
        normalized = value.strip()
        if normalized == "":
            continue
        if len(normalized) > _MAX_RECORD_DIAGNOSTIC_LENGTH:
            normalized = normalized[:_MAX_RECORD_DIAGNOSTIC_LENGTH]
        diagnostics.append(f"{field_name}: {normalized}")
    return tuple(diagnostics)


def _decode_limited(value: bytes) -> str:
    return value[:_MAX_DIAGNOSTIC_BYTES].decode("utf-8", errors="replace")


def _read_limited_file(path: Path) -> str:
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            if size > _MAX_DIAGNOSTIC_BYTES:
                handle.seek(size - _MAX_DIAGNOSTIC_BYTES)
            payload = handle.read(_MAX_DIAGNOSTIC_BYTES)
    except OSError:
        return ""
    prefix = "[truncated]\n" if size > _MAX_DIAGNOSTIC_BYTES else ""
    return prefix + payload.decode("utf-8", errors="replace")


__all__ = [
    "NEXTCLADE_EXECUTABLE_NAME",
    "NEXTCLADE_PANGO_COLUMN",
    "NEXTCLADE_SEQUENCE_NAME_COLUMN",
    "NextcladeAvailability",
    "NextcladeError",
    "NextcladePangoRecord",
    "NextcladeRunResult",
    "NextcladeRunner",
    "build_nextclade_argv",
    "parse_nextclade_pango_tsv",
]
