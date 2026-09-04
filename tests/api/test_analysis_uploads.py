from __future__ import annotations

import io
import json
from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from http.cookies import SimpleCookie
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi import FastAPI, HTTPException, Response, UploadFile
from sqlalchemy.exc import IntegrityError
from starlette.datastructures import Headers
from starlette.requests import Request

from jelica_api.actor_identity import GUEST_SESSION_COOKIE_NAME, WebActorIdentity
from jelica_api.analysis_uploads import (
    AnalysisUploadService,
    UploadConflictError,
    UploadDirectoryFileSource,
    UploadFileSource,
    UploadLimitError,
    UploadRequestError,
    UploadStorageError,
    UploadUnavailableError,
)
from jelica_api.api.routes.analysis_uploads import (
    create_upload_session as api_create_session,
)
from jelica_api.api.routes.analysis_uploads import (
    delete_upload_item as api_delete_item,
)
from jelica_api.api.routes.analysis_uploads import (
    delete_upload_session as api_delete_session,
)
from jelica_api.api.routes.analysis_uploads import (
    get_upload_session as api_get_session,
)
from jelica_api.api.routes.analysis_uploads import (
    upload_config_file as api_upload_config,
)
from jelica_api.api.routes.analysis_uploads import (
    upload_input_directory as api_upload_directory,
)
from jelica_api.api.routes.analysis_uploads import (
    upload_input_files as api_upload_files,
)
from jelica_api.app import create_app
from jelica_api.auth import hash_opaque_token
from jelica_api.models import AuthSession, Base, UploadItem, UploadSession, User
from jelica_api.settings import ApiSettings


@dataclass(frozen=True, slots=True)
class _UploadHarness:
    app: FastAPI
    root: Path
    user_ids: dict[str, str]
    auth_tokens: dict[str, str]


@dataclass(slots=True)
class _Browser:
    app: FastAPI
    cookies: dict[str, str]

    def request(self, *, path: str, method: str = "GET") -> Request:
        cookie_header = "; ".join(f"{name}={value}" for name, value in self.cookies.items())
        headers = [] if cookie_header == "" else [(b"cookie", cookie_header.encode("ascii"))]
        return Request(
            {
                "type": "http",
                "http_version": "1.1",
                "method": method,
                "path": path,
                "raw_path": path.encode("utf-8"),
                "query_string": b"",
                "headers": headers,
                "app": self.app,
            }
        )


@pytest.fixture
def upload_harness(tmp_path: Path) -> Iterator[_UploadHarness]:
    root = tmp_path / "uploads"
    settings = ApiSettings(
        app_name="JELICA Web Backend",
        api_host="127.0.0.1",
        api_port=8000,
        database_url=f"sqlite+pysqlite:///{tmp_path / 'web.db'}",
        cli_command_prefix=("jelica",),
        cli_timeout_seconds=30.0,
        upload_root=root,
        upload_max_file_bytes=128,
        upload_max_session_bytes=512,
        upload_max_session_files=8,
        upload_max_relative_path_length=80,
        upload_session_ttl_seconds=3600,
    )
    app = create_app(settings=settings)
    state = app.state.jelica_api_state
    Base.metadata.create_all(state.engine)
    now = datetime.now(UTC)
    user_ids: dict[str, str] = {}
    auth_tokens: dict[str, str] = {}
    with state.session_factory() as database:
        for label in ("alice", "bob"):
            user = User(
                username=label,
                email=f"{label}@example.org",
                password_hash="test-password-hash",
                email_verified=True,
                language="en",
            )
            database.add(user)
            database.flush()
            token = f"{label}-auth-token"
            database.add(
                AuthSession(
                    user_id=user.id,
                    token_hash=hash_opaque_token(token),
                    created_at=now,
                    expires_at=now + timedelta(hours=1),
                    last_used_at=now,
                )
            )
            user_ids[label] = user.id
            auth_tokens[label] = token
        database.commit()
    try:
        yield _UploadHarness(
            app=app,
            root=root,
            user_ids=user_ids,
            auth_tokens=auth_tokens,
        )
    finally:
        state.engine.dispose()


