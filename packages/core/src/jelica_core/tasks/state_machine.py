from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .registry_models import (
    ALLOWED_ANALYTICAL_TASK_JOB_TRANSITIONS,
    AnalyticalTaskMutationResultType,
    AnalyticalTaskState,
    is_job_transition_allowed,
)


class AnalyticalTaskLifecycleIntent(StrEnum):
    START = "start"
    RESUME = "resume"
    PAUSE = "pause"
    CANCEL = "cancel"
    SCHEDULER_PREEMPT = "scheduler_preempt"


class AnalyticalTaskControlPriority(StrEnum):
    DELETION = "deletion"
    CANCEL = "cancel"
    EXPLICIT_PAUSE = "explicit_pause"
    SCHEDULER_PREEMPTION = "scheduler_preemption"


@dataclass(frozen=True, slots=True)
class StateMachineDecision:
    result_type: AnalyticalTaskMutationResultType
    target_state: AnalyticalTaskState | None = None
    details: dict[str, str] | None = None


def evaluate_job_intent(
    *,
    intent: AnalyticalTaskLifecycleIntent,
    current_state: AnalyticalTaskState,
) -> StateMachineDecision:
    if intent is AnalyticalTaskLifecycleIntent.START:
        return _evaluate_start(current_state=current_state)
    if intent is AnalyticalTaskLifecycleIntent.RESUME:
        return _evaluate_resume(current_state=current_state)
    if intent is AnalyticalTaskLifecycleIntent.PAUSE:
        return _evaluate_pause(current_state=current_state)
    if intent is AnalyticalTaskLifecycleIntent.CANCEL:
        return _evaluate_cancel(current_state=current_state)
    return _evaluate_scheduler_preempt(current_state=current_state)


def is_reprioritize_allowed(state: AnalyticalTaskState) -> bool:
    return state in {
        AnalyticalTaskState.WAITING,
        AnalyticalTaskState.QUEUED,
        AnalyticalTaskState.RUNNING,
        AnalyticalTaskState.PAUSE_REQUESTED,
        AnalyticalTaskState.PREEMPTION_REQUESTED,
        AnalyticalTaskState.PAUSED,
    }


def is_runtime_transition_allowed(
    *,
    from_state: AnalyticalTaskState,
    to_state: AnalyticalTaskState,
) -> bool:
    if from_state not in ALLOWED_ANALYTICAL_TASK_JOB_TRANSITIONS:
        return False
    return is_job_transition_allowed(from_state=from_state, to_state=to_state)


def _evaluate_start(current_state: AnalyticalTaskState) -> StateMachineDecision:
    if current_state in {AnalyticalTaskState.QUEUED, AnalyticalTaskState.RUNNING}:
        return StateMachineDecision(
            result_type=AnalyticalTaskMutationResultType.ALREADY_SATISFIED,
            target_state=current_state,
        )
    if current_state is AnalyticalTaskState.WAITING:
        return StateMachineDecision(
            result_type=AnalyticalTaskMutationResultType.APPLIED,
            target_state=AnalyticalTaskState.QUEUED,
        )
    if current_state is AnalyticalTaskState.PAUSED:
        return StateMachineDecision(
            result_type=AnalyticalTaskMutationResultType.INVALID_TRANSITION,
            details={"reason": "paused jobs must be resumed"},
        )
    if current_state is AnalyticalTaskState.CANCEL_REQUESTED:
        return StateMachineDecision(
            result_type=AnalyticalTaskMutationResultType.CONFLICT,
            details={"priority": AnalyticalTaskControlPriority.CANCEL.value},
        )
    if current_state in {
        AnalyticalTaskState.PAUSE_REQUESTED,
        AnalyticalTaskState.PREEMPTION_REQUESTED,
    }:
        return StateMachineDecision(
            result_type=AnalyticalTaskMutationResultType.CONFLICT,
            details={"reason": "job has a pending control request"},
        )
    return StateMachineDecision(result_type=AnalyticalTaskMutationResultType.INVALID_TRANSITION)


def _evaluate_resume(current_state: AnalyticalTaskState) -> StateMachineDecision:
    if current_state is AnalyticalTaskState.PAUSED:
        return StateMachineDecision(
            result_type=AnalyticalTaskMutationResultType.APPLIED,
            target_state=AnalyticalTaskState.QUEUED,
        )
    if current_state in {AnalyticalTaskState.QUEUED, AnalyticalTaskState.RUNNING}:
        return StateMachineDecision(
            result_type=AnalyticalTaskMutationResultType.ALREADY_SATISFIED,
            target_state=current_state,
        )
    if current_state in {
        AnalyticalTaskState.PAUSE_REQUESTED,
        AnalyticalTaskState.PREEMPTION_REQUESTED,
        AnalyticalTaskState.CANCEL_REQUESTED,
    }:
        return StateMachineDecision(
            result_type=AnalyticalTaskMutationResultType.CONFLICT,
            details={"reason": "job has a pending control request"},
        )
    return StateMachineDecision(result_type=AnalyticalTaskMutationResultType.INVALID_TRANSITION)


