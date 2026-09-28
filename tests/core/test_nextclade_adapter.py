from __future__ import annotations

import subprocess
from pathlib import Path
from typing import BinaryIO

import pytest

from jelica_core.lineage_detection import (
    NextcladeAvailability,
    NextcladeError,
    NextcladeRunner,
    build_nextclade_argv,
    parse_nextclade_pango_tsv,
)
from jelica_core.lineage_detection import nextclade as nextclade_module


class _FakeProcess:
    def __init__(
        self,
        *,
        stdout: BinaryIO,
        stderr: BinaryIO,
        diagnostics: bytes = b"",
        return_code: int = 0,
        wait_once: bool = False,
    ) -> None:
        self.pid = 24680
        self._running = True
        self._return_code = return_code
        self._wait_once = wait_once
        stdout.write(b"nextclade stdout")
        stderr.write(diagnostics)

    def wait(self, timeout: float | None = None) -> int:
        if self._wait_once:
            self._wait_once = False
            raise subprocess.TimeoutExpired(cmd="fake-nextclade", timeout=timeout or 0.0)
        self._running = False
        return self._return_code

    def poll(self) -> int | None:
        return None if self._running else self._return_code

    def terminate(self) -> None:
        self._running = False

    def kill(self) -> None:
        self._running = False


def _availability(tmp_path: Path) -> NextcladeAvailability:
    executable = tmp_path / "nextclade"
    executable.write_text("placeholder", encoding="utf-8")
    executable.chmod(0o700)
    return NextcladeAvailability(
        available=True,
        executable=executable,
        version="nextclade 3.14.0",
        source="PATH",
    )


def _run_arguments(tmp_path: Path) -> dict[str, Path]:
    input_fasta = tmp_path / "input.fasta"
    input_fasta.write_text(">sha256:" + "a" * 64 + "\nACGT\n", encoding="utf-8")
    dataset = tmp_path / "dataset"
    dataset.mkdir()
    return {
        "dataset": dataset,
        "input_fasta": input_fasta,
        "output_tsv": tmp_path / "output.tsv",
        "working_directory": tmp_path / "work",
    }


def _install_fake_popen(
    monkeypatch: pytest.MonkeyPatch,
    *,
    output_tsv: Path | None,
    output: str = "seqName\tNextclade_pango\nsha256:" + "a" * 64 + "\tB.1.351\n",
    diagnostics: bytes = b"",
    return_code: int = 0,
    wait_once: bool = False,
) -> list[tuple[tuple[str, ...], dict[str, object], _FakeProcess]]:
    calls: list[tuple[tuple[str, ...], dict[str, object], _FakeProcess]] = []

    def _popen(argv: tuple[str, ...], **kwargs: object) -> _FakeProcess:
        if output_tsv is not None:
            output_tsv.write_text(output, encoding="utf-8")
        process = _FakeProcess(
            stdout=kwargs["stdout"],  # type: ignore[arg-type]
            stderr=kwargs["stderr"],  # type: ignore[arg-type]
            diagnostics=diagnostics,
            return_code=return_code,
            wait_once=wait_once,
        )
        calls.append((argv, kwargs, process))
        return process

    monkeypatch.setattr(nextclade_module.subprocess, "Popen", _popen)
    return calls


def test_probe_reports_unavailable_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(nextclade_module.shutil, "which", lambda _name: None)

    availability = NextcladeRunner().probe()

    assert availability.available is False
    assert availability.error_code == "nextclade_not_found"


def test_probe_reads_version_without_shell(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    executable = tmp_path / "nextclade"
    executable.write_text("placeholder", encoding="utf-8")
    executable.chmod(0o700)
    calls: list[tuple[list[str], dict[str, object]]] = []

    def _run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, stdout=b"nextclade 3.14.0", stderr=b"")

    monkeypatch.setattr(nextclade_module.shutil, "which", lambda _name: str(executable))
    monkeypatch.setattr(nextclade_module.subprocess, "run", _run)

    availability = NextcladeRunner().probe()

    assert availability.available is True
    assert availability.version == "nextclade 3.14.0"
    assert calls[0][0] == [str(executable.resolve()), "--version"]
    assert calls[0][1]["shell"] is False


