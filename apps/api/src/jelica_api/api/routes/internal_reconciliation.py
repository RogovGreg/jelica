from __future__ import annotations

import hmac

from fastapi import APIRouter, HTTPException, Request, status
from sqlalchemy.exc import SQLAlchemyError

from jelica_api.app_state import get_app_state
from jelica_api.cli import (
    JelicaCliCommandError,
    JelicaCliInvocationError,
    JelicaCliProtocolError,
)
from jelica_api.schemas import ReconciliationDiagnosticsResponse

router = APIRouter(
    prefix="/api/internal/reconciliation", tags=["internal"], include_in_schema=False
)


@router.post("/run", response_model=ReconciliationDiagnosticsResponse)
def run_reconciliation(request: Request) -> ReconciliationDiagnosticsResponse:
    state = get_app_state(request)
    _require_internal_access(request=request, state=state)
    try:
        state.web_task_reconciler.reconcile()
    except (
        JelicaCliCommandError,
        JelicaCliInvocationError,
        JelicaCliProtocolError,
        SQLAlchemyError,
        RuntimeError,
        ValueError,
    ) as error:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={
                "error": "reconciliation_failed",
                "message": str(error),
            },
        ) from error
    diagnostics = state.web_task_reconciler.get_diagnostics()
    return ReconciliationDiagnosticsResponse(
        scanned=diagnostics.scanned,
        updated=diagnostics.updated,
        errors=diagnostics.errors,
        last_run_at=diagnostics.last_run_at,
    )


@router.get("/report", response_model=ReconciliationDiagnosticsResponse)
def get_reconciliation_report(request: Request) -> ReconciliationDiagnosticsResponse:
    state = get_app_state(request)
    _require_internal_access(request=request, state=state)
    diagnostics = state.web_task_reconciler.get_diagnostics()
    return ReconciliationDiagnosticsResponse(
        scanned=diagnostics.scanned,
        updated=diagnostics.updated,
        errors=diagnostics.errors,
        last_run_at=diagnostics.last_run_at,
    )


def _require_internal_access(*, request: Request, state: object) -> None:
    settings = state.settings
    presented = request.headers.get("x-jelica-internal-token", "")
    configured = settings.internal_api_token
    if (
        not settings.internal_api_enabled
        or not presented
        or not hmac.compare_digest(presented.encode("utf-8"), configured.encode("utf-8"))
    ):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not Found")


__all__ = ["router"]
