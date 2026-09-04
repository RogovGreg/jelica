from __future__ import annotations

import os
import shutil
import stat
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import BinaryIO
from uuid import UUID, uuid4

from sqlalchemy import and_, false, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, selectinload, sessionmaker

from jelica_api.actor_identity import WebActorIdentity
from jelica_api.contracts import UploadItemKind, UploadItemResponse, UploadSessionResponse
from jelica_api.models import UploadItem, UploadSession, WebTask
from jelica_api.settings import ApiSettings

_COPY_CHUNK_BYTES = 1024 * 1024


class AnalysisUploadError(RuntimeError):
    """Base controlled Analysis Upload failure."""


class UploadUnavailableError(AnalysisUploadError):
    """Raised for absent, inaccessible, expired, or otherwise unusable uploads."""


class UploadRequestError(AnalysisUploadError):
    """Raised when upload transport metadata is malformed or unsafe."""


class UploadConflictError(AnalysisUploadError):
    """Raised when an upload conflicts with existing session state."""


class UploadLimitError(AnalysisUploadError):
    """Raised when configured upload limits would be exceeded."""


class UploadStorageError(AnalysisUploadError):
    """Raised when managed storage cannot complete an operation."""


@dataclass(frozen=True, slots=True)
class UploadFileSource:
    display_name: str
    stream: BinaryIO


@dataclass(frozen=True, slots=True)
class UploadDirectoryFileSource:
    relative_path: str
    stream: BinaryIO


@dataclass(frozen=True, slots=True)
class MaterializedUploadItem:
    session_id: str
    item_id: str
    kind: UploadItemKind
    path: Path


@dataclass(frozen=True, slots=True)
class SubmissionReservation:
    session_id: str
    status: str
    trace_id: str | None
    task_id: str | None


@dataclass(frozen=True, slots=True)
class _PendingItem:
    item_id: str
    kind: UploadItemKind
    display_name: str
    file_count: int
    total_bytes: int
    temporary_path: Path
    final_path: Path


