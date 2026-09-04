from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import HTTPException, status
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from starlette.requests import Request

from jelica_api.api.routes.support import create_support_request, get_support_request
from jelica_api.app import create_app
from jelica_api.contracts import SupportRequestCreateRequest
from jelica_api.models import Base
from jelica_api.settings import ApiSettings
from jelica_api.support_requests import SupportRequestRecord, SupportRequestStore


def _settings() -> ApiSettings:
    return ApiSettings(
        app_name="JELICA Web Backend",
        api_host="127.0.0.1",
        api_port=8000,
        database_url="sqlite+pysqlite:///:memory:",
        cli_command_prefix=("jelica",),
        cli_timeout_seconds=30.0,
    )


def _request_for_app(app: object, *, path: str) -> Request:
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "path": path,
        "raw_path": path.encode("utf-8"),
        "query_string": b"",
        "headers": [],
        "app": app,
    }
    return Request(scope)


def test_create_app_registers_support_routes() -> None:
    app = create_app(settings=_settings())
    openapi_paths = set(app.openapi().get("paths", {}).keys())
    assert "/api/support" in openapi_paths
    assert "/api/support/{id}" in openapi_paths
    app.state.jelica_api_state.task_orchestrator.shutdown()
    app.state.jelica_api_state.engine.dispose()


def test_create_support_request_endpoint_returns_created_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created_at = datetime(2026, 8, 24, 12, 10, tzinfo=UTC)
    store = _SupportRequestStoreStub()
    store.next_created_at = created_at
    monkeypatch.setattr(
        "jelica_api.api.routes.support.get_app_state",
        lambda _request: SimpleNamespace(support_request_store=store),
    )

    request = _request_for_app(object(), path="/api/support")
    payload = create_support_request(
        SupportRequestCreateRequest(
            name="Dr. Ada",
            email="ada@example.org",
            subject="Task failed during alignment",
            message="Please check why the task moved to failed state.",
        ),
        request,
    )

    assert store.create_calls == [
        {
            "name": "Dr. Ada",
            "email": "ada@example.org",
            "subject": "Task failed during alignment",
            "message": "Please check why the task moved to failed state.",
        }
    ]
    assert payload.id == "support-1"
    assert payload.status == "open"
    assert payload.created_at == created_at


def test_get_support_request_endpoint_returns_existing_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created_at = datetime(2026, 8, 24, 12, 30, tzinfo=UTC)
    store = _SupportRequestStoreStub()
    store.by_id["support-11"] = SupportRequestRecord(
        request_id="support-11",
        name="Dr. Euler",
        email="euler@example.org",
        subject="Result package lookup",
        message="I cannot locate the .jelica package.",
        created_at=created_at,
        status="open",
    )
    monkeypatch.setattr(
        "jelica_api.api.routes.support.get_app_state",
        lambda _request: SimpleNamespace(support_request_store=store),
    )

    request = _request_for_app(object(), path="/api/support/support-11")
    payload = get_support_request("support-11", request)

    assert store.get_calls == ["support-11"]
    assert payload.id == "support-11"
    assert payload.email == "euler@example.org"
    assert payload.status == "open"
    assert payload.created_at == created_at


def test_get_support_request_endpoint_returns_404_for_unknown_identifier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _SupportRequestStoreStub()
    monkeypatch.setattr(
        "jelica_api.api.routes.support.get_app_state",
        lambda _request: SimpleNamespace(support_request_store=store),
    )

    request = _request_for_app(object(), path="/api/support/unknown")
    with pytest.raises(HTTPException) as raised:
        get_support_request("unknown", request)
    assert raised.value.status_code == status.HTTP_404_NOT_FOUND
    assert raised.value.detail["error"] == "support_request_not_found"


def test_support_request_store_roundtrip_with_sqlite() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    store = SupportRequestStore(session_factory=session_factory)

    created = store.create_request(
        name="Dr. Curie",
        email="curie@example.org",
        subject="Pipeline state question",
        message="Can you help me understand current task status?",
    )

    assert created.request_id != ""
    assert created.status == "open"
    loaded = store.get_request(request_id=created.request_id)
    assert loaded == created
    engine.dispose()


def test_create_support_request_response_contract_is_stable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _SupportRequestStoreStub()
    monkeypatch.setattr(
        "jelica_api.api.routes.support.get_app_state",
        lambda _request: SimpleNamespace(support_request_store=store),
    )

    request = _request_for_app(object(), path="/api/support")
    payload = create_support_request(
        SupportRequestCreateRequest(
            name="Dr. Turing",
            email="turing@example.org",
            subject="Support contract check",
            message="Ensuring stable response fields.",
        ),
        request,
    )
    dumped = payload.model_dump(mode="json")

    assert set(dumped.keys()) == {
        "id",
        "name",
        "email",
        "subject",
        "message",
        "created_at",
        "status",
    }


class _SupportRequestStoreStub:
    def __init__(self) -> None:
        self.create_calls: list[dict[str, str]] = []
        self.get_calls: list[str] = []
        self.next_id = "support-1"
        self.next_created_at = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)
        self.by_id: dict[str, SupportRequestRecord] = {}

    def create_request(
        self,
        *,
        name: str,
        email: str,
        subject: str,
        message: str,
    ) -> SupportRequestRecord:
        self.create_calls.append(
            {
                "name": name,
                "email": email,
                "subject": subject,
                "message": message,
            }
        )
        record = SupportRequestRecord(
            request_id=self.next_id,
            name=name,
            email=email,
            subject=subject,
            message=message,
            created_at=self.next_created_at,
            status="open",
        )
        self.by_id[record.request_id] = record
        return record

    def get_request(self, *, request_id: str) -> SupportRequestRecord | None:
        self.get_calls.append(request_id)
        return self.by_id.get(request_id)