def test_guest_upload_smoke_materializes_files_directory_and_config(
    upload_harness: _UploadHarness,
) -> None:
    browser = _Browser(app=upload_harness.app, cookies={})
    session_id = _create_session(browser).id
    assert GUEST_SESSION_COOKIE_NAME in browser.cookies

    file_items = api_upload_files(
        session_id,
        browser.request(path=f"/api/analysis-uploads/{session_id}/files", method="POST"),
        [_upload_file("same.fasta", b">a\nACGT\n"), _upload_file("same.fasta", b">b\nTGCA\n")],
    ).items
    assert [item.kind for item in file_items] == ["input_file", "input_file"]
    assert file_items[0].id != file_items[1].id

    directory_item = api_upload_directory(
        session_id,
        browser.request(path=f"/api/analysis-uploads/{session_id}/directories", method="POST"),
        "samples",
        [_upload_file("a.fasta", b">a\nAAAA\n"), _upload_file("b.fasta", b">b\nCCCC\n")],
        ["a.fasta", "nested/b.fasta"],
    )
    assert directory_item.kind == "input_directory"
    assert directory_item.file_count == 2

    config_bytes = b"{ definitely-not-valid-json"
    config_item = api_upload_config(
        session_id,
        browser.request(path=f"/api/analysis-uploads/{session_id}/config", method="POST"),
        _upload_file("analysis.anything", config_bytes, content_type="application/x-custom"),
    )
    assert config_item.kind == "config_file"
    _assert_http_error(
        409,
        lambda: api_upload_config(
            session_id,
            browser.request(path=f"/api/analysis-uploads/{session_id}/config", method="POST"),
            _upload_file("replacement.json", b"{}"),
        ),
    )

    service = upload_harness.app.state.jelica_api_state.analysis_upload_service
    actor = WebActorIdentity(
        guest_session_hash=hash_opaque_token(browser.cookies[GUEST_SESSION_COOKIE_NAME])
    )
    directory = service.resolve_item(actor=actor, session_id=session_id, item_id=directory_item.id)
    assert directory.kind == "input_directory"
    assert (directory.path / "a.fasta").read_bytes() == b">a\nAAAA\n"
    assert (directory.path / "nested" / "b.fasta").read_bytes() == b">b\nCCCC\n"
    assert directory.path.is_relative_to(upload_harness.root.resolve())
    config = service.resolve_item(actor=actor, session_id=session_id, item_id=config_item.id)
    assert config.path.name == "config.json"
    assert config.path.read_bytes() == config_bytes

    snapshot = api_get_session(
        session_id, browser.request(path=f"/api/analysis-uploads/{session_id}")
    )
    assert snapshot.file_count == 5
    public_payload = snapshot.model_dump(mode="json")
    assert str(upload_harness.root) not in json.dumps(public_payload)
    assert all("path" not in item for item in public_payload["items"])

    assert (
        api_delete_item(
            session_id,
            config_item.id,
            browser.request(
                path=f"/api/analysis-uploads/{session_id}/items/{config_item.id}", method="DELETE"
            ),
        ).status_code
        == 204
    )
    replacement = api_upload_config(
        session_id,
        browser.request(path=f"/api/analysis-uploads/{session_id}/config", method="POST"),
        _upload_file("replacement.json", b"{}"),
    )
    assert replacement.kind == "config_file"
    assert (
        api_delete_session(
            session_id,
            browser.request(path=f"/api/analysis-uploads/{session_id}", method="DELETE"),
        ).status_code
        == 204
    )
    _assert_http_error(
        404,
        lambda: api_get_session(
            session_id, browser.request(path=f"/api/analysis-uploads/{session_id}")
        ),
    )
    assert not (upload_harness.root / session_id).exists()


def test_actor_isolation_and_login_does_not_claim_guest_upload(
    upload_harness: _UploadHarness,
) -> None:
    alice = _authenticated_browser(upload_harness, "alice")
    bob = _authenticated_browser(upload_harness, "bob")
    guest_a = _Browser(app=upload_harness.app, cookies={})
    guest_b = _Browser(app=upload_harness.app, cookies={})
    alice_session = _create_session(alice).id
    guest_session = _create_session(guest_a).id
    _create_session(guest_b)

    for browser in (bob, guest_a, guest_b):
        _assert_hidden_session(browser, alice_session)
        _assert_http_error(
            404,
            lambda browser=browser: api_upload_files(
                alice_session,
                browser.request(path=f"/api/analysis-uploads/{alice_session}/files", method="POST"),
                [_upload_file("x", b"x")],
            ),
        )
    _assert_hidden_session(alice, guest_session)
    guest_a.cookies["jelica_session"] = upload_harness.auth_tokens["alice"]
    _assert_hidden_session(guest_a, guest_session)
    _assert_hidden_session(alice, str(uuid4()))