@dataclass(frozen=True, slots=True)
class AnalysisUploadService:
    session_factory: sessionmaker[Session]
    settings: ApiSettings

    def create_session(
        self,
        *,
        actor: WebActorIdentity,
        now: datetime | None = None,
    ) -> UploadSessionResponse:
        self._require_identified_actor(actor=actor)
        current_time = _as_utc(now)
        row = UploadSession(
            owner_user_id=actor.user_id,
            guest_session_hash=actor.guest_session_hash,
            expires_at=current_time + timedelta(seconds=self.settings.upload_session_ttl_seconds),
        )
        with self.session_factory() as database:
            database.add(row)
            try:
                database.commit()
                database.refresh(row)
            except SQLAlchemyError as error:
                database.rollback()
                raise UploadStorageError("Upload session metadata could not be stored.") from error
            return _session_response(row=row, items=())

    def get_session(
        self,
        *,
        actor: WebActorIdentity,
        session_id: str,
        now: datetime | None = None,
    ) -> UploadSessionResponse:
        with self.session_factory() as database:
            row = self._require_session(
                database=database,
                actor=actor,
                session_id=session_id,
                now=_as_utc(now),
            )
            return _session_response(row=row, items=tuple(row.items))

    def reserve_submission(
        self,
        *,
        actor: WebActorIdentity,
        session_id: str,
        trace_id: str,
        now: datetime | None = None,
    ) -> SubmissionReservation:
        current_time = _as_utc(now)
        with self.session_factory() as database:
            row = self._require_session(
                database=database,
                actor=actor,
                session_id=session_id,
                now=current_time,
                for_update=True,
            )
            if row.submission_status == "consumed":
                return SubmissionReservation(
                    row.id, row.submission_status, row.submission_trace_id, row.bound_core_task_id
                )
            if row.submission_status == "submitting":
                return SubmissionReservation(
                    row.id, row.submission_status, row.submission_trace_id, None
                )
            row.submission_status = "submitting"
            row.submission_trace_id = trace_id
            row.updated_at = current_time
            database.commit()
            # The caller acquired an open session. The persisted row is now
            # ``submitting`` so concurrent callers can be rejected, but returning
            # that value here would make the acquiring caller reject itself.
            return SubmissionReservation(
                row.id, "open", row.submission_trace_id, None
            )

    def mark_submission_retryable(
        self,
        *,
        actor: WebActorIdentity,
        session_id: str,
        trace_id: str,
        now: datetime | None = None,
    ) -> None:
        current_time = _as_utc(now)
        with self.session_factory() as database:
            row = self._require_session(
                database=database,
                actor=actor,
                session_id=session_id,
                now=current_time,
                for_update=True,
            )
            if row.submission_status == "consumed":
                return
            if row.submission_status == "submitting" and row.submission_trace_id == trace_id:
                row.submission_status = "open"
                row.submission_trace_id = None
                row.updated_at = current_time
                database.commit()

    def reset_stale_submission(
        self,
        *,
        actor: WebActorIdentity,
        session_id: str,
        trace_id: str,
        stale_after: timedelta,
        now: datetime | None = None,
    ) -> bool:
        current_time = _as_utc(now)
        with self.session_factory() as database:
            row = self._require_session(
                database=database,
                actor=actor,
                session_id=session_id,
                now=current_time,
                for_update=True,
            )
            if (
                row.submission_status != "submitting"
                or row.submission_trace_id != trace_id
                or row.task_id is not None
                or current_time - _as_utc(row.updated_at) < stale_after
            ):
                return False
            row.submission_status = "open"
            row.submission_trace_id = None
            row.updated_at = current_time
            database.commit()
            return True

    def bind_submission(
        self,
        *,
        actor: WebActorIdentity,
        session_id: str,
        trace_id: str,
        core_task_id: str,
        now: datetime | None = None,
    ) -> SubmissionReservation:
        current_time = _as_utc(now)
        with self.session_factory() as database:
            row = self._require_session(
                database=database,
                actor=actor,
                session_id=session_id,
                now=current_time,
                for_update=True,
            )
            if row.submission_status == "consumed":
                return SubmissionReservation(
                    row.id, row.submission_status, row.submission_trace_id, row.bound_core_task_id
                )
            if row.submission_trace_id != trace_id:
                raise UploadConflictError("Upload submission reservation does not match.")
            task = database.execute(
                select(WebTask).where(WebTask.core_task_id == core_task_id)
            ).scalar_one_or_none()
            if task is None:
                raise UploadUnavailableError("Submitted task projection is unavailable.")
            row.submission_status = "consumed"
            row.task_id = task.id
            row.bound_core_task_id = task.core_task_id
            row.updated_at = current_time
            database.commit()
            return SubmissionReservation(
                row.id, row.submission_status, row.submission_trace_id, row.bound_core_task_id
            )

    def upload_input_files(
        self,
        *,
        actor: WebActorIdentity,
        session_id: str,
        files: Sequence[UploadFileSource],
        now: datetime | None = None,
    ) -> tuple[UploadItemResponse, ...]:
        if len(files) == 0:
            raise UploadRequestError("At least one file is required.")
        normalized_names = tuple(_validate_display_name(source.display_name) for source in files)
        current_time = _as_utc(now)
        with self.session_factory() as database:
            session_row = self._require_session(
                database=database,
                actor=actor,
                session_id=session_id,
                now=current_time,
                for_update=True,
            )
            self._require_open_session(session_row)
            existing_bytes, existing_files = _session_accounting(session_row.items)
            self._enforce_file_count(existing_files=existing_files, added_files=len(files))
            pending: list[_PendingItem] = []
            temporary_paths: list[Path] = []
            published: list[Path] = []
            cumulative_bytes = 0
            try:
                session_path = self._ensure_session_path(session_id=session_row.id)
                for source, display_name in zip(files, normalized_names, strict=True):
                    item_id = str(uuid4())
                    temporary_path = session_path / f".tmp-{item_id}"
                    final_path = session_path / item_id
                    temporary_path.mkdir(mode=0o700)
                    temporary_paths.append(temporary_path)
                    written = self._copy_stream(
                        stream=source.stream,
                        destination=temporary_path / "content",
                        existing_session_bytes=existing_bytes + cumulative_bytes,
                    )
                    cumulative_bytes += written
                    pending.append(
                        _PendingItem(
                            item_id=item_id,
                            kind="input_file",
                            display_name=display_name,
                            file_count=1,
                            total_bytes=written,
                            temporary_path=temporary_path,
                            final_path=final_path,
                        )
                    )
                self._publish_pending(pending=pending, published=published)
                rows = self._store_pending(
                    database=database,
                    session_row=session_row,
                    pending=pending,
                    now=current_time,
                )
                return tuple(_item_response(row=row) for row in rows)
            except AnalysisUploadError:
                database.rollback()
                _cleanup_paths(self.settings.upload_root, (*temporary_paths, *published))
                raise
            except (OSError, SQLAlchemyError) as error:
                database.rollback()
                _cleanup_paths(self.settings.upload_root, (*temporary_paths, *published))
                raise UploadStorageError("Uploaded files could not be stored.") from error

    def upload_directory(
        self,
        *,
        actor: WebActorIdentity,
        session_id: str,
        display_name: str,
        files: Sequence[UploadDirectoryFileSource],
        now: datetime | None = None,
    ) -> UploadItemResponse:
        normalized_display_name = _validate_display_name(display_name)
        if len(files) == 0:
            raise UploadRequestError("A directory upload must contain at least one regular file.")
        normalized_paths = tuple(
            _canonical_relative_path(
                source.relative_path,
                max_length=self.settings.upload_max_relative_path_length,
            )
            for source in files
        )
        _validate_tree_collisions(normalized_paths)
        current_time = _as_utc(now)
        with self.session_factory() as database:
            session_row = self._require_session(
                database=database,
                actor=actor,
                session_id=session_id,
                now=current_time,
                for_update=True,
            )
            self._require_open_session(session_row)
            existing_bytes, existing_files = _session_accounting(session_row.items)
            self._enforce_file_count(existing_files=existing_files, added_files=len(files))
            item_id = str(uuid4())
            session_path: Path | None = None
            temporary_path: Path | None = None
            final_path: Path | None = None
            published: list[Path] = []
            try:
                session_path = self._ensure_session_path(session_id=session_row.id)
                temporary_path = session_path / f".tmp-{item_id}"
                final_path = session_path / item_id
                tree_path = temporary_path / "tree"
                tree_path.mkdir(parents=True, mode=0o700)
                total_bytes = 0
                for source, relative_path in zip(files, normalized_paths, strict=True):
                    destination = tree_path.joinpath(*relative_path.split("/"))
                    destination.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
                    written = self._copy_stream(
                        stream=source.stream,
                        destination=destination,
                        existing_session_bytes=existing_bytes + total_bytes,
                    )
                    total_bytes += written
                pending = _PendingItem(
                    item_id=item_id,
                    kind="input_directory",
                    display_name=normalized_display_name,
                    file_count=len(files),
                    total_bytes=total_bytes,
                    temporary_path=temporary_path,
                    final_path=final_path,
                )
                self._publish_pending(pending=(pending,), published=published)
                rows = self._store_pending(
                    database=database,
                    session_row=session_row,
                    pending=(pending,),
                    now=current_time,
                )
                return _item_response(row=rows[0])
            except AnalysisUploadError:
                database.rollback()
                _cleanup_paths(
                    self.settings.upload_root,
                    tuple(path for path in (temporary_path, *published) if path),
                )
                raise
            except (OSError, SQLAlchemyError) as error:
                database.rollback()
                _cleanup_paths(
                    self.settings.upload_root,
                    tuple(path for path in (temporary_path, *published) if path),
                )
                raise UploadStorageError("Uploaded directory could not be stored.") from error

    def upload_config(
        self,
        *,
        actor: WebActorIdentity,
        session_id: str,
        file: UploadFileSource,
        now: datetime | None = None,
    ) -> UploadItemResponse:
        display_name = _validate_display_name(file.display_name)
        current_time = _as_utc(now)
        with self.session_factory() as database:
            session_row = self._require_session(
                database=database,
                actor=actor,
                session_id=session_id,
                now=current_time,
                for_update=True,
            )
            self._require_open_session(session_row)
            if any(item.kind == "config_file" for item in session_row.items):
                raise UploadConflictError(
                    "This upload session already has a config file; delete it before replacing it."
                )
            existing_bytes, existing_files = _session_accounting(session_row.items)
            self._enforce_file_count(existing_files=existing_files, added_files=1)
            item_id = str(uuid4())
            temporary_path: Path | None = None
            published: list[Path] = []
            try:
                session_path = self._ensure_session_path(session_id=session_row.id)
                temporary_path = session_path / f".tmp-{item_id}"
                final_path = session_path / item_id
                temporary_path.mkdir(mode=0o700)
                written = self._copy_stream(
                    stream=file.stream,
                    # Existing `jelica analyze` recognizes its first config argument by
                    # a `.json` suffix. Keep that authoritative CLI convention internal;
                    # browser filenames remain display-only metadata.
                    destination=temporary_path / "config.json",
                    existing_session_bytes=existing_bytes,
                )
                pending = _PendingItem(
                    item_id=item_id,
                    kind="config_file",
                    display_name=display_name,
                    file_count=1,
                    total_bytes=written,
                    temporary_path=temporary_path,
                    final_path=final_path,
                )
                self._publish_pending(pending=(pending,), published=published)
                rows = self._store_pending(
                    database=database,
                    session_row=session_row,
                    pending=(pending,),
                    now=current_time,
                )
                return _item_response(row=rows[0])
            except AnalysisUploadError:
                database.rollback()
                _cleanup_paths(
                    self.settings.upload_root,
                    tuple(path for path in (temporary_path, *published) if path),
                )
                raise
            except (OSError, SQLAlchemyError) as error:
                database.rollback()
                _cleanup_paths(
                    self.settings.upload_root,
                    tuple(path for path in (temporary_path, *published) if path),
                )
                raise UploadStorageError("Uploaded config could not be stored.") from error

    def delete_item(
        self,
        *,
        actor: WebActorIdentity,
        session_id: str,
        item_id: str,
        now: datetime | None = None,
    ) -> None:
        with self.session_factory() as database:
            session_row = self._require_session(
                database=database,
                actor=actor,
                session_id=session_id,
                now=_as_utc(now),
                for_update=True,
            )
            self._require_open_session(session_row)
            item = next(
                (candidate for candidate in session_row.items if candidate.id == item_id),
                None,
            )
            if item is None:
                raise UploadUnavailableError("Upload item is unavailable.")
            item_path = self._item_root(session_id=session_row.id, item_id=item.id)
            try:
                database.delete(item)
                session_row.updated_at = _as_utc(now)
                database.commit()
                _remove_managed_tree(root=self.settings.upload_root, path=item_path)
            except (OSError, SQLAlchemyError) as error:
                database.rollback()
                raise UploadStorageError("Upload item could not be deleted.") from error

    def delete_session(
        self,
        *,
        actor: WebActorIdentity,
        session_id: str,
        now: datetime | None = None,
    ) -> None:
        with self.session_factory() as database:
            session_row = self._require_session(
                database=database,
                actor=actor,
                session_id=session_id,
                now=_as_utc(now),
                for_update=True,
            )
            session_path = self._session_path(session_id=session_row.id)
            try:
                database.delete(session_row)
                database.commit()
                _remove_managed_tree(root=self.settings.upload_root, path=session_path)
            except (OSError, SQLAlchemyError) as error:
                database.rollback()
                raise UploadStorageError("Upload session could not be deleted.") from error

    def cleanup_expired(self, *, now: datetime | None = None) -> int:
        current_time = _as_utc(now)
        removed = 0
        with self.session_factory() as database:
            rows = database.execute(
                select(UploadSession).where(
                    UploadSession.expires_at <= current_time,
                    UploadSession.submission_status == "open",
                )
            ).scalars()
            for row in rows:
                try:
                    _remove_managed_tree(
                        root=self.settings.upload_root,
                        path=self._session_path(session_id=row.id),
                    )
                    database.delete(row)
                    database.commit()
                except (OSError, SQLAlchemyError) as error:
                    database.rollback()
                    raise UploadStorageError("Expired uploads could not be cleaned up.") from error
                removed += 1
        return removed

    def resolve_item(
        self,
        *,
        actor: WebActorIdentity,
        session_id: str,
        item_id: str,
        now: datetime | None = None,
    ) -> MaterializedUploadItem:
        with self.session_factory() as database:
            session_row = self._require_session(
                database=database,
                actor=actor,
                session_id=session_id,
                now=_as_utc(now),
            )
            item = next(
                (candidate for candidate in session_row.items if candidate.id == item_id),
                None,
            )
            if item is None:
                raise UploadUnavailableError("Upload item is unavailable.")
            kind = _item_kind(item.kind)
            try:
                item_root = self._item_root(session_id=session_row.id, item_id=item.id)
                relative_materialized_name = {
                    "input_file": "content",
                    "input_directory": "tree",
                    "config_file": "config.json",
                }[kind]
                candidate = item_root / relative_materialized_name
                root = self.settings.upload_root.resolve(strict=True)
                item_root_stat = item_root.lstat()
                candidate_stat = candidate.lstat()
                resolved = candidate.resolve(strict=True)
            except (FileNotFoundError, OSError, UploadStorageError) as error:
                raise UploadUnavailableError("Upload item is unavailable.") from error
            if stat.S_ISLNK(item_root_stat.st_mode) or stat.S_ISLNK(candidate_stat.st_mode):
                raise UploadUnavailableError("Upload item is unavailable.")
            if not resolved.is_relative_to(root):
                raise UploadUnavailableError("Upload item is unavailable.")
            expected_type = stat.S_ISDIR if kind == "input_directory" else stat.S_ISREG
            if not expected_type(candidate_stat.st_mode):
                raise UploadUnavailableError("Upload item is unavailable.")
            return MaterializedUploadItem(
                session_id=session_row.id,
                item_id=item.id,
                kind=kind,
                path=resolved,
            )

    def _require_session(
        self,
        *,
        database: Session,
        actor: WebActorIdentity,
        session_id: str,
        now: datetime,
        for_update: bool = False,
    ) -> UploadSession:
        normalized_id = _normalize_uuid(session_id)
        statement = (
            select(UploadSession)
            .options(selectinload(UploadSession.items))
            .where(
                UploadSession.id == normalized_id,
                UploadSession.expires_at > now,
                _actor_predicate(actor=actor),
            )
        )
        if for_update:
            statement = statement.with_for_update()
        row = database.execute(statement).scalar_one_or_none()
        if row is None:
            raise UploadUnavailableError("Upload session is unavailable.")
        return row

    @staticmethod
    def _require_open_session(row: UploadSession) -> None:
        if row.submission_status != "open":
            raise UploadConflictError(
                "This upload session has already been submitted and is read-only."
            )

    def _copy_stream(
        self,
        *,
        stream: BinaryIO,
        destination: Path,
        existing_session_bytes: int,
    ) -> int:
        total = 0
        with destination.open("xb") as target:
            while True:
                chunk = stream.read(_COPY_CHUNK_BYTES)
                if not chunk:
                    break
                if not isinstance(chunk, bytes):
                    raise UploadRequestError("Uploaded file stream did not provide bytes.")
                total += len(chunk)
                if total > self.settings.upload_max_file_bytes:
                    raise UploadLimitError("Uploaded file exceeds the configured size limit.")
                if existing_session_bytes + total > self.settings.upload_max_session_bytes:
                    raise UploadLimitError(
                        "Upload session exceeds the configured total size limit."
                    )
                target.write(chunk)
            target.flush()
            os.fsync(target.fileno())
        return total

    def _enforce_file_count(self, *, existing_files: int, added_files: int) -> None:
        if existing_files + added_files > self.settings.upload_max_session_files:
            raise UploadLimitError("Upload session exceeds the configured file-count limit.")

    def _ensure_session_path(self, *, session_id: str) -> Path:
        try:
            self.settings.upload_root.mkdir(parents=True, mode=0o700, exist_ok=True)
            root = self.settings.upload_root.resolve(strict=True)
            if not root.is_dir():
                raise UploadStorageError("Configured upload root is not a directory.")
            session_path = self._session_path(session_id=session_id)
            session_path.mkdir(mode=0o700, exist_ok=True)
            if session_path.is_symlink() or not session_path.is_dir():
                raise UploadStorageError("Managed upload session path is unsafe.")
            return session_path
        except OSError as error:
            raise UploadStorageError("Configured upload storage is unavailable.") from error

    def _session_path(self, *, session_id: str) -> Path:
        return _contained_path(self.settings.upload_root, _normalize_uuid(session_id))

    def _item_root(self, *, session_id: str, item_id: str) -> Path:
        return _contained_path(
            self.settings.upload_root,
            _normalize_uuid(session_id),
            _normalize_uuid(item_id),
        )

    def _publish_pending(self, *, pending: Sequence[_PendingItem], published: list[Path]) -> None:
        for item in pending:
            if item.final_path.exists() or item.final_path.is_symlink():
                raise UploadStorageError("Managed upload item identity collided.")
            item.temporary_path.rename(item.final_path)
            published.append(item.final_path)

    @staticmethod
    def _store_pending(
        *,
        database: Session,
        session_row: UploadSession,
        pending: Sequence[_PendingItem],
        now: datetime,
    ) -> tuple[UploadItem, ...]:
        rows = tuple(
            UploadItem(
                id=item.item_id,
                session_id=session_row.id,
                kind=item.kind,
                display_name=item.display_name,
                file_count=item.file_count,
                total_bytes=item.total_bytes,
            )
            for item in pending
        )
        database.add_all(rows)
        session_row.updated_at = now
        database.commit()
        for row in rows:
            database.refresh(row)
        return rows

    @staticmethod
    def _require_identified_actor(*, actor: WebActorIdentity) -> None:
        if actor.user_id is None and actor.guest_session_hash is None:
            raise UploadUnavailableError("An identified Web actor is required.")