def test_run_uses_local_dataset_argv_and_registers_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    arguments = _run_arguments(tmp_path)
    calls = _install_fake_popen(monkeypatch, output_tsv=arguments["output_tsv"])
    registered: list[int] = []
    unregistered: list[int] = []

    result = NextcladeRunner().run(
        availability=_availability(tmp_path),
        **arguments,
        process_started=registered.append,
        process_stopped=unregistered.append,
    )

    argv, options, process = calls[0]
    assert argv[1:] == (
        "run",
        "--input-dataset",
        str(arguments["dataset"]),
        "--output-tsv",
        str(arguments["output_tsv"]),
        str(arguments["input_fasta"]),
    )
    assert "--dataset-name" not in argv
    assert options["shell"] is False
    assert result.output_tsv_path == arguments["output_tsv"]
    assert registered == [process.pid]
    assert unregistered == [process.pid]


def test_run_reports_nonzero_exit_with_bounded_diagnostics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    arguments = _run_arguments(tmp_path)
    _install_fake_popen(
        monkeypatch,
        output_tsv=arguments["output_tsv"],
        return_code=7,
        diagnostics=b"d" * (nextclade_module._MAX_DIAGNOSTIC_BYTES * 2),
    )

    with pytest.raises(NextcladeError) as error_info:
        NextcladeRunner().run(availability=_availability(tmp_path), **arguments)

    assert error_info.value.code == "nextclade_nonzero_exit"
    assert error_info.value.exit_code == 7
    assert error_info.value.diagnostics is not None
    assert len(error_info.value.diagnostics) <= nextclade_module._MAX_DIAGNOSTIC_BYTES + 16


def test_run_rejects_missing_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    arguments = _run_arguments(tmp_path)
    _install_fake_popen(monkeypatch, output_tsv=None)

    with pytest.raises(NextcladeError, match="without producing") as error_info:
        NextcladeRunner().run(availability=_availability(tmp_path), **arguments)

    assert error_info.value.code == "nextclade_output_missing"


def test_control_request_terminates_process_and_clears_registration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    arguments = _run_arguments(tmp_path)
    calls = _install_fake_popen(
        monkeypatch,
        output_tsv=arguments["output_tsv"],
        wait_once=True,
    )
    terminated: list[int] = []
    registered: list[int] = []
    unregistered: list[int] = []

    class _Cancelled(RuntimeError):
        pass

    def _terminate(process: _FakeProcess) -> None:
        terminated.append(process.pid)
        process.terminate()

    monkeypatch.setattr(nextclade_module, "terminate_process_tree", _terminate)
    with pytest.raises(_Cancelled):
        NextcladeRunner().run(
            availability=_availability(tmp_path),
            **arguments,
            control_check=lambda: (_ for _ in ()).throw(_Cancelled()),
            process_started=registered.append,
            process_stopped=unregistered.append,
        )

    process = calls[0][2]
    assert terminated == [process.pid]
    assert registered == [process.pid]
    assert unregistered == [process.pid]


def test_strict_tsv_parsing_handles_assigned_and_unassigned_lineages() -> None:
    records = parse_nextclade_pango_tsv(
        "seqName\tNextclade_pango\twarnings\terrors\n"
        "sha256:" + "a" * 64 + "\tB.1.351\tQC warning\t\n"
        "sha256:" + "b" * 64 + "\t\t\tQC error\n"
    )

    assert records[0].lineage == "B.1.351"
    assert records[0].diagnostics == ("warnings: QC warning",)
    assert records[1].lineage is None
    assert records[1].diagnostics == ("errors: QC error",)


@pytest.mark.parametrize(
    ("payload", "code"),
    [
        ("seqName\tclade\nsha256:abc\tX\n", "nextclade_pango_column_missing"),
        ("Nextclade_pango\nB.1.351\n", "nextclade_pango_column_missing"),
        ("seqName\tNextclade_pango\n\tB.1.351\n", "nextclade_tsv_malformed"),
    ],
)
def test_strict_tsv_parsing_rejects_invalid_contract(payload: str, code: str) -> None:
    with pytest.raises(NextcladeError) as error_info:
        parse_nextclade_pango_tsv(payload)

    assert error_info.value.code == code


def test_build_nextclade_argv_uses_input_dataset_only(tmp_path: Path) -> None:
    argv = build_nextclade_argv(
        executable=tmp_path / "nextclade",
        dataset=tmp_path / "dataset",
        output_tsv=tmp_path / "result.tsv",
        input_fasta=tmp_path / "input.fa",
    )

    assert argv == (
        str(tmp_path / "nextclade"),
        "run",
        "--input-dataset",
        str(tmp_path / "dataset"),
        "--output-tsv",
        str(tmp_path / "result.tsv"),
        str(tmp_path / "input.fa"),
    )
