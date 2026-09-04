from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .analysis_overrides import AnalysisOverrides

_NCBI_ACCESSION = re.compile(r"^[A-Z][A-Z0-9_]*\d(?:\.\d+)?$", re.IGNORECASE)


def _validate_ncbi_source(value: str) -> str:
    candidate = value.strip()
    if _NCBI_ACCESSION.fullmatch(candidate):
        return candidate.upper()
    parsed = urlparse(candidate)
    if parsed.scheme.lower() != "https" or parsed.hostname not in {
        "ncbi.nlm.nih.gov",
        "www.ncbi.nlm.nih.gov",
    }:
        raise ValueError("Only NCBI nucleotide accessions or supported NCBI URLs are allowed")
    path = parsed.path.lower()
    if not path.startswith(("/nuccore/", "/nucleotide/")) and path not in {
        "/entrez/viewer.fcgi",
        "/sviewer/viewer.fcgi",
    }:
        raise ValueError("NCBI URL path is not supported")
    query = parse_qs(parsed.query)
    if query.get("db") and any(item.lower() != "nuccore" for item in query["db"]):
        raise ValueError("NCBI URL database must be nuccore")
    if path in {"/entrez/viewer.fcgi", "/sviewer/viewer.fcgi"} and not (
        query.get("id") or query.get("val")
    ):
        raise ValueError("NCBI URL must identify a nucleotide accession")
    return candidate


class TaskSubmissionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    sources: tuple[str, ...] = Field(min_length=1)
    config_path: str | None = None
    name: str | None = None
    trace_id: str | None = None
    overrides: tuple[str, ...] = ()

    @field_validator("sources")
    @classmethod
    def _normalize_sources(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized: list[str] = []
        for index, source in enumerate(value):
            candidate = source.strip()
            if candidate == "":
                raise ValueError(f"sources[{index}] must not be empty")
            normalized.append(candidate)
        return tuple(normalized)

    @field_validator("name")
    @classmethod
    def _normalize_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        candidate = value.strip()
        if candidate == "":
            raise ValueError("name must not be empty when provided")
        return candidate

    @field_validator("trace_id")
    @classmethod
    def _normalize_trace_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        candidate = value.strip()
        if candidate == "":
            raise ValueError("trace_id must not be empty when provided")
        return candidate

    @field_validator("config_path")
    @classmethod
    def _normalize_config_path(cls, value: str | None) -> str | None:
        if value is None:
            return None
        candidate = value.strip()
        if candidate == "":
            raise ValueError("config_path must not be empty when provided")
        return candidate


class BrowserTaskSubmissionRequest(BaseModel):
    """Browser-safe task request; local sources are represented only by an upload session."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    upload_session_id: str = Field(min_length=1, max_length=36)
    ncbi_sources: tuple[str, ...] = ()
    name: str | None = None
    trace_id: str | None = Field(default=None, max_length=36)
    analysis_overrides: AnalysisOverrides | None = None

    @field_validator("upload_session_id")
    @classmethod
    def _normalize_upload_session_id(cls, value: str) -> str:
        candidate = value.strip()
        if candidate == "":
            raise ValueError("upload_session_id must not be empty")
        return candidate

    @field_validator("ncbi_sources")
    @classmethod
    def _normalize_ncbi_sources(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized: list[str] = []
        for index, source in enumerate(value):
            candidate = source.strip()
            if candidate == "":
                raise ValueError(f"ncbi_sources[{index}] must not be empty")
            normalized.append(_validate_ncbi_source(candidate))
        return tuple(normalized)

    @field_validator("name")
    @classmethod
    def _normalize_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        candidate = value.strip()
        if candidate == "":
            raise ValueError("name must not be empty when provided")
        return candidate

    @field_validator("trace_id")
    @classmethod
    def _normalize_trace_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        candidate = value.strip()
        if candidate == "":
            raise ValueError("trace_id must not be empty when provided")
        return candidate


class TaskSubmissionResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: str = Field(min_length=1)
    final_state: str = Field(min_length=1)
    trace_id: str | None = None
    command_id: str = Field(min_length=1)


__all__ = ["BrowserTaskSubmissionRequest", "TaskSubmissionRequest", "TaskSubmissionResult"]