def _actor_predicate(*, actor: WebActorIdentity):
    if actor.user_id is not None:
        return and_(
            UploadSession.owner_user_id == actor.user_id,
            UploadSession.guest_session_hash.is_(None),
        )
    if actor.guest_session_hash is not None:
        return and_(
            UploadSession.owner_user_id.is_(None),
            UploadSession.guest_session_hash == actor.guest_session_hash,
        )
    return false()


def _normalize_uuid(value: str) -> str:
    try:
        return str(UUID(value))
    except (ValueError, AttributeError) as error:
        raise UploadUnavailableError("Upload is unavailable.") from error


def _validate_display_name(value: str) -> str:
    normalized = value.strip()
    if normalized == "" or len(normalized) > 512:
        raise UploadRequestError("Upload display name is invalid.")
    if any(ord(character) < 32 or ord(character) == 127 for character in normalized):
        raise UploadRequestError("Upload display name contains unsafe control characters.")
    return normalized


def _canonical_relative_path(value: str, *, max_length: int) -> str:
    if value == "" or len(value) > max_length:
        raise UploadRequestError("Directory relative path is empty or too long.")
    if value.startswith("/") or "\\" in value or "\x00" in value:
        raise UploadRequestError("Directory relative path is unsafe.")
    if len(value) >= 2 and value[0].isalpha() and value[1] == ":":
        raise UploadRequestError("Directory relative path is unsafe.")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise UploadRequestError("Directory relative path contains control characters.")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise UploadRequestError("Directory relative path is unsafe.")
    return "/".join(parts)


