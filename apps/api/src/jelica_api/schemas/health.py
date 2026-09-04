from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ReconciliationDiagnosticsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    scanned: int = Field(ge=0)
    updated: int = Field(ge=0)
    errors: int = Field(ge=0)
    last_run_at: datetime | None = None


class HealthResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    service: Literal["web-backend"] = "web-backend"
    status: Literal["ok", "degraded"]
    database: Literal["ok", "error"]
    detail: str | None = None
    reconciliation: ReconciliationDiagnosticsResponse | None = None


__all__ = ["HealthResponse", "ReconciliationDiagnosticsResponse"]
