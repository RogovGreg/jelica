from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import BinaryIO

import pytest

from jelica_core.alignment import (
    AlignmentEngineRequest,
    AlignmentInputSequence,
    AlignmentToolAvailability,
    MafftAlignmentEngine,
    MafftError,
    build_mafft_option_arguments,
)
from jelica_core.alignment import (
    mafft as mafft_module,
)
from jelica_core.config import (
    AnalysisAlignmentConstruction,
    AnalysisMafftDirectionAdjustment,
    AnalysisMafftMemoryMode,
    AnalysisMafftPhaseThreadMode,
    AnalysisMafftStrategy,
    ResolvedAnalysisMafftConfig,
)
from jelica_core.runtime.messages import WorkerStopReason

_FIRST_ID = "sha256:" + "1" * 64
_SECOND_ID = "sha256:" + "2" * 64


class _FakeProcess:
    def __init__(
        self,
        *,
        stdout: BinaryIO,
        stderr: BinaryIO,
        output: bytes = b"result",
        diagnostics: bytes = b"",
        return_code: int = 0,
        wait_once: bool = False,
    ) -> None:
        self.pid = 24680
        self._return_code = return_code
        self._running = True
        self._wait_once = wait_once
        stdout.write(output)
        stderr.write(diagnostics)

    def wait(self, timeout: float | None = None) -> int:
        if self._wait_once:
            self._wait_once = False
            raise subprocess.TimeoutExpired(cmd="fake-mafft", timeout=timeout or 0.0)
        self._running = False
        return self._return_code

    def poll(self) -> int | None:
        return None if self._running else self._return_code

    def terminate(self) -> None:
        self._running = False

    def kill(self) -> None:
        self._running = False


def _availability(tmp_path: Path) -> AlignmentToolAvailability:
    return AlignmentToolAvailability(
        available=True,
        executable=tmp_path / "fake-mafft",
        version="7.526",
        version_parts=(7, 526),
        source="system_config",
    )


def _request(
    tmp_path: Path,
    *,
    construction: AnalysisAlignmentConstruction = AnalysisAlignmentConstruction.JOINT,
    config: ResolvedAnalysisMafftConfig | None = None,
    control_check: object | None = None,
    process_started: object | None = None,
    process_stopped: object | None = None,
) -> AlignmentEngineRequest:
    return AlignmentEngineRequest(
        sequences=(
            AlignmentInputSequence(
                sequence_id=_FIRST_ID,
                sequence="NN",
                logical_sample_ids=("sample-1",),
            ),
            AlignmentInputSequence(
                sequence_id=_SECOND_ID,
                sequence="NNN",
                logical_sample_ids=("sample-2",),
            ),
        ),
        construction=construction,
        reference_sequence_id=(
            _FIRST_ID
            if construction is AnalysisAlignmentConstruction.REFERENCE_GUIDED
            else None
        ),
        mafft_config=config or ResolvedAnalysisMafftConfig(),
        working_directory=tmp_path / "alignment" / ".mafft-work",
        control_check=control_check,  # type: ignore[arg-type]
        process_started=process_started,  # type: ignore[arg-type]
        process_stopped=process_stopped,  # type: ignore[arg-type]
    )


def _install_fake_popen(
    monkeypatch: pytest.MonkeyPatch,
    *,
    output: bytes = b"result",
    diagnostics: bytes = b"",
    return_code: int = 0,
    wait_once: bool = False,
) -> list[tuple[list[str], dict[str, object], _FakeProcess]]:
    calls: list[tuple[list[str], dict[str, object], _FakeProcess]] = []

    def _popen(argv: list[str], **kwargs: object) -> _FakeProcess:
        process = _FakeProcess(
            stdout=kwargs["stdout"],  # type: ignore[arg-type]
            stderr=kwargs["stderr"],  # type: ignore[arg-type]
            output=output,
            diagnostics=diagnostics,
            return_code=return_code,
            wait_once=wait_once,
        )
        calls.append((argv, kwargs, process))
        return process

    monkeypatch.setattr(mafft_module.subprocess, "Popen", _popen)
    return calls


def test_probe_resolves_version_without_shell(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / "fake-mafft"
    executable.touch()
    executable.chmod(0o700)
    calls: list[tuple[list[str], dict[str, object]]] = []

    def _run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, stdout=b"v7.526", stderr=b"")

    monkeypatch.setattr(mafft_module.subprocess, "run", _run)

    availability = MafftAlignmentEngine().probe(explicit_executable=str(executable))

    assert availability.available is True
    assert availability.version == "7.526"
    assert calls[0][0] == [str(executable.resolve()), "--version"]
    assert calls[0][1]["shell"] is False


def test_probe_reports_missing_executable_without_invoking_process(tmp_path: Path) -> None:
    availability = MafftAlignmentEngine().probe(
        explicit_executable=str(tmp_path / "missing-mafft")
    )

    assert availability.available is False
    assert availability.executable is None
    assert availability.error_code == "mafft_not_found"
    assert availability.reason is not None