@pytest.mark.parametrize(
    "relative_path",
    [
        "../secret",
        "a/../../secret",
        "/absolute",
        "C:/drive/file",
        "//server/share",
        "a\\b.fasta",
        "a//b.fasta",
        "a/./b.fasta",
        "a\x00b.fasta",
        "a/\x1fb.fasta",
    ],
)
def test_directory_rejects_unsafe_paths_without_partial_storage(
    upload_harness: _UploadHarness,
    relative_path: str,
) -> None:
    browser = _Browser(app=upload_harness.app, cookies={})
    session_id = _create_session(browser).id
    _assert_http_error(
        422,
        lambda: api_upload_directory(
            session_id,
            browser.request(path=f"/api/analysis-uploads/{session_id}/directories", method="POST"),
            "samples",
            [_upload_file("sample.fasta", b">a\nACGT\n")],
            [relative_path],
        ),
    )
    assert (
        api_get_session(
            session_id, browser.request(path=f"/api/analysis-uploads/{session_id}")
        ).items
        == ()
    )
    assert _managed_entries(upload_harness.root / session_id) == []


@pytest.mark.parametrize(
    "relative_paths",
    [["a.fasta", "a.fasta"], ["a", "a/b.fasta"], ["nested/a", "nested/a/b.fasta"]],
)
def test_directory_rejects_tree_collisions(
    upload_harness: _UploadHarness,
    relative_paths: list[str],
) -> None:
    browser = _Browser(app=upload_harness.app, cookies={})
    session_id = _create_session(browser).id
    _assert_http_error(
        409,
        lambda: api_upload_directory(
            session_id,
            browser.request(path=f"/api/analysis-uploads/{session_id}/directories", method="POST"),
            "samples",
            [_upload_file("one", b"one"), _upload_file("two", b"two")],
            relative_paths,
        ),
    )
    assert _managed_entries(upload_harness.root / session_id) == []


def test_limits_are_stream_enforced_and_partial_items_are_removed(
    upload_harness: _UploadHarness,
) -> None:
    state = upload_harness.app.state.jelica_api_state
    service = AnalysisUploadService(
        session_factory=state.session_factory,
        settings=replace(
            state.settings,
            upload_max_file_bytes=4,
            upload_max_session_bytes=6,
            upload_max_session_files=2,
        ),
    )
    actor = WebActorIdentity(user_id=upload_harness.user_ids["alice"])
    session = service.create_session(actor=actor)
    with pytest.raises(UploadLimitError, match="file exceeds"):
        service.upload_input_files(
            actor=actor,
            session_id=session.id,
            files=(UploadFileSource("large", io.BytesIO(b"12345")),),
        )
    assert service.get_session(actor=actor, session_id=session.id).items == ()
    assert _managed_entries(upload_harness.root / session.id) == []

    first = service.upload_input_files(
        actor=actor,
        session_id=session.id,
        files=(UploadFileSource("first", io.BytesIO(b"1234")),),
    )[0]
    with pytest.raises(UploadLimitError, match="total size"):
        service.upload_input_files(
            actor=actor,
            session_id=session.id,
            files=(UploadFileSource("second", io.BytesIO(b"123")),),
        )
    assert service.get_session(actor=actor, session_id=session.id).items[0].id == first.id
    assert not any(
        path.name.startswith(".tmp-") for path in (upload_harness.root / session.id).iterdir()
    )
    with pytest.raises(UploadLimitError, match="file-count"):
        service.upload_directory(
            actor=actor,
            session_id=session.id,
            display_name="two-files",
            files=(
                UploadDirectoryFileSource("a", io.BytesIO(b"a")),
                UploadDirectoryFileSource("b", io.BytesIO(b"b")),
            ),
        )


