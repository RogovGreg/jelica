from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, File, Form, HTTPException, Request, Response, UploadFile, status

from jelica_api.actor_identity import (
    WebActorIdentity,
    actor_identity_for_request,
    guest_identity_hash_for_creation,
)
from jelica_api.analysis_uploads import (
    AnalysisUploadError,
    UploadConflictError,
    UploadDirectoryFileSource,
    UploadFileSource,
    UploadLimitError,
    UploadRequestError,
    UploadStorageError,
    UploadUnavailableError,
)
from jelica_api.api.authentication import optional_current_user
from jelica_api.app_state import get_app_state
from jelica_api.contracts import (
    UploadItemResponse,
    UploadItemsResponse,
    UploadSessionResponse,
)

router = APIRouter(prefix="/api/analysis-uploads", tags=["analysis-uploads"])


@router.post("", response_model=UploadSessionResponse, status_code=status.HTTP_201_CREATED)
def create_upload_session(request: Request, response: Response) -> UploadSessionResponse:
    state = get_app_state(request)
    current_user = optional_current_user(request)
    if current_user is not None:
        actor = WebActorIdentity(user_id=current_user.user_id)
    else:
        actor = WebActorIdentity(
            guest_session_hash=guest_identity_hash_for_creation(
                request=request,
                response=response,
                secure=state.settings.auth_cookie_secure,
            )
        )
    try:
        return state.analysis_upload_service.create_session(actor=actor)
    except AnalysisUploadError as error:
        raise _http_upload_error(error) from error


@router.get("/{session_id}", response_model=UploadSessionResponse)
def get_upload_session(session_id: str, request: Request) -> UploadSessionResponse:
    try:
        return get_app_state(request).analysis_upload_service.get_session(
            actor=_actor_for_request(request),
            session_id=session_id,
        )
    except AnalysisUploadError as error:
        raise _http_upload_error(error) from error


@router.delete("/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_upload_session(session_id: str, request: Request) -> Response:
    try:
        get_app_state(request).analysis_upload_service.delete_session(
            actor=_actor_for_request(request),
            session_id=session_id,
        )
    except AnalysisUploadError as error:
        raise _http_upload_error(error) from error
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/{session_id}/files",
    response_model=UploadItemsResponse,
    status_code=status.HTTP_201_CREATED,
)
def upload_input_files(
    session_id: str,
    request: Request,
    files: Annotated[list[UploadFile], File(description="Separate logical input files")],
) -> UploadItemsResponse:
    try:
        items = get_app_state(request).analysis_upload_service.upload_input_files(
            actor=_actor_for_request(request),
            session_id=session_id,
            files=tuple(
                UploadFileSource(display_name=file.filename or "", stream=file.file)
                for file in files
            ),
        )
        return UploadItemsResponse(items=items)
    except AnalysisUploadError as error:
        raise _http_upload_error(error) from error


@router.post(
    "/{session_id}/directories",
    response_model=UploadItemResponse,
    status_code=status.HTTP_201_CREATED,
)
def upload_input_directory(
    session_id: str,
    request: Request,
    display_name: Annotated[str, Form()],
    files: Annotated[list[UploadFile], File(description="Directory regular files")],
    relative_paths: Annotated[
        list[str], Form(description="Root-relative Web-style path for each file")
    ],
) -> UploadItemResponse:
    if len(files) != len(relative_paths):
        raise _http_upload_error(
            UploadRequestError("Each directory file must have exactly one relative path.")
        )
    try:
        return get_app_state(request).analysis_upload_service.upload_directory(
            actor=_actor_for_request(request),
            session_id=session_id,
            display_name=display_name,
            files=tuple(
                UploadDirectoryFileSource(relative_path=path, stream=file.file)
                for file, path in zip(files, relative_paths, strict=True)
            ),
        )
    except AnalysisUploadError as error:
        raise _http_upload_error(error) from error


@router.post(
    "/{session_id}/config",
    response_model=UploadItemResponse,
    status_code=status.HTTP_201_CREATED,
)
def upload_config_file(
    session_id: str,
    request: Request,
    file: Annotated[UploadFile, File(description="JELICA config artifact")],
) -> UploadItemResponse:
    try:
        return get_app_state(request).analysis_upload_service.upload_config(
            actor=_actor_for_request(request),
            session_id=session_id,
            file=UploadFileSource(display_name=file.filename or "", stream=file.file),
        )
    except AnalysisUploadError as error:
        raise _http_upload_error(error) from error


@router.delete(
    "/{session_id}/items/{item_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_upload_item(session_id: str, item_id: str, request: Request) -> Response:
    try:
        get_app_state(request).analysis_upload_service.delete_item(
            actor=_actor_for_request(request),
            session_id=session_id,
            item_id=item_id,
        )
    except AnalysisUploadError as error:
        raise _http_upload_error(error) from error
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _actor_for_request(request: Request) -> WebActorIdentity:
    return actor_identity_for_request(
        request=request,
        current_user=optional_current_user(request),
    )


def _http_upload_error(error: AnalysisUploadError) -> HTTPException:
    if isinstance(error, UploadUnavailableError):
        return HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "upload_not_found", "message": "Upload was not found."},
        )
    if isinstance(error, UploadLimitError):
        return HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail={"error": "upload_limit_exceeded", "message": str(error)},
        )
    if isinstance(error, UploadConflictError):
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "upload_conflict", "message": str(error)},
        )
    if isinstance(error, UploadRequestError):
        return HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"error": "upload_request_invalid", "message": str(error)},
        )
    if isinstance(error, UploadStorageError):
        return HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error": "upload_storage_unavailable",
                "message": "Upload storage is temporarily unavailable.",
            },
        )
    return HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail={"error": "upload_error", "message": "Upload operation failed."},
    )


__all__ = ["router"]