def _evaluate_pause(current_state: AnalyticalTaskState) -> StateMachineDecision:
    if current_state is AnalyticalTaskState.RUNNING:
        return StateMachineDecision(
            result_type=AnalyticalTaskMutationResultType.APPLIED,
            target_state=AnalyticalTaskState.PAUSE_REQUESTED,
        )
    if current_state in {AnalyticalTaskState.QUEUED, AnalyticalTaskState.WAITING}:
        return StateMachineDecision(
            result_type=AnalyticalTaskMutationResultType.APPLIED,
            target_state=AnalyticalTaskState.PAUSED,
        )
    if current_state is AnalyticalTaskState.PREEMPTION_REQUESTED:
        return StateMachineDecision(
            result_type=AnalyticalTaskMutationResultType.APPLIED,
            target_state=AnalyticalTaskState.PAUSE_REQUESTED,
        )
    if current_state in {AnalyticalTaskState.PAUSE_REQUESTED, AnalyticalTaskState.PAUSED}:
        return StateMachineDecision(
            result_type=AnalyticalTaskMutationResultType.ALREADY_SATISFIED,
            target_state=current_state,
        )
    if current_state is AnalyticalTaskState.CANCEL_REQUESTED:
        return StateMachineDecision(
            result_type=AnalyticalTaskMutationResultType.CONFLICT,
            details={"priority": AnalyticalTaskControlPriority.CANCEL.value},
        )
    return StateMachineDecision(result_type=AnalyticalTaskMutationResultType.INVALID_TRANSITION)


def _evaluate_cancel(current_state: AnalyticalTaskState) -> StateMachineDecision:
    if current_state in {
        AnalyticalTaskState.RUNNING,
        AnalyticalTaskState.PAUSE_REQUESTED,
        AnalyticalTaskState.PREEMPTION_REQUESTED,
    }:
        return StateMachineDecision(
            result_type=AnalyticalTaskMutationResultType.APPLIED,
            target_state=AnalyticalTaskState.CANCEL_REQUESTED,
        )
    if current_state in {
        AnalyticalTaskState.WAITING,
        AnalyticalTaskState.QUEUED,
        AnalyticalTaskState.PAUSED,
    }:
        return StateMachineDecision(
            result_type=AnalyticalTaskMutationResultType.APPLIED,
            target_state=AnalyticalTaskState.CANCELLED,
        )
    if current_state in {AnalyticalTaskState.CANCEL_REQUESTED, AnalyticalTaskState.CANCELLED}:
        return StateMachineDecision(
            result_type=AnalyticalTaskMutationResultType.ALREADY_SATISFIED,
            target_state=current_state,
        )
    return StateMachineDecision(result_type=AnalyticalTaskMutationResultType.INVALID_TRANSITION)


def _evaluate_scheduler_preempt(current_state: AnalyticalTaskState) -> StateMachineDecision:
    if current_state is AnalyticalTaskState.RUNNING:
        return StateMachineDecision(
            result_type=AnalyticalTaskMutationResultType.APPLIED,
            target_state=AnalyticalTaskState.PREEMPTION_REQUESTED,
        )
    if current_state is AnalyticalTaskState.PREEMPTION_REQUESTED:
        return StateMachineDecision(
            result_type=AnalyticalTaskMutationResultType.ALREADY_SATISFIED,
            target_state=current_state,
        )
    if current_state in {AnalyticalTaskState.PAUSE_REQUESTED, AnalyticalTaskState.PAUSED}:
        return StateMachineDecision(
            result_type=AnalyticalTaskMutationResultType.CONFLICT,
            details={"priority": AnalyticalTaskControlPriority.EXPLICIT_PAUSE.value},
        )
    if current_state is AnalyticalTaskState.CANCEL_REQUESTED:
        return StateMachineDecision(
            result_type=AnalyticalTaskMutationResultType.CONFLICT,
            details={"priority": AnalyticalTaskControlPriority.CANCEL.value},
        )
    return StateMachineDecision(result_type=AnalyticalTaskMutationResultType.INVALID_TRANSITION)
