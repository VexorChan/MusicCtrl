"""Thread-owned repository for the P1 read-only library index."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import threading
from collections.abc import Iterable, Iterator
from typing import Any
from uuid import uuid4

from database import DatabaseConfig, apply_migrations, open_database


ASSET_KINDS = frozenset({"audio", "lyric"})
ASSET_STATES = frozenset({"active", "missing", "external_changed"})
SCAN_MODES = frozenset({"audio", "lyric"})
SCAN_SESSION_FINAL_STATES = frozenset({"cancelled", "completed", "failed"})
SCAN_ITEM_STATES = frozenset({"waiting", "indexed", "skipped", "failed"})


class RepositoryError(RuntimeError):
    """Base error for repository boundary violations and corrupt data."""


class RepositoryPathError(RepositoryError, ValueError):
    """Raised when a caller supplies a non-absolute path."""


class RepositoryThreadError(RepositoryError):
    """Raised when a repository is used outside its creating thread."""


class RepositoryClosedError(RepositoryError):
    """Raised when a closed repository is used again."""


class RepositoryDataError(RepositoryError, ValueError):
    """Raised for invalid input data or corrupt persisted JSON."""


class RecordNotFoundError(RepositoryError):
    """Raised when an operation requires a record that does not exist."""


@dataclass(frozen=True, slots=True)
class AssetUpsert:
    canonical_path: Path
    size_bytes: int
    mtime_ns: int | None = None
    kind: str = "audio"
    file_state: str = "active"


@dataclass(frozen=True, slots=True)
class AssetRecord:
    id: str
    kind: str
    canonical_path: Path
    normalized_path: str
    file_name: str
    extension: str
    size_bytes: int
    mtime_ns: int | None
    file_state: str
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class SettingRecord:
    key: str
    value: object
    updated_at: str


@dataclass(frozen=True, slots=True)
class ScanSessionRecord:
    id: str
    mode: str
    source_folder: Path
    status: str
    started_at: str
    completed_at: str | None


@dataclass(frozen=True, slots=True)
class ScanItemInput:
    source_path: Path
    size_bytes: int | None
    status: str = "waiting"
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class ScanItemRecord:
    id: str
    session_id: str
    source_path: Path
    size_bytes: int | None
    status: str
    reason: str | None


@dataclass(frozen=True, slots=True)
class IndexBatchItem:
    """One source file that must update its asset and scan item atomically."""

    canonical_path: Path
    size_bytes: int
    mtime_ns: int | None
    kind: str = "audio"
    file_state: str = "active"


@dataclass(frozen=True, slots=True)
class IndexBatchRecord:
    asset: AssetRecord
    scan_item: ScanItemRecord


@dataclass(frozen=True, slots=True)
class ScanReconcileResult:
    session_id: str
    seen_count: int
    active_count: int
    external_changed_count: int
    missing_count: int


@dataclass(frozen=True, slots=True)
class _PreparedAsset:
    canonical_path: Path
    normalized_path: str
    file_name: str
    extension: str
    size_bytes: int
    mtime_ns: int | None
    kind: str
    file_state: str


@dataclass(frozen=True, slots=True)
class _PreparedScanItem:
    source_path: Path
    normalized_path: str
    size_bytes: int | None
    status: str
    reason: str | None


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _canonicalize_path(path: Path, *, field_name: str) -> tuple[Path, str]:
    if not isinstance(path, Path):
        raise RepositoryPathError(f"{field_name} 必须使用 pathlib.Path")
    if not path.is_absolute():
        raise RepositoryPathError(f"{field_name} 必须是绝对路径：{path}")

    canonical_text = os.path.abspath(os.fspath(path))
    canonical_text = os.path.normpath(canonical_text)
    canonical_path = Path(canonical_text)
    normalized_path = os.path.normcase(canonical_text).replace("\\", "/")
    return canonical_path, normalized_path


def _require_path_within_root(
    normalized_path: str,
    normalized_root: str,
    *,
    field_name: str,
) -> None:
    try:
        common_path = os.path.commonpath((normalized_root, normalized_path))
    except ValueError as exc:
        raise RepositoryPathError(
            f"{field_name} 不在扫描根目录内：{normalized_path}"
        ) from exc
    normalized_common = os.path.normcase(os.path.normpath(common_path)).replace("\\", "/")
    if normalized_common != normalized_root:
        raise RepositoryPathError(f"{field_name} 不在扫描根目录内：{normalized_path}")


def _require_non_negative_integer(value: int | None, *, field_name: str, optional: bool) -> None:
    if value is None and optional:
        return
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        qualifier = "非负整数或 None" if optional else "非负整数"
        raise RepositoryDataError(f"{field_name} 必须是{qualifier}")


def _require_choice(value: str, choices: frozenset[str], *, field_name: str) -> None:
    if value not in choices:
        allowed = "、".join(sorted(choices))
        raise RepositoryDataError(f"{field_name} 必须是以下值之一：{allowed}")


def _strict_json_loads(value_json: str) -> object:
    def reject_constant(value: str) -> Any:
        raise ValueError(f"不允许的 JSON 常量：{value}")

    try:
        return json.loads(value_json, parse_constant=reject_constant)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RepositoryDataError("设置包含损坏或非标准 JSON") from exc


class LibraryRepository:
    """Own one SQLite connection for exactly one creating thread."""

    def __init__(self, config: DatabaseConfig):
        self._owner_thread_id = threading.get_ident()
        self._closed = False
        self._connection = open_database(config)
        try:
            apply_migrations(self._connection, config.path)
        except Exception:
            self._connection.close()
            self._closed = True
            raise

    def __enter__(self) -> LibraryRepository:
        self._require_open_in_owner_thread()
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()

    def _require_owner_thread(self) -> None:
        if threading.get_ident() != self._owner_thread_id:
            raise RepositoryThreadError("LibraryRepository 只能在创建它的线程中使用和关闭")

    def _require_open_in_owner_thread(self) -> None:
        self._require_owner_thread()
        if self._closed:
            raise RepositoryClosedError("LibraryRepository 已关闭")

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            yield
            self._connection.execute("COMMIT")
        except BaseException:
            if self._connection.in_transaction:
                self._connection.execute("ROLLBACK")
            raise

    def close(self) -> None:
        self._require_owner_thread()
        if self._closed:
            raise RepositoryClosedError("LibraryRepository 已关闭")
        self._connection.close()
        self._closed = True

    def _prepare_asset(self, item: AssetUpsert) -> _PreparedAsset:
        if not isinstance(item, AssetUpsert):
            raise RepositoryDataError("资产必须使用 AssetUpsert")
        canonical_path, normalized_path = _canonicalize_path(
            item.canonical_path,
            field_name="canonical_path",
        )
        _require_non_negative_integer(item.size_bytes, field_name="size_bytes", optional=False)
        _require_non_negative_integer(item.mtime_ns, field_name="mtime_ns", optional=True)
        _require_choice(item.kind, ASSET_KINDS, field_name="kind")
        _require_choice(item.file_state, ASSET_STATES, field_name="file_state")
        return _PreparedAsset(
            canonical_path=canonical_path,
            normalized_path=normalized_path,
            file_name=canonical_path.name,
            extension=canonical_path.suffix.casefold(),
            size_bytes=item.size_bytes,
            mtime_ns=item.mtime_ns,
            kind=item.kind,
            file_state=item.file_state,
        )

    @staticmethod
    def _asset_from_row(row: sqlite3.Row) -> AssetRecord:
        return AssetRecord(
            id=row["id"],
            kind=row["kind"],
            canonical_path=Path(row["canonical_path"]),
            normalized_path=row["normalized_path"],
            file_name=row["file_name"],
            extension=row["extension"],
            size_bytes=row["size_bytes"],
            mtime_ns=row["mtime_ns"],
            file_state=row["file_state"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def _upsert_prepared_asset(self, item: _PreparedAsset) -> AssetRecord:
        now = _utc_now()
        self._connection.execute(
            """
            INSERT INTO assets(
                id, kind, canonical_path, normalized_path, file_name, extension,
                size_bytes, mtime_ns, file_state, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(normalized_path) DO UPDATE SET
                kind = excluded.kind,
                canonical_path = excluded.canonical_path,
                file_name = excluded.file_name,
                extension = excluded.extension,
                size_bytes = excluded.size_bytes,
                mtime_ns = excluded.mtime_ns,
                file_state = excluded.file_state,
                updated_at = excluded.updated_at
            """,
            (
                str(uuid4()),
                item.kind,
                os.fspath(item.canonical_path),
                item.normalized_path,
                item.file_name,
                item.extension,
                item.size_bytes,
                item.mtime_ns,
                item.file_state,
                now,
                now,
            ),
        )
        row = self._connection.execute(
            "SELECT * FROM assets WHERE normalized_path = ?",
            (item.normalized_path,),
        ).fetchone()
        if row is None:
            raise RepositoryDataError("资产写入后无法读取")
        return self._asset_from_row(row)

    def _upsert_indexed_asset(self, item: _PreparedAsset) -> AssetRecord:
        existing = self._connection.execute(
            "SELECT size_bytes, mtime_ns FROM assets WHERE normalized_path = ?",
            (item.normalized_path,),
        ).fetchone()
        file_state = "active"
        if existing is not None and (
            existing["size_bytes"] != item.size_bytes
            or existing["mtime_ns"] != item.mtime_ns
        ):
            file_state = "external_changed"
        indexed_item = _PreparedAsset(
            canonical_path=item.canonical_path,
            normalized_path=item.normalized_path,
            file_name=item.file_name,
            extension=item.extension,
            size_bytes=item.size_bytes,
            mtime_ns=item.mtime_ns,
            kind=item.kind,
            file_state=file_state,
        )
        return self._upsert_prepared_asset(indexed_item)

    def upsert_asset(self, item: AssetUpsert) -> AssetRecord:
        self._require_open_in_owner_thread()
        return self.upsert_assets((item,))[0]

    def upsert_assets(self, items: Iterable[AssetUpsert]) -> tuple[AssetRecord, ...]:
        self._require_open_in_owner_thread()
        prepared = tuple(self._prepare_asset(item) for item in items)
        if not prepared:
            return ()

        records: list[AssetRecord] = []
        with self._transaction():
            for item in prepared:
                records.append(self._upsert_prepared_asset(item))
        return tuple(records)

    def get_asset_by_path(self, canonical_path: Path) -> AssetRecord | None:
        self._require_open_in_owner_thread()
        _, normalized_path = _canonicalize_path(canonical_path, field_name="canonical_path")
        row = self._connection.execute(
            "SELECT * FROM assets WHERE normalized_path = ?",
            (normalized_path,),
        ).fetchone()
        return None if row is None else self._asset_from_row(row)

    def list_assets(
        self,
        *,
        kind: str | None = None,
        file_state: str | None = None,
    ) -> tuple[AssetRecord, ...]:
        self._require_open_in_owner_thread()
        clauses: list[str] = []
        parameters: list[str] = []
        if kind is not None:
            _require_choice(kind, ASSET_KINDS, field_name="kind")
            clauses.append("kind = ?")
            parameters.append(kind)
        if file_state is not None:
            _require_choice(file_state, ASSET_STATES, field_name="file_state")
            clauses.append("file_state = ?")
            parameters.append(file_state)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._connection.execute(
            f"SELECT * FROM assets{where} ORDER BY normalized_path, id",
            parameters,
        ).fetchall()
        return tuple(self._asset_from_row(row) for row in rows)

    def latest_completed_audio_roots(
        self,
        asset_ids: Iterable[str],
    ) -> dict[str, Path]:
        """Return each audio asset's latest trustworthy completed scan root.

        The provenance is reconstructed from the existing scan history.  This
        method deliberately performs no writes and does not fall back to an
        asset's parent directory when history is absent or invalid.
        """

        self._require_open_in_owner_thread()
        requested: list[str] = []
        seen_ids: set[str] = set()
        for asset_id in asset_ids:
            if not isinstance(asset_id, str) or not asset_id.strip():
                raise RepositoryDataError("asset_id 必须是非空字符串")
            if asset_id not in seen_ids:
                requested.append(asset_id)
                seen_ids.add(asset_id)
        if not requested:
            return {}

        placeholders = ", ".join("?" for _ in requested)
        asset_rows = self._connection.execute(
            f"""
            SELECT id, kind, canonical_path, normalized_path
            FROM assets
            WHERE id IN ({placeholders})
            """,
            requested,
        ).fetchall()
        normalized_to_ids: dict[str, list[str]] = {}
        for row in asset_rows:
            if row["kind"] != "audio":
                continue
            try:
                _, normalized_path = _canonicalize_path(
                    Path(row["canonical_path"]),
                    field_name="assets.canonical_path",
                )
            except (RepositoryError, TypeError, ValueError) as exc:
                raise RepositoryDataError("资产路径记录损坏，无法确认扫描来源") from exc
            if normalized_path != row["normalized_path"]:
                raise RepositoryDataError("资产规范路径记录不一致，无法确认扫描来源")
            normalized_to_ids.setdefault(normalized_path, []).append(row["id"])

        if not normalized_to_ids:
            return {}

        candidate_rows = self._connection.execute(
            """
            SELECT
                si.source_path,
                ss.id AS session_id,
                ss.source_folder,
                ss.started_at,
                ss.completed_at
            FROM scan_items AS si
            JOIN scan_sessions AS ss ON ss.id = si.session_id
            WHERE ss.mode = 'audio'
              AND ss.status = 'completed'
              AND si.status = 'indexed'
            ORDER BY ss.completed_at DESC, ss.started_at DESC, ss.id DESC
            """
        ).fetchall()

        roots: dict[str, Path] = {}
        for row in candidate_rows:
            if not isinstance(row["started_at"], str) or not row["started_at"]:
                raise RepositoryDataError("扫描会话开始时间损坏，无法确认资产来源")
            if not isinstance(row["completed_at"], str) or not row["completed_at"]:
                raise RepositoryDataError("已完成扫描会话缺少完成时间")
            try:
                source_path, normalized_source = _canonicalize_path(
                    Path(row["source_path"]),
                    field_name="scan_items.source_path",
                )
                source_root, normalized_root = _canonicalize_path(
                    Path(row["source_folder"]),
                    field_name="scan_sessions.source_folder",
                )
                _require_path_within_root(
                    normalized_source,
                    normalized_root,
                    field_name="扫描条目路径",
                )
            except (RepositoryError, TypeError, ValueError) as exc:
                raise RepositoryDataError("扫描来源记录损坏，无法安全授权资产路径") from exc
            matching_ids = normalized_to_ids.get(normalized_source)
            if not matching_ids:
                continue
            for asset_id in matching_ids:
                roots.setdefault(asset_id, source_root)
        return roots

    def set_setting(self, key: str, value: object) -> SettingRecord:
        self._require_open_in_owner_thread()
        if not isinstance(key, str) or not key.strip():
            raise RepositoryDataError("设置 key 必须是非空字符串")
        try:
            value_json = json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        except (TypeError, ValueError) as exc:
            raise RepositoryDataError("设置值必须兼容标准 JSON，且不能包含 NaN 或 Infinity") from exc

        updated_at = _utc_now()
        with self._transaction():
            self._connection.execute(
                """
                INSERT INTO settings(key, value_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value_json = excluded.value_json,
                    updated_at = excluded.updated_at
                """,
                (key, value_json, updated_at),
            )
        return SettingRecord(key=key, value=_strict_json_loads(value_json), updated_at=updated_at)

    def get_setting(self, key: str) -> SettingRecord | None:
        self._require_open_in_owner_thread()
        if not isinstance(key, str) or not key.strip():
            raise RepositoryDataError("设置 key 必须是非空字符串")
        row = self._connection.execute(
            "SELECT key, value_json, updated_at FROM settings WHERE key = ?",
            (key,),
        ).fetchone()
        if row is None:
            return None
        return SettingRecord(
            key=row["key"],
            value=_strict_json_loads(row["value_json"]),
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _scan_session_from_row(row: sqlite3.Row) -> ScanSessionRecord:
        return ScanSessionRecord(
            id=row["id"],
            mode=row["mode"],
            source_folder=Path(row["source_folder"]),
            status=row["status"],
            started_at=row["started_at"],
            completed_at=row["completed_at"],
        )

    def create_scan_session(self, *, mode: str, source_folder: Path) -> ScanSessionRecord:
        self._require_open_in_owner_thread()
        _require_choice(mode, SCAN_MODES, field_name="mode")
        canonical_folder, _ = _canonicalize_path(source_folder, field_name="source_folder")
        session_id = str(uuid4())
        started_at = _utc_now()
        with self._transaction():
            self._connection.execute(
                """
                INSERT INTO scan_sessions(id, mode, source_folder, status, started_at)
                VALUES (?, ?, ?, 'running', ?)
                """,
                (session_id, mode, os.fspath(canonical_folder), started_at),
            )
        return ScanSessionRecord(
            id=session_id,
            mode=mode,
            source_folder=canonical_folder,
            status="running",
            started_at=started_at,
            completed_at=None,
        )

    def get_scan_session(self, session_id: str) -> ScanSessionRecord | None:
        self._require_open_in_owner_thread()
        row = self._connection.execute(
            "SELECT * FROM scan_sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
        return None if row is None else self._scan_session_from_row(row)

    def finish_scan_session(self, session_id: str, *, status: str) -> ScanSessionRecord:
        self._require_open_in_owner_thread()
        _require_choice(status, SCAN_SESSION_FINAL_STATES, field_name="status")
        completed_at = _utc_now()
        with self._transaction():
            cursor = self._connection.execute(
                """
                UPDATE scan_sessions
                SET status = ?, completed_at = ?
                WHERE id = ? AND status = 'running'
                """,
                (status, completed_at, session_id),
            )
            if cursor.rowcount != 1:
                existing = self._connection.execute(
                    "SELECT status FROM scan_sessions WHERE id = ?",
                    (session_id,),
                ).fetchone()
                if existing is None:
                    raise RecordNotFoundError(f"扫描会话不存在：{session_id}")
                raise RepositoryDataError(
                    f"扫描会话已经结束，不能重复终结：{session_id}"
                )
        record = self.get_scan_session(session_id)
        if record is None:
            raise RecordNotFoundError(f"扫描会话不存在：{session_id}")
        return record

    def _prepare_scan_item(self, item: ScanItemInput) -> _PreparedScanItem:
        if not isinstance(item, ScanItemInput):
            raise RepositoryDataError("扫描条目必须使用 ScanItemInput")
        canonical_path, normalized_path = _canonicalize_path(
            item.source_path,
            field_name="source_path",
        )
        _require_non_negative_integer(item.size_bytes, field_name="size_bytes", optional=True)
        _require_choice(item.status, SCAN_ITEM_STATES, field_name="status")
        if item.reason is not None and not isinstance(item.reason, str):
            raise RepositoryDataError("reason 必须是字符串或 None")
        return _PreparedScanItem(
            source_path=canonical_path,
            normalized_path=normalized_path,
            size_bytes=item.size_bytes,
            status=item.status,
            reason=item.reason,
        )

    @staticmethod
    def _scan_item_from_row(row: sqlite3.Row) -> ScanItemRecord:
        return ScanItemRecord(
            id=row["id"],
            session_id=row["session_id"],
            source_path=Path(row["source_path"]),
            size_bytes=row["size_bytes"],
            status=row["status"],
            reason=row["reason"],
        )

    def _require_running_session(self, session_id: str) -> sqlite3.Row:
        session = self._connection.execute(
            "SELECT * FROM scan_sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
        if session is None:
            raise RecordNotFoundError(f"扫描会话不存在：{session_id}")
        if session["status"] != "running":
            raise RepositoryDataError(f"扫描会话已经结束，不能继续写入条目：{session_id}")
        return session

    @staticmethod
    def _normalized_session_root(session: sqlite3.Row) -> str:
        _, normalized_root = _canonicalize_path(
            Path(session["source_folder"]),
            field_name="source_folder",
        )
        return normalized_root

    def _validated_session_paths(
        self,
        session_id: str,
        normalized_root: str,
    ) -> set[str]:
        rows = self._connection.execute(
            "SELECT source_path FROM scan_items WHERE session_id = ?",
            (session_id,),
        ).fetchall()
        paths: set[str] = set()
        for row in rows:
            _, normalized_path = _canonicalize_path(
                Path(row["source_path"]),
                field_name="scan_items.source_path",
            )
            _require_path_within_root(
                normalized_path,
                normalized_root,
                field_name="扫描条目路径",
            )
            paths.add(normalized_path)
        return paths

    def _previous_completed_session_id(
        self,
        current_session_id: str,
        normalized_root: str,
    ) -> str | None:
        rows = self._connection.execute(
            """
            SELECT id, source_folder
            FROM scan_sessions
            WHERE mode = 'audio' AND status = 'completed' AND id != ?
            ORDER BY completed_at DESC, started_at DESC, id DESC
            """,
            (current_session_id,),
        ).fetchall()
        for row in rows:
            _, candidate_root = _canonicalize_path(
                Path(row["source_folder"]),
                field_name="scan_sessions.source_folder",
            )
            if candidate_root == normalized_root:
                return row["id"]
        return None

    def _replace_reconcile_temp_paths(
        self,
        table_name: str,
        paths: set[str],
    ) -> None:
        if table_name not in {"reconcile_current_paths", "reconcile_previous_paths"}:
            raise RepositoryDataError("非法的重新校准临时表")
        self._connection.execute(
            f"CREATE TEMP TABLE IF NOT EXISTS {table_name}(normalized_path TEXT PRIMARY KEY)"
        )
        self._connection.execute(f"DELETE FROM {table_name}")
        if paths:
            self._connection.executemany(
                f"INSERT INTO {table_name}(normalized_path) VALUES (?)",
                ((path,) for path in sorted(paths)),
            )

    def _insert_prepared_scan_item(
        self,
        session_id: str,
        item: _PreparedScanItem,
    ) -> ScanItemRecord:
        item_id = str(uuid4())
        self._connection.execute(
            """
            INSERT INTO scan_items(
                id, session_id, source_path, size_bytes, status, reason
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                item_id,
                session_id,
                item.normalized_path,
                item.size_bytes,
                item.status,
                item.reason,
            ),
        )
        row = self._connection.execute(
            "SELECT * FROM scan_items WHERE id = ?",
            (item_id,),
        ).fetchone()
        if row is None:
            raise RepositoryDataError("扫描条目写入后无法读取")
        return self._scan_item_from_row(row)

    def add_scan_items(
        self,
        session_id: str,
        items: Iterable[ScanItemInput],
    ) -> tuple[ScanItemRecord, ...]:
        self._require_open_in_owner_thread()
        prepared = tuple(self._prepare_scan_item(item) for item in items)
        if not prepared:
            return ()
        records: list[ScanItemRecord] = []
        with self._transaction():
            self._require_running_session(session_id)
            for item in prepared:
                records.append(self._insert_prepared_scan_item(session_id, item))
        return tuple(records)

    def index_scan_batch(
        self,
        session_id: str,
        items: Iterable[IndexBatchItem],
    ) -> tuple[IndexBatchRecord, ...]:
        """Atomically upsert assets and insert their matching scan items."""

        self._require_open_in_owner_thread()
        prepared: list[tuple[_PreparedAsset, _PreparedScanItem]] = []
        for item in items:
            if not isinstance(item, IndexBatchItem):
                raise RepositoryDataError("索引批次必须使用 IndexBatchItem")
            asset = self._prepare_asset(
                AssetUpsert(
                    canonical_path=item.canonical_path,
                    size_bytes=item.size_bytes,
                    mtime_ns=item.mtime_ns,
                    kind=item.kind,
                    file_state=item.file_state,
                )
            )
            scan_item = _PreparedScanItem(
                source_path=asset.canonical_path,
                normalized_path=asset.normalized_path,
                size_bytes=asset.size_bytes,
                status="indexed",
                reason=None,
            )
            prepared.append((asset, scan_item))
        if not prepared:
            return ()

        records: list[IndexBatchRecord] = []
        with self._transaction():
            session = self._require_running_session(session_id)
            normalized_root = self._normalized_session_root(session)
            for asset, _scan_item in prepared:
                if asset.kind != session["mode"]:
                    raise RepositoryDataError("索引资产类型必须与扫描会话模式一致")
                _require_path_within_root(
                    asset.normalized_path,
                    normalized_root,
                    field_name="索引文件路径",
                )
            for asset, scan_item in prepared:
                asset_record = self._upsert_indexed_asset(asset)
                scan_item_record = self._insert_prepared_scan_item(session_id, scan_item)
                records.append(IndexBatchRecord(asset_record, scan_item_record))
        return tuple(records)

    def complete_scan_and_reconcile(self, session_id: str) -> ScanReconcileResult:
        """Complete one audio scan and atomically reconcile its exact-root baseline."""

        self._require_open_in_owner_thread()
        result: ScanReconcileResult | None = None
        with self._transaction():
            session = self._require_running_session(session_id)
            if session["mode"] != "audio":
                raise RepositoryDataError("只有运行中的音频扫描会话可以重新校准音乐库")
            normalized_root = self._normalized_session_root(session)
            current_paths = self._validated_session_paths(session_id, normalized_root)
            previous_session_id = self._previous_completed_session_id(
                session_id,
                normalized_root,
            )
            previous_paths = (
                set()
                if previous_session_id is None
                else self._validated_session_paths(previous_session_id, normalized_root)
            )
            self._replace_reconcile_temp_paths("reconcile_current_paths", current_paths)
            self._replace_reconcile_temp_paths("reconcile_previous_paths", previous_paths)

            now = _utc_now()
            missing_cursor = self._connection.execute(
                """
                UPDATE assets
                SET file_state = 'missing', updated_at = ?
                WHERE kind = 'audio'
                  AND file_state = 'active'
                  AND EXISTS (
                      SELECT 1 FROM reconcile_previous_paths AS previous
                      WHERE previous.normalized_path = assets.normalized_path
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM reconcile_current_paths AS current
                      WHERE current.normalized_path = assets.normalized_path
                  )
                """,
                (now,),
            )
            state_rows = self._connection.execute(
                """
                SELECT assets.file_state, COUNT(*) AS count
                FROM assets
                JOIN reconcile_current_paths AS current
                  ON current.normalized_path = assets.normalized_path
                WHERE assets.kind = 'audio'
                GROUP BY assets.file_state
                """
            ).fetchall()
            state_counts = {row["file_state"]: row["count"] for row in state_rows}
            completed_cursor = self._connection.execute(
                """
                UPDATE scan_sessions
                SET status = 'completed', completed_at = ?
                WHERE id = ? AND status = 'running' AND mode = 'audio'
                """,
                (now, session_id),
            )
            if completed_cursor.rowcount != 1:
                raise RepositoryDataError(f"扫描会话无法完成重新校准：{session_id}")
            result = ScanReconcileResult(
                session_id=session_id,
                seen_count=len(current_paths),
                active_count=int(state_counts.get("active", 0)),
                external_changed_count=int(state_counts.get("external_changed", 0)),
                missing_count=missing_cursor.rowcount,
            )
        if result is None:
            raise RepositoryDataError("扫描重新校准未产生结果")
        return result

    def list_scan_items(self, session_id: str) -> tuple[ScanItemRecord, ...]:
        self._require_open_in_owner_thread()
        rows = self._connection.execute(
            """
            SELECT * FROM scan_items
            WHERE session_id = ?
            ORDER BY source_path COLLATE NOCASE, source_path, id
            """,
            (session_id,),
        ).fetchall()
        return tuple(self._scan_item_from_row(row) for row in rows)