def test_probe_reports_launch_error_safely(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / "fake-mafft"
    executable.touch()
    executable.chmod(0o700)
    monkeypatch.setattr(
        mafft_module.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("probe failed")),
    )

    availability = MafftAlignmentEngine().probe(explicit_executable=str(executable))

    assert availability.available is False
    assert availability.version is None
    assert availability.error_code == "mafft_version_probe_failed"
    assert availability.reason is not None
    assert "probe failed" not in availability.reason


@pytest.mark.parametrize(
    ("strategy", "expected"),
    [
        (AnalysisMafftStrategy.AUTO, ("--auto",)),
        (AnalysisMafftStrategy.FFT_NS_1, ("--retree", "1", "--maxiterate", "0")),
        (AnalysisMafftStrategy.FFT_NS_2, ("--retree", "2", "--maxiterate", "0")),
        (AnalysisMafftStrategy.FFT_NS_I, ("--retree", "2", "--maxiterate", "1000")),
        (
            AnalysisMafftStrategy.NW_NS_1,
            ("--retree", "1", "--maxiterate", "0", "--nofft"),
        ),
        (
            AnalysisMafftStrategy.NW_NS_2,
            ("--retree", "2", "--maxiterate", "0", "--nofft"),
        ),
        (
            AnalysisMafftStrategy.NW_NS_I,
            ("--retree", "2", "--maxiterate", "1000", "--nofft"),
        ),
        (AnalysisMafftStrategy.G_INS_I, ("--globalpair", "--maxiterate", "1000")),
        (AnalysisMafftStrategy.L_INS_I, ("--localpair", "--maxiterate", "1000")),
        (
            AnalysisMafftStrategy.E_INS_I,
            ("--genafpair", "--maxiterate", "1000", "--ep", "0"),
        ),
    ],
)
def test_public_strategies_map_to_main_executable_arguments(
    strategy: AnalysisMafftStrategy,
    expected: tuple[str, ...],
) -> None:
    arguments = build_mafft_option_arguments(ResolvedAnalysisMafftConfig(strategy=strategy))

    cursor = 0
    for token in expected:
        cursor = arguments.index(token, cursor) + 1
    assert "custom" not in arguments


def test_named_strategy_translates_typed_overrides() -> None:
    arguments = build_mafft_option_arguments(
        ResolvedAnalysisMafftConfig(
            strategy=AnalysisMafftStrategy.L_INS_I,
            direction_adjustment=AnalysisMafftDirectionAdjustment.FAST,
            memory_mode=AnalysisMafftMemoryMode.SAVE,
            threads=4,
            progressive_threads=2,
            iterative_threads=AnalysisMafftPhaseThreadMode.DISABLED,
            gap_open_penalty=1.5,
            offset=0.0,
        )
    )

    assert "--adjustdirection" in arguments
    assert "--memsave" in arguments
    assert ("--thread", "4") == arguments[arguments.index("--thread") :][:2]
    assert ("--threadtb", "2") == arguments[arguments.index("--threadtb") :][:2]
    assert ("--threadit", "0") == arguments[arguments.index("--threadit") :][:2]
    assert "--op" in arguments
    assert "--ep" in arguments


@pytest.mark.parametrize(
    "construction",
    [
        AnalysisAlignmentConstruction.JOINT,
        AnalysisAlignmentConstruction.REFERENCE_GUIDED,
    ],
)
def test_align_builds_list_argv_and_safe_construction_arguments(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    construction: AnalysisAlignmentConstruction,
) -> None:
    calls = _install_fake_popen(monkeypatch)

    result = MafftAlignmentEngine().align(
        availability=_availability(tmp_path),
        request=_request(tmp_path, construction=construction),
    )

    argv, kwargs, _process = calls[0]
    assert isinstance(argv, list)
    assert kwargs["shell"] is False
    assert ("--add" in argv) is (
        construction is AnalysisAlignmentConstruction.REFERENCE_GUIDED
    )
    assert not {"--keeplength", "--mapout", "--compactmapout"}.intersection(argv)
    assert ("--add" in result.effective_arguments) is (
        construction is AnalysisAlignmentConstruction.REFERENCE_GUIDED
    )


def test_align_reports_nonzero_exit_and_bounds_diagnostics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_popen(
        monkeypatch,
        return_code=9,
        diagnostics=b"d" * (mafft_module._MAX_DIAGNOSTIC_BYTES * 2),
    )

    with pytest.raises(MafftError) as captured:
        MafftAlignmentEngine().align(
            availability=_availability(tmp_path),
            request=_request(tmp_path),
        )

    assert captured.value.code == "mafft_nonzero_exit"
    assert len(captured.value.detail) < 200
    assert "dddddddd" not in captured.value.detail
    diagnostic_path = tmp_path / "alignment" / "diagnostics" / "mafft.stderr.log"
    assert diagnostic_path.stat().st_size <= mafft_module._MAX_DIAGNOSTIC_BYTES + 64


def test_align_rejects_empty_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_popen(monkeypatch, output=b"")

    with pytest.raises(MafftError) as captured:
        MafftAlignmentEngine().align(
            availability=_availability(tmp_path),
            request=_request(tmp_path),
        )

    assert captured.value.code == "mafft_empty_output"