def _validate_tree_collisions(paths: Sequence[str]) -> None:
    known = set(paths)
    if len(known) != len(paths):
        raise UploadConflictError("Directory upload contains duplicate relative paths.")
    for path in paths:
        parts = path.split("/")
        for index in range(1, len(parts)):
            if "/".join(parts[:index]) in known:
                raise UploadConflictError("Directory upload contains a file/directory collision.")


def _contained_path(root: Path, *components: str) -> Path:
    resolved_root = root.resolve(strict=False)
    candidate = resolved_root.joinpath(*components)
    resolved_candidate = candidate.resolve(strict=False)
    if not resolved_candidate.is_relative_to(resolved_root):
        raise UploadStorageError("Managed upload path escaped the configured root.")
    return candidate


def _session_accounting(items: Sequence[UploadItem]) -> tuple[int, int]:
    return (
        sum(item.total_bytes for item in items),
        sum(item.file_count for item in items),
    )


def _item_kind(value: str) -> UploadItemKind:
    if value not in {"input_file", "input_directory", "config_file"}:
        raise UploadUnavailableError("Upload item is unavailable.")
    return value  # type: ignore[return-value]


def _item_response(*, row: UploadItem) -> UploadItemResponse:
    return UploadItemResponse(
        id=row.id,
        kind=_item_kind(row.kind),
        display_name=row.display_name,
        file_count=row.file_count,
        total_bytes=row.total_bytes,
        ready=True,
        created_at=row.created_at,
    )


