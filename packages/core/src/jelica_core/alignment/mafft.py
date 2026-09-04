from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import time
from pathlib import Path
from typing import Final

from jelica_core.config import (
    AnalysisAlignmentConstruction,
    AnalysisMafftDirectionAdjustment,
    AnalysisMafftMemoryMode,
    AnalysisMafftPhaseThreadMode,
    AnalysisMafftStrategy,
    AnalysisMafftThreadMode,
    ResolvedAnalysisMafftConfig,
)

from .models import (
    ALIGNMENT_DIAGNOSTICS_RELATIVE_PATH,
    AlignmentEngineRequest,
    AlignmentEngineResult,
    AlignmentToolAvailability,
)

MAFFT_EXECUTABLE_NAME: Final = "mafft"
MINIMUM_MAFFT_VERSION: Final = (6, 900)
_REFERENCE_GUIDED_MINIMUM_VERSION: Final = (6, 924)
_ITERATIVE_THREADS_MINIMUM_VERSION: Final = (7, 12)
_PROGRESSIVE_THREADS_MINIMUM_VERSION: Final = (7, 340)
_VERSION_PROBE_TIMEOUT_SECONDS: Final = 10.0
_PROCESS_POLL_INTERVAL_SECONDS: Final = 0.1
_PROCESS_TERMINATION_GRACE_SECONDS: Final = 2.0
_MAX_DIAGNOSTIC_BYTES: Final = 64 * 1024

_STRATEGY_ARGUMENTS: Final[dict[AnalysisMafftStrategy, tuple[str, ...]]] = {
    AnalysisMafftStrategy.AUTO: ("--auto",),
    AnalysisMafftStrategy.FFT_NS_1: ("--retree", "1", "--maxiterate", "0"),
    AnalysisMafftStrategy.FFT_NS_2: ("--retree", "2", "--maxiterate", "0"),
    AnalysisMafftStrategy.FFT_NS_I: ("--retree", "2", "--maxiterate", "1000"),
    AnalysisMafftStrategy.NW_NS_1: (
        "--retree",
        "1",
        "--maxiterate",
        "0",
        "--nofft",
    ),
    AnalysisMafftStrategy.NW_NS_2: (
        "--retree",
        "2",
        "--maxiterate",
        "0",
        "--nofft",
    ),
    AnalysisMafftStrategy.NW_NS_I: (
        "--retree",
        "2",
        "--maxiterate",
        "1000",
        "--nofft",
    ),
    AnalysisMafftStrategy.G_INS_I: ("--globalpair", "--maxiterate", "1000"),
    AnalysisMafftStrategy.L_INS_I: ("--localpair", "--maxiterate", "1000"),
    AnalysisMafftStrategy.E_INS_I: ("--genafpair", "--maxiterate", "1000"),
}


class MafftError(RuntimeError):
    def __init__(self, *, code: str, detail: str, exit_code: int | None = None) -> None:
        self.code = code
        self.detail = detail
        self.exit_code = exit_code
        super().__init__(detail)


