from __future__ import annotations

from pathlib import Path


class AnalysisTaskInitializationError(RuntimeError):
    """Raised when analysis task initialization fails after config resolution."""


class AnalysisTaskWorkspaceCompensationError(AnalysisTaskInitializationError):
    """Raised when task workspace cleanup fails after a pre-registration error."""

    def __init__(
        self,
        *,
        task_id: str,
        task_dir: Path,
        original_error: Exception,
        cleanup_error: Exception,
    ) -> None:
        self.task_id = task_id
        self.task_dir = task_dir
        self.original_error = original_error
        self.cleanup_error = cleanup_error
        self.original_exception_type = type(original_error).__name__
        self.cleanup_exception_type = type(cleanup_error).__name__
        self.original_message = str(original_error)
        self.cleanup_message = str(cleanup_error)
        super().__init__(
            "Analysis task was not registered, but temporary task directory cleanup failed: "
            f"task_id='{task_id}', task_dir='{task_dir}', "
            f"original_error={self.original_exception_type}: {self.original_message}; "
            f"cleanup_error={self.cleanup_exception_type}: {self.cleanup_message}."
        )