def _session_response(*, row: UploadSession, items: Sequence[UploadItem]) -> UploadSessionResponse:
    ordered_items = tuple(sorted(items, key=lambda item: (item.created_at, item.id)))
    total_bytes, file_count = _session_accounting(ordered_items)
    return UploadSessionResponse(
        id=row.id,
        created_at=row.created_at,
        updated_at=row.updated_at,
        expires_at=row.expires_at,
        file_count=file_count,
        total_bytes=total_bytes,
        items=tuple(_item_response(row=item) for item in ordered_items),
        submission_status=row.submission_status,
        task_id=row.bound_core_task_id,
    )


def _remove_managed_tree(*, root: Path, path: Path) -> None:
    if not path.exists() and not path.is_symlink():
        return
    resolved_root = root.resolve(strict=True)
    if path.is_symlink():
        raise UploadStorageError("Managed upload path is unexpectedly a symlink.")
    resolved_path = path.resolve(strict=True)
    if resolved_path == resolved_root or not resolved_path.is_relative_to(resolved_root):
        raise UploadStorageError("Managed upload deletion path escaped its configured root.")
    shutil.rmtree(path)


def _cleanup_paths(root: Path, paths: Sequence[Path]) -> None:
    for path in paths:
        try:
            _remove_managed_tree(root=root, path=path)
        except (OSError, UploadStorageError):
            continue


def _as_utc(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(UTC)
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


__all__ = [
    "AnalysisUploadError",
    "AnalysisUploadService",
    "MaterializedUploadItem",
    "SubmissionReservation",
    "UploadConflictError",
    "UploadDirectoryFileSource",
    "UploadFileSource",
    "UploadLimitError",
    "UploadRequestError",
    "UploadStorageError",
    "UploadUnavailableError",
]