class MafftAlignmentEngine:
    @property
    def name(self) -> str:
        return "mafft"

    def probe(self, *, explicit_executable: str | None = None) -> AlignmentToolAvailability:
        executable, source, resolution_error = _resolve_executable(explicit_executable)
        if executable is None:
            return AlignmentToolAvailability(
                available=False,
                executable=None,
                version=None,
                version_parts=None,
                source=source,
                error_code="mafft_not_found",
                reason=resolution_error or "MAFFT executable was not found.",
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
            return AlignmentToolAvailability(
                available=False,
                executable=executable,
                version=None,
                version_parts=None,
                source=source,
                error_code="mafft_version_probe_failed",
                reason=f"MAFFT version probe could not be executed ({type(error).__name__}).",
            )

        if completed.returncode != 0:
            return AlignmentToolAvailability(
                available=False,
                executable=executable,
                version=None,
                version_parts=None,
                source=source,
                error_code="mafft_version_probe_failed",
                reason=f"MAFFT version probe exited with code {completed.returncode}.",
            )
        probe_text = _decode_probe_output(completed.stdout, completed.stderr)
        parsed = parse_mafft_version(probe_text)
        if parsed is None:
            return AlignmentToolAvailability(
                available=False,
                executable=executable,
                version=None,
                version_parts=None,
                source=source,
                error_code="mafft_version_unrecognized",
                reason="MAFFT version output was not recognized.",
            )
        version, version_parts = parsed
        if not _version_at_least(version_parts, MINIMUM_MAFFT_VERSION):
            return AlignmentToolAvailability(
                available=False,
                executable=executable,
                version=version,
                version_parts=version_parts,
                source=source,
                error_code="mafft_version_unsupported",
                reason=(
                    "MAFFT version is older than the minimum supported version "
                    f"{_format_version(MINIMUM_MAFFT_VERSION)}."
                ),
            )
        return AlignmentToolAvailability(
            available=True,
            executable=executable,
            version=version,
            version_parts=version_parts,
            source=source,
        )

    def align(
        self,
        *,
        availability: AlignmentToolAvailability,
        request: AlignmentEngineRequest,
    ) -> AlignmentEngineResult:
        if not availability.available or availability.executable is None:
            raise MafftError(
                code="mafft_unavailable",
                detail=availability.reason or "MAFFT is unavailable.",
            )
        if availability.version is None or availability.version_parts is None:
            raise MafftError(
                code="mafft_version_unavailable",
                detail="MAFFT availability result does not contain a parsed version.",
            )
        if len(request.sequences) < 2:
            raise MafftError(
                code="mafft_input_count_invalid",
                detail="MAFFT requires at least two unique sequences in this stage.",
            )
        _validate_version_capabilities(
            version_parts=availability.version_parts,
            construction=request.construction,
            config=request.mafft_config,
        )

        run_directory = request.working_directory
        temp_directory = run_directory / "tmp"
        run_directory.mkdir(parents=True, exist_ok=True)
        temp_directory.mkdir(parents=True, exist_ok=True)
        ordered_sequences = _order_sequences(
            sequences=request.sequences,
            reference_sequence_id=request.reference_sequence_id,
        )
        internal_record_ids = {
            item.sequence_id: _internal_record_id(index=index, sequence_id=item.sequence_id)
            for index, item in enumerate(ordered_sequences, start=1)
        }
        option_arguments = build_mafft_option_arguments(request.mafft_config)
        input_arguments = _write_engine_inputs(
            run_directory=run_directory,
            sequences=ordered_sequences,
            internal_record_ids=internal_record_ids,
            construction=request.construction,
            reference_sequence_id=request.reference_sequence_id,
        )
        argv = [str(availability.executable), *option_arguments, *input_arguments]
        output_path = run_directory / "mafft-output.fasta"
        raw_stderr_path = run_directory / "mafft.stderr.raw"
        diagnostics_path = (
            run_directory.parent.parent / Path(ALIGNMENT_DIAGNOSTICS_RELATIVE_PATH)
        )
        diagnostics_path.parent.mkdir(parents=True, exist_ok=True)
        environment = dict(os.environ)
        environment["MAFFT_TMPDIR"] = str(temp_directory)

        process: subprocess.Popen[bytes] | None = None
        registered_process_id: int | None = None
        try:
            with output_path.open("wb") as output_handle, raw_stderr_path.open(
                "wb"
            ) as stderr_handle:
                if os.name == "nt":
                    process = subprocess.Popen(
                        argv,
                        stdin=subprocess.DEVNULL,
                        stdout=output_handle,
                        stderr=stderr_handle,
                        cwd=run_directory,
                        env=environment,
                        shell=False,
                        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
                    )
                else:
                    process = subprocess.Popen(
                        argv,
                        stdin=subprocess.DEVNULL,
                        stdout=output_handle,
                        stderr=stderr_handle,
                        cwd=run_directory,
                        env=environment,
                        shell=False,
                        start_new_session=True,
                    )
                registered_process_id = process.pid
                if request.process_started is not None:
                    request.process_started(registered_process_id)
                while True:
                    try:
                        return_code = process.wait(timeout=_PROCESS_POLL_INTERVAL_SECONDS)
                        break
                    except subprocess.TimeoutExpired:
                        if request.control_check is not None:
                            request.control_check()
        except BaseException as error:
            if process is not None and process.poll() is None:
                terminate_process_tree(process)
            if isinstance(error, (OSError, subprocess.SubprocessError)):
                raise MafftError(
                    code="mafft_launch_failed",
                    detail=f"MAFFT process could not be started ({type(error).__name__}).",
                ) from error
            raise
        finally:
            if registered_process_id is not None and request.process_stopped is not None:
                request.process_stopped(registered_process_id)
            if raw_stderr_path.is_file():
                _copy_limited_diagnostics(
                    source=raw_stderr_path,
                    destination=diagnostics_path,
                )

        if return_code != 0:
            raise MafftError(
                code="mafft_nonzero_exit",
                detail=(
                    f"MAFFT exited with code {return_code}; limited diagnostics are available "
                    "in the task stage directory."
                ),
                exit_code=return_code,
            )
        if not output_path.is_file() or output_path.stat().st_size == 0:
            raise MafftError(
                code="mafft_empty_output",
                detail="MAFFT completed without producing a non-empty alignment result.",
            )
        effective_arguments = (*option_arguments, *(_safe_construction_arguments(request)))
        return AlignmentEngineResult(
            output_path=output_path,
            diagnostics_path=diagnostics_path,
            version=availability.version,
            effective_arguments=effective_arguments,
            internal_record_ids=internal_record_ids,
            reverse_marked_record_ids=frozenset(),
        )


def parse_mafft_version(text: str) -> tuple[str, tuple[int, ...]] | None:
    patterns = (
        r"(?i)\bmafft\s+(?:version\s+)?v?(\d+\.\d+(?:\.\d+)*)",
        r"(?i)(?<![\d.])v(\d+\.\d+(?:\.\d+)*)",
    )
    for pattern in patterns:
        match = re.search(pattern, text)
        if match is None:
            continue
        version = match.group(1)
        return version, tuple(int(part) for part in version.split("."))
    return None


def build_mafft_option_arguments(config: ResolvedAnalysisMafftConfig) -> tuple[str, ...]:
    arguments: list[str] = ["--nuc", "--quiet"]
    arguments.extend(_STRATEGY_ARGUMENTS[config.strategy])
    if config.direction_adjustment is AnalysisMafftDirectionAdjustment.FAST:
        arguments.append("--adjustdirection")
    elif config.direction_adjustment is AnalysisMafftDirectionAdjustment.ACCURATE:
        arguments.append("--adjustdirectionaccurately")
    if config.memory_mode is AnalysisMafftMemoryMode.SAVE:
        arguments.append("--memsave")
    thread_value = -1 if config.threads is AnalysisMafftThreadMode.AUTO else config.threads
    arguments.extend(("--thread", str(thread_value)))
    if config.progressive_threads is AnalysisMafftPhaseThreadMode.DISABLED:
        arguments.extend(("--threadtb", "0"))
    elif isinstance(config.progressive_threads, int):
        arguments.extend(("--threadtb", str(config.progressive_threads)))
    if config.iterative_threads is AnalysisMafftPhaseThreadMode.DISABLED:
        arguments.extend(("--threadit", "0"))
    elif isinstance(config.iterative_threads, int):
        arguments.extend(("--threadit", str(config.iterative_threads)))
    if config.gap_open_penalty is not None:
        arguments.extend(("--op", _format_number(config.gap_open_penalty)))
    if config.offset is not None:
        arguments.extend(("--ep", _format_number(config.offset)))
    elif config.strategy is AnalysisMafftStrategy.E_INS_I:
        arguments.extend(("--ep", "0"))
    return tuple(arguments)


def terminate_process_tree(
    process: subprocess.Popen[bytes],
    *,
    grace_seconds: float = _PROCESS_TERMINATION_GRACE_SECONDS,
) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        _terminate_windows_tree(process=process, force=False)
    else:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except (OSError, ProcessLookupError):
            try:
                process.terminate()
            except OSError:
                pass
    try:
        process.wait(timeout=max(grace_seconds, 0.0))
        return
    except subprocess.TimeoutExpired:
        pass
    if os.name == "nt":
        _terminate_windows_tree(process=process, force=True)
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            try:
                process.kill()
            except OSError:
                pass
    try:
        process.wait(timeout=1.0)
    except subprocess.TimeoutExpired:
        pass


def terminate_process_tree_by_pid(
    pid: int,
    *,
    grace_seconds: float = _PROCESS_TERMINATION_GRACE_SECONDS,
) -> None:
    """Terminate a MAFFT process tree when only its process-group leader PID is known."""
    if pid <= 0 or not _process_tree_exists(pid):
        return
    if os.name == "nt":
        _terminate_windows_pid_tree(pid=pid, force=False)
    else:
        _signal_posix_process_tree(pid=pid, sig=signal.SIGTERM)
    if _wait_for_process_tree_exit(pid=pid, timeout_seconds=grace_seconds):
        return
    if os.name == "nt":
        _terminate_windows_pid_tree(pid=pid, force=True)
    else:
        _signal_posix_process_tree(pid=pid, sig=signal.SIGKILL)
    _wait_for_process_tree_exit(pid=pid, timeout_seconds=1.0)


def _resolve_executable(
    explicit_executable: str | None,
) -> tuple[Path | None, str, str | None]:
    if explicit_executable is not None:
        configured = explicit_executable.strip()
        if configured == "":
            return None, "system_config", "Configured MAFFT executable is empty."
        configured_path = Path(configured).expanduser()
        resolved: str | None
        if configured_path.is_absolute() or configured_path.parent != Path("."):
            resolved = str(configured_path)
        else:
            resolved = shutil.which(configured)
        if resolved is None:
            return None, "system_config", "Configured MAFFT executable was not found."
        path = Path(resolved).resolve(strict=False)
        if not path.is_file() or not os.access(path, os.X_OK):
            return None, "system_config", "Configured MAFFT executable is not executable."
        return path, "system_config", None

    found = shutil.which(MAFFT_EXECUTABLE_NAME)
    if found is None:
        return None, "PATH", "Command 'mafft' was not found in PATH."
    path = Path(found).resolve(strict=False)
    if not path.is_file() or not os.access(path, os.X_OK):
        return None, "PATH", "Command 'mafft' from PATH is not executable."
    return path, "PATH", None


def _decode_probe_output(stdout: bytes, stderr: bytes) -> str:
    return (stdout + b"\n" + stderr).decode("utf-8", errors="replace")[:16_384]


def _validate_version_capabilities(
    *,
    version_parts: tuple[int, ...],
    construction: AnalysisAlignmentConstruction,
    config: ResolvedAnalysisMafftConfig,
) -> None:
    if (
        construction is AnalysisAlignmentConstruction.REFERENCE_GUIDED
        and not _version_at_least(version_parts, _REFERENCE_GUIDED_MINIMUM_VERSION)
    ):
        raise MafftError(
            code="mafft_add_version_unsupported",
            detail=(
                "The installed MAFFT version is too old for safe reference-guided "
                "alignment with a single-sequence reference."
            ),
        )
    if (
        config.iterative_threads is not AnalysisMafftPhaseThreadMode.AUTO
        and not _version_at_least(version_parts, _ITERATIVE_THREADS_MINIMUM_VERSION)
    ):
        raise MafftError(
            code="mafft_threadit_version_unsupported",
            detail="The installed MAFFT version does not support iterative_threads.",
        )
    if (
        config.progressive_threads is not AnalysisMafftPhaseThreadMode.AUTO
        and not _version_at_least(version_parts, _PROGRESSIVE_THREADS_MINIMUM_VERSION)
    ):
        raise MafftError(
            code="mafft_threadtb_version_unsupported",
            detail="The installed MAFFT version does not support progressive_threads.",
        )


def _order_sequences(
    *,
    sequences: tuple,
    reference_sequence_id: str | None,
) -> tuple:
    if reference_sequence_id is None:
        return sequences
    matching = [item for item in sequences if item.sequence_id == reference_sequence_id]
    if len(matching) != 1:
        raise MafftError(
            code="mafft_reference_not_unique",
            detail="The resolved reference sequence is not present exactly once in engine input.",
        )
    return (matching[0], *(item for item in sequences if item.sequence_id != reference_sequence_id))


def _internal_record_id(*, index: int, sequence_id: str) -> str:
    digest = sequence_id.rsplit(":", maxsplit=1)[-1]
    safe_digest = re.sub(r"[^0-9a-f]", "", digest.lower())[:16]
    if safe_digest == "":
        safe_digest = "unknown"
    return f"jelica_seq_{index:06d}_{safe_digest}"


def _write_engine_inputs(
    *,
    run_directory: Path,
    sequences: tuple,
    internal_record_ids: dict[str, str],
    construction: AnalysisAlignmentConstruction,
    reference_sequence_id: str | None,
) -> tuple[str, ...]:
    if construction is AnalysisAlignmentConstruction.JOINT:
        input_path = run_directory / "unique-sequences.fasta"
        _write_fasta(
            path=input_path,
            records=tuple(
                (internal_record_ids[item.sequence_id], item.sequence) for item in sequences
            ),
        )
        return (str(input_path),)
    if reference_sequence_id is None:
        raise MafftError(
            code="mafft_reference_required",
            detail="Reference-guided alignment requires a resolved reference sequence.",
        )
    reference = [item for item in sequences if item.sequence_id == reference_sequence_id]
    additions = [item for item in sequences if item.sequence_id != reference_sequence_id]
    if len(reference) != 1 or len(additions) == 0:
        raise MafftError(
            code="mafft_reference_input_invalid",
            detail="Reference-guided engine input has an invalid reference/addition set.",
        )
    reference_path = run_directory / "reference.fasta"
    additions_path = run_directory / "additions.fasta"
    _write_fasta(
        path=reference_path,
        records=((internal_record_ids[reference[0].sequence_id], reference[0].sequence),),
    )
    _write_fasta(
        path=additions_path,
        records=tuple(
            (internal_record_ids[item.sequence_id], item.sequence) for item in additions
        ),
    )
    return ("--add", str(additions_path), str(reference_path))


def _write_fasta(*, path: Path, records: tuple[tuple[str, str], ...]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for record_id, sequence in records:
            handle.write(f">{record_id}\n")
            for start in range(0, len(sequence), 80):
                handle.write(sequence[start : start + 80])
                handle.write("\n")


def _safe_construction_arguments(request: AlignmentEngineRequest) -> tuple[str, ...]:
    if request.construction is AnalysisAlignmentConstruction.REFERENCE_GUIDED:
        return ("--add",)
    return tuple()


def _terminate_windows_tree(*, process: subprocess.Popen[bytes], force: bool) -> None:
    try:
        _terminate_windows_pid_tree(pid=process.pid, force=force)
    except (OSError, subprocess.SubprocessError):
        try:
            process.kill() if force else process.terminate()
        except OSError:
            pass


def _terminate_windows_pid_tree(*, pid: int, force: bool) -> None:
    command = ["taskkill", "/PID", str(pid), "/T"]
    if force:
        command.append("/F")
    subprocess.run(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
        timeout=2.0,
        shell=False,
    )


def _signal_posix_process_tree(*, pid: int, sig: signal.Signals) -> None:
    try:
        os.killpg(pid, sig)
    except (OSError, ProcessLookupError):
        try:
            os.kill(pid, sig)
        except OSError:
            pass


def _process_tree_exists(pid: int) -> bool:
    try:
        if os.name == "nt":
            os.kill(pid, 0)
        else:
            os.killpg(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _wait_for_process_tree_exit(*, pid: int, timeout_seconds: float) -> bool:
    deadline = time.monotonic() + max(timeout_seconds, 0.0)
    while _process_tree_exists(pid):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(_PROCESS_POLL_INTERVAL_SECONDS, remaining))
    return True


def _copy_limited_diagnostics(*, source: Path, destination: Path) -> None:
    try:
        size = source.stat().st_size
        with source.open("rb") as source_handle:
            if size > _MAX_DIAGNOSTIC_BYTES:
                source_handle.seek(size - _MAX_DIAGNOSTIC_BYTES)
            payload = source_handle.read(_MAX_DIAGNOSTIC_BYTES)
        with destination.open("wb") as destination_handle:
            if size > _MAX_DIAGNOSTIC_BYTES:
                destination_handle.write(b"[earlier MAFFT diagnostics truncated]\n")
            destination_handle.write(payload)
    except OSError:
        destination.unlink(missing_ok=True)


def _version_at_least(actual: tuple[int, ...], minimum: tuple[int, ...]) -> bool:
    width = max(len(actual), len(minimum))
    padded_actual = actual + (0,) * (width - len(actual))
    padded_minimum = minimum + (0,) * (width - len(minimum))
    return padded_actual >= padded_minimum


def _format_version(parts: tuple[int, ...]) -> str:
    return ".".join(str(part) for part in parts)


def _format_number(value: float) -> str:
    return format(value, ".15g")