def test_identical_content_isolated_and_item_delete_preserves_other_item(
    upload_harness: _UploadHarness,
) -> None:
    service = upload_harness.app.state.jelica_api_state.analysis_upload_service
    alice = WebActorIdentity(user_id=upload_harness.user_ids["alice"])
    bob = WebActorIdentity(user_id=upload_harness.user_ids["bob"])
    alice_session = service.create_session(actor=alice)
    bob_session = service.create_session(actor=bob)
    alice_item = service.upload_input_files(
        actor=alice,
        session_id=alice_session.id,
        files=(UploadFileSource("same.fasta", io.BytesIO(b"identical")),),
    )[0]
    alice_other = service.upload_input_files(
        actor=alice,
        session_id=alice_session.id,
        files=(UploadFileSource("other.fasta", io.BytesIO(b"other")),),
    )[0]
    bob_item = service.upload_input_files(
        actor=bob,
        session_id=bob_session.id,
        files=(UploadFileSource("same.fasta", io.BytesIO(b"identical")),),
    )[0]
    alice_path = service.resolve_item(
        actor=alice, session_id=alice_session.id, item_id=alice_item.id
    ).path
    bob_path = service.resolve_item(actor=bob, session_id=bob_session.id, item_id=bob_item.id).path
    assert alice_item.id != bob_item.id
    assert alice_path != bob_path
    assert alice_path.read_bytes() == bob_path.read_bytes()

    service.delete_item(actor=alice, session_id=alice_session.id, item_id=alice_item.id)
    assert not alice_path.exists()
    assert service.resolve_item(
        actor=alice, session_id=alice_session.id, item_id=alice_other.id
    ).path.exists()
    with pytest.raises(UploadUnavailableError):
        service.resolve_item(actor=bob, session_id=alice_session.id, item_id=alice_other.id)


def test_expiration_hides_items_and_cleanup_removes_metadata_and_bytes(
    upload_harness: _UploadHarness,
) -> None:
    service = upload_harness.app.state.jelica_api_state.analysis_upload_service
    actor = WebActorIdentity(user_id=upload_harness.user_ids["alice"])
    now = datetime.now(UTC)
    session = service.create_session(actor=actor, now=now)
    item = service.upload_config(
        actor=actor,
        session_id=session.id,
        file=UploadFileSource("config.json", io.BytesIO(b"not parsed")),
        now=now,
    )
    path = service.resolve_item(actor=actor, session_id=session.id, item_id=item.id, now=now).path
    after_expiry = now + timedelta(seconds=3601)
    with pytest.raises(UploadUnavailableError):
        service.get_session(actor=actor, session_id=session.id, now=after_expiry)
    with pytest.raises(UploadUnavailableError):
        service.resolve_item(actor=actor, session_id=session.id, item_id=item.id, now=after_expiry)
    assert service.cleanup_expired(now=after_expiry) == 1
    assert not path.exists()
    with service.session_factory() as database:
        assert database.get(UploadSession, session.id) is None


def test_database_invariants_and_multipart_routes(upload_harness: _UploadHarness) -> None:
    state = upload_harness.app.state.jelica_api_state
    expiry = datetime.now(UTC) + timedelta(hours=1)
    for row in (
        UploadSession(expires_at=expiry),
        UploadSession(
            owner_user_id=upload_harness.user_ids["alice"],
            guest_session_hash="f" * 64,
            expires_at=expiry,
        ),
    ):
        with state.session_factory() as database:
            database.add(row)
            with pytest.raises(IntegrityError):
                database.commit()

    actor = WebActorIdentity(user_id=upload_harness.user_ids["alice"])
    session = state.analysis_upload_service.create_session(actor=actor)
    with state.session_factory() as database:
        database.add_all(
            [
                UploadItem(
                    session_id=session.id,
                    kind="config_file",
                    display_name=f"config-{index}",
                    file_count=1,
                    total_bytes=0,
                )
                for index in range(2)
            ]
        )
        with pytest.raises(IntegrityError):
            database.commit()

    paths = upload_harness.app.openapi()["paths"]
    assert (
        "multipart/form-data"
        in paths["/api/analysis-uploads/{session_id}/files"]["post"]["requestBody"]["content"]
    )
    assert "get" not in paths["/api/analysis-uploads"]


