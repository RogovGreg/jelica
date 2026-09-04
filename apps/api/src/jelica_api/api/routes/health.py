from __future__ import annotations

from fastapi import APIRouter, Request, Response, status

from jelica_api.app_state import get_app_state
from jelica_api.database import DatabaseUnavailableError, probe_database
from jelica_api.schemas import HealthResponse, ReconciliationDiagnosticsResponse

router = APIRouter(tags=["system"])


@router.get("/health", response_model=HealthResponse)
@router.get("/api/health", response_model=HealthResponse, include_in_schema=False)
def health_check(request: Request, response: Response) -> HealthResponse:
    state = get_app_state(request)
    reconciliation = state.web_task_reconciler.get_diagnostics()
    diagnostics = ReconciliationDiagnosticsResponse(
        scanned=reconciliation.scanned,
        updated=reconciliation.updated,
        errors=reconciliation.errors,
        last_run_at=reconciliation.last_run_at,
    )
    try:
        probe_database(engine=state.engine)
    except DatabaseUnavailableError as error:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return HealthResponse(
            status="degraded",
            database="error",
            detail=str(error),
            reconciliation=diagnostics,
        )
    return HealthResponse(status="ok", database="ok", reconciliation=diagnostics)


__all__ = ["router"]
