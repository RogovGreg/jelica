from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ReportMetric:
    label: str
    value: str


@dataclass(frozen=True, slots=True)
class ReportMessage:
    code: str | None
    message: str


@dataclass(frozen=True, slots=True)
class ReportStage:
    stage_id: str
    display_name: str
    status: str
    metrics: tuple[ReportMetric, ...]
    warnings: tuple[ReportMessage, ...]
    errors: tuple[ReportMessage, ...]
    details: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PairwiseReportItem:
    left_sequence_id: str
    right_sequence_id: str
    status: str
    compared_length: int | None
    matches: int | None
    identity: float | None
    substitutions: int | None
    insertions: int | None
    deletions: int | None
    distance: float | None
    warnings: tuple[ReportMessage, ...]
    errors: tuple[ReportMessage, ...]


@dataclass(frozen=True, slots=True)
class ReportMetadata:
    task_id: str
    task_status: str
    created_at: str
    completed_at: str
    package_created_at: str
    content_id: str
    format_version: str
    producer_version: str
    stage_count: int
    protected_artifact_count: int


@dataclass(frozen=True, slots=True)
class AnalysisReportModel:
    metadata: ReportMetadata
    stages: tuple[ReportStage, ...]
    pairwise_comparisons: tuple[PairwiseReportItem, ...]


__all__ = [
    "AnalysisReportModel",
    "PairwiseReportItem",
    "ReportMessage",
    "ReportMetadata",
    "ReportMetric",
    "ReportStage",
]