def test_stale_submission_reservation_can_be_reopened(upload_harness: _UploadHarness) -> None:
    state = upload_harness.app.state.jelica_api_state
    service = state.analysis_upload_service
    actor = WebActorIdentity(user_id=upload_harness.user_ids["alice"])
    created_at = datetime(2026, 1, 1, tzinfo=UTC)
    session = service.create_session(actor=actor, now=created_at)
    acquired = service.reserve_submission(
        actor=actor, session_id=session.id, trace_id="trace-1", now=created_at
    )
    assert acquired.status == "open"
    assert service.get_session(
        actor=actor, session_id=session.id, now=created_at
    ).submission_status == "submitting"

    concurrent = service.reserve_submission(
        actor=actor,
        session_id=session.id,
        trace_id="trace-2",
        now=created_at + timedelta(minutes=1),
    )
    assert concurrent.status == "submitting"
    assert concurrent.trace_id == "trace-1"

    assert not service.reset_stale_submission(
        actor=actor,
        session_id=session.id,
        trace_id="trace-1",
        stale_after=timedelta(minutes=5),
        now=created_at + timedelta(minutes=4),
    )
    assert service.reset_stale_submission(
        actor=actor,
        session_id=session.id,
        trace_id="trace-1",
        stale_after=timedelta(minutes=5),
        now=created_at + timedelta(minutes=6),
    )
    reopened = service.get_session(
        actor=actor, session_id=session.id, now=created_at + timedelta(minutes=6)
    )
    assert reopened.submission_status == "open"
    assert reopened.task_id is None


def test_service_rejects_unsafe_tree_before_writing(upload_harness: _UploadHarness) -> None:
    service = upload_harness.app.state.jelica_api_state.analysis_upload_service
    actor = WebActorIdentity(user_id=upload_harness.user_ids["alice"])
    session = service.create_session(actor=actor)
    with pytest.raises(UploadConflictError):
        service.upload_directory(
            actor=actor,
            session_id=session.id,
            display_name="samples",
            files=(
                UploadDirectoryFileSource("a", io.BytesIO(b"a")),
                UploadDirectoryFileSource("a/b", io.BytesIO(b"b")),
            ),
        )
    with pytest.raises(UploadRequestError):
        service.upload_directory(
            actor=actor,
            session_id=session.id,
            display_name="samples",
            files=(UploadDirectoryFileSource(".", io.BytesIO(b"a")),),
        )


def test_managed_delete_refuses_symlink_redirection(upload_harness: _UploadHarness) -> None:
    service = upload_harness.app.state.jelica_api_state.analysis_upload_service
    actor = WebActorIdentity(user_id=upload_harness.user_ids["alice"])
    session = service.create_session(actor=actor)
    item = service.upload_input_files(
        actor=actor,
        session_id=session.id,
        files=(UploadFileSource("sample", io.BytesIO(b"sample")),),
    )[0]
    item_root = upload_harness.root / session.id / item.id
    original = upload_harness.root / session.id / f"original-{item.id}"
    outside = upload_harness.root.parent / "outside"
    outside.mkdir()
    marker = outside / "keep"
    marker.write_bytes(b"keep")
    item_root.rename(original)
    item_root.symlink_to(outside, target_is_directory=True)

    with pytest.raises(UploadUnavailableError):
        service.resolve_item(actor=actor, session_id=session.id, item_id=item.id)
    with pytest.raises(UploadStorageError):
        service.delete_item(actor=actor, session_id=session.id, item_id=item.id)
    assert marker.read_bytes() == b"keep"


def _create_session(browser: _Browser):
    response = Response()
    result = api_create_session(
        browser.request(path="/api/analysis-uploads", method="POST"), response
    )
    raw_cookie = response.headers.get("set-cookie")
    if raw_cookie is not None:
        parsed = SimpleCookie()
        parsed.load(raw_cookie)
        browser.cookies[GUEST_SESSION_COOKIE_NAME] = parsed[GUEST_SESSION_COOKIE_NAME].value
    return result


def _authenticated_browser(harness: _UploadHarness, label: str) -> _Browser:
    return _Browser(app=harness.app, cookies={"jelica_session": harness.auth_tokens[label]})


def _upload_file(
    filename: str, content: bytes, *, content_type: str = "application/octet-stream"
) -> UploadFile:
    return UploadFile(
        file=io.BytesIO(content),
        filename=filename,
        headers=Headers({"content-type": content_type}),
    )


def _assert_hidden_session(browser: _Browser, session_id: str) -> None:
    _assert_http_error(
        404,
        lambda: api_get_session(
            session_id, browser.request(path=f"/api/analysis-uploads/{session_id}")
        ),
    )
    _assert_http_error(
        404,
        lambda: api_delete_session(
            session_id,
            browser.request(path=f"/api/analysis-uploads/{session_id}", method="DELETE"),
        ),
    )


def _assert_http_error(status_code: int, operation: Callable[[], object]) -> None:
    with pytest.raises(HTTPException) as captured:
        operation()
    assert captured.value.status_code == status_code


def _managed_entries(session_path: Path) -> list[Path]:
    if not session_path.exists():
        return []
    return list(session_path.iterdir())