@pytest.mark.parametrize(
    "reason",
    [
        WorkerStopReason.PAUSE_REQUESTED,
        WorkerStopReason.CANCEL_REQUESTED,
        WorkerStopReason.RUNTIME_SHUTDOWN,
    ],
)
def test_control_request_terminates_process_and_clears_registration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reason: WorkerStopReason,
) -> None:
    calls = _install_fake_popen(monkeypatch, wait_once=True)
    terminated: list[int] = []
    registered: list[int] = []
    unregistered: list[int] = []

    class _ControlRequested(RuntimeError):
        def __init__(self) -> None:
            self.reason = reason

    def _terminate(process: _FakeProcess) -> None:
        terminated.append(process.pid)
        process.terminate()

    monkeypatch.setattr(mafft_module, "terminate_process_tree", _terminate)

    with pytest.raises(_ControlRequested):
        MafftAlignmentEngine().align(
            availability=_availability(tmp_path),
            request=_request(
                tmp_path,
                control_check=lambda: (_ for _ in ()).throw(_ControlRequested()),
                process_started=registered.append,
                process_stopped=unregistered.append,
            ),
        )

    process = calls[0][2]
    assert registered == [process.pid]
    assert terminated == [process.pid]
    assert unregistered == [process.pid]
    assert process.poll() is not None
    assert not (tmp_path / "alignment" / "aligned.fasta").exists()


def test_resume_restarts_unfinished_external_alignment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _install_fake_popen(monkeypatch, wait_once=True)
    paused = True

    class _PauseRequested(RuntimeError):
        reason = WorkerStopReason.PAUSE_REQUESTED

    def _control() -> None:
        nonlocal paused
        if paused:
            paused = False
            raise _PauseRequested

    def _terminate(process: _FakeProcess) -> None:
        process.terminate()

    monkeypatch.setattr(mafft_module, "terminate_process_tree", _terminate)
    request = _request(tmp_path, control_check=_control)

    with pytest.raises(_PauseRequested):
        MafftAlignmentEngine().align(
            availability=_availability(tmp_path),
            request=request,
        )
    result = MafftAlignmentEngine().align(
        availability=_availability(tmp_path),
        request=request,
    )

    assert len(calls) == 2
    assert result.output_path.is_file()


@pytest.mark.parametrize("platform", ["posix", "nt"])
def test_force_tree_helper_uses_soft_then_forced_cross_platform_path(
    monkeypatch: pytest.MonkeyPatch,
    platform: str,
) -> None:
    actions: list[tuple[str, object]] = []
    monkeypatch.setattr(mafft_module.os, "name", platform)
    monkeypatch.setattr(mafft_module, "_process_tree_exists", lambda _pid: True)
    monkeypatch.setattr(
        mafft_module,
        "_wait_for_process_tree_exit",
        lambda **_kwargs: False,
    )
    monkeypatch.setattr(
        mafft_module,
        "_signal_posix_process_tree",
        lambda **kwargs: actions.append(("posix", kwargs["sig"])),
    )
    monkeypatch.setattr(
        mafft_module,
        "_terminate_windows_pid_tree",
        lambda **kwargs: actions.append(("windows", kwargs["force"])),
    )

    mafft_module.terminate_process_tree_by_pid(24680, grace_seconds=0.0)

    if platform == "nt":
        assert actions == [("windows", False), ("windows", True)]
    else:
        assert [name for name, _value in actions] == ["posix", "posix"]


def test_force_tree_helper_stops_synchronized_long_running_process_tree(
    tmp_path: Path,
) -> None:
    ready_path = tmp_path / "child-ready.txt"
    child_program = "import threading; threading.Event().wait(60)"
    parent_program = (
        "import pathlib, subprocess, sys, threading; "
        "child=subprocess.Popen([sys.executable, '-c', sys.argv[2]]); "
        "pathlib.Path(sys.argv[1]).write_text(str(child.pid), encoding='ascii'); "
        "threading.Event().wait(60)"
    )
    if os.name == "nt":
        parent = subprocess.Popen(
            [sys.executable, "-c", parent_program, str(ready_path), child_program],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            shell=False,
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        )
    else:
        parent = subprocess.Popen(
            [sys.executable, "-c", parent_program, str(ready_path), child_program],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            shell=False,
            start_new_session=True,
        )
    child_pid: int | None = None
    try:
        deadline = time.monotonic() + 3.0
        while not ready_path.is_file() and time.monotonic() < deadline:
            threading.Event().wait(0.01)
        assert ready_path.is_file()
        child_pid = int(ready_path.read_text(encoding="ascii"))

        mafft_module.terminate_process_tree_by_pid(parent.pid, grace_seconds=0.2)
        parent.wait(timeout=3.0)

        deadline = time.monotonic() + 3.0
        while _pid_exists(child_pid) and time.monotonic() < deadline:
            threading.Event().wait(0.01)
        assert _pid_exists(child_pid) is False
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait(timeout=3.0)
        if child_pid is not None and _pid_exists(child_pid):
            try:
                os.kill(child_pid, signal.SIGTERM)
            except OSError:
                pass


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True
