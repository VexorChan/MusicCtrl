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
RENAME_OPERATION_STATES = frozenset(
    {"planned", "running", "success", "partial", "failed", "cancelled"}
)
RENAME_ITEM_STATES = frozenset(
    {
        "planned",
        "running",
        "success",
        "failed",
        "rolled_back",
        "rollback_failed",
        "cancelled",
    }
)
RENAME_ITEM_OUTCOMES = RENAME_ITEM_STATES - {"planned", "running", "success"}
RENAME_FAILURE_RESULTS = frozenset({"failed", "rolled_back", "rollback_failed"})
LYRICS_MATCH_SOURCES = frozenset({"embedded", "external"})
LYRICS_MATCH_METHODS = frozenset({"automatic", "manual"})


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


class RepositoryCommitOutcomeUnknown(RepositoryError):
    """Raised when COMMIT failed after SQLite left the transaction."""

    def __init__(self, message: str, *, operation_id: str, item_id: str) -> None:
        super().__init__(message)
        self.operation_id = operation_id
        self.item_id = item_id


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
class RenamePlanItem:
    asset_id: str
    source_path: Path
    target_path: Path
    expected_size_bytes: int
    expected_mtime_ns: int | None


@dataclass(frozen=True, slots=True)
class RenameOperationRecord:
    id: str
    operation_type: str
    status: str
    success_count: int
    failure_count: int
    summary: object
    created_at: str
    started_at: str | None
    completed_at: str | None


@dataclass(frozen=True, slots=True)
class RenameOperationItemRecord:
    id: str
    operation_id: str
    asset_id: str
    source_path: Path
    normalized_source_path: str
    target_path: Path
    normalized_target_path: str
    expected_size_bytes: int
    expected_mtime_ns: int | None
    result: str
    error_code: str | None
    error_message: str | None
    before: object
    after: object | None
    created_at: str
    completed_at: str | None


@dataclass(frozen=True, slots=True)
class LyricsMatchRecord:
    id: str
    audio_asset_id: str
    lyric_asset_id: str | None
    source_kind: str
    confidence: int
    method: str
    state: str
    is_current: bool
    created_at: str
    updated_at: str


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


def _strict_json_dumps(value: object, *, field_name: str) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise RepositoryDataError(f"{field_name} 必须兼容严格 JSON") from exc


def _strict_json_field(value_json: str, *, field_name: str) -> object:
    def reject_constant(value: str) -> Any:
        raise ValueError(f"不允许的 JSON 常量：{value}")

    try:
        return json.loads(value_json, parse_constant=reject_constant)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RepositoryDataError(f"{field_name} 包含损坏或非标准 JSON") from exc


def _windows_path_key(path: Path, *, field_name: str) -> tuple[Path, str]:
    canonical_path, _ = _canonicalize_path(path, field_name=field_name)
    parent_text = os.path.normcase(os.path.normpath(os.fspath(canonical_path.parent)))
    parent_key = parent_text.replace("\\", "/").casefold().rstrip("/")
    name_key = canonical_path.name.rstrip(" .").casefold()
    return canonical_path, f"{parent_key}/{name_key}"


def _validate_windows_leaf_name(path: Path, *, field_name: str) -> None:
    name = path.name
    if not name or name in {".", ".."}:
        raise RepositoryPathError(f"{field_name} 缺少有效文件名")
    if name.endswith((" ", ".")):
        raise RepositoryPathError(f"{field_name} 不能以空格或句点结尾")
    if any(ord(character) < 32 or character in '<>:"/\\|?*' for character in name):
        raise RepositoryPathError(f"{field_name} 包含 Windows 非法字符")
    device_name = name.split(".", 1)[0].rstrip(" .").casefold()
    reserved = {"con", "prn", "aux", "nul"}
    reserved.update(f"com{index}" for index in range(1, 10))
    reserved.update(f"lpt{index}" for index in range(1, 10))
    if device_name in reserved:
        raise RepositoryPathError(f"{field_name} 使用了 Windows 保留设备名")


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
    def _transaction(
        self,
        *,
        operation_id: str | None = None,
        item_id: str | None = None,
    ) -> Iterator[None]:
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            if self._connection.in_transaction:
                self._connection.execute("ROLLBACK")
            raise
        try:
            self._connection.execute("COMMIT")
        except BaseException as exc:
            if self._connection.in_transaction:
                self._connection.execute("ROLLBACK")
                raise
            if operation_id is not None and item_id is not None:
                raise RepositoryCommitOutcomeUnknown(
                    "SQLite COMMIT 后发生异常，提交结果未知，必须重新读取确认",
                    operation_id=operation_id,
                    item_id=item_id,
                ) from exc
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

    def get_asset_by_id(self, asset_id: str) -> AssetRecord | None:
        self._require_open_in_owner_thread()
        if not isinstance(asset_id, str) or not asset_id.strip():
            raise RepositoryDataError("asset_id 必须是非空字符串")
        row = self._connection.execute(
            "SELECT * FROM assets WHERE id = ?",
            (asset_id,),
        ).fetchone()
        return None if row is None else self._asset_from_row(row)

    @staticmethod
    def _rename_operation_from_row(row: sqlite3.Row) -> RenameOperationRecord:
        return RenameOperationRecord(
            id=row["id"],
            operation_type=row["operation_type"],
            status=row["status"],
            success_count=row["success_count"],
            failure_count=row["failure_count"],
            summary=_strict_json_field(row["summary_json"], field_name="summary_json"),
            created_at=row["created_at"],
            started_at=row["started_at"],
            completed_at=row["completed_at"],
        )

    @staticmethod
    def _rename_item_from_row(row: sqlite3.Row) -> RenameOperationItemRecord:
        return RenameOperationItemRecord(
            id=row["id"],
            operation_id=row["operation_id"],
            asset_id=row["asset_id"],
            source_path=Path(row["source_path"]),
            normalized_source_path=row["normalized_source_path"],
            target_path=Path(row["target_path"]),
            normalized_target_path=row["normalized_target_path"],
            expected_size_bytes=row["expected_size_bytes"],
            expected_mtime_ns=row["expected_mtime_ns"],
            result=row["result"],
            error_code=row["error_code"],
            error_message=row["error_message"],
            before=_strict_json_field(row["before_json"], field_name="before_json"),
            after=(
                None
                if row["after_json"] is None
                else _strict_json_field(row["after_json"], field_name="after_json")
            ),
            created_at=row["created_at"],
            completed_at=row["completed_at"],
        )

    def get_rename_operation(self, operation_id: str) -> RenameOperationRecord | None:
        self._require_open_in_owner_thread()
        if not isinstance(operation_id, str) or not operation_id.strip():
            raise RepositoryDataError("operation_id 必须是非空字符串")
        row = self._connection.execute(
            "SELECT * FROM operations WHERE id = ? AND operation_type = 'rename'",
            (operation_id,),
        ).fetchone()
        return None if row is None else self._rename_operation_from_row(row)

    def list_rename_operations(self) -> tuple[RenameOperationRecord, ...]:
        """Return every persisted rename operation, newest first, without writing."""

        self._require_open_in_owner_thread()
        rows = self._connection.execute(
            """
            SELECT * FROM operations
            WHERE operation_type = 'rename'
            ORDER BY created_at DESC, id DESC
            """
        ).fetchall()
        return tuple(self._rename_operation_from_row(row) for row in rows)

    def get_rename_operation_item(self, item_id: str) -> RenameOperationItemRecord | None:
        self._require_open_in_owner_thread()
        if not isinstance(item_id, str) or not item_id.strip():
            raise RepositoryDataError("item_id 必须是非空字符串")
        row = self._connection.execute(
            "SELECT * FROM operation_items WHERE id = ?",
            (item_id,),
        ).fetchone()
        return None if row is None else self._rename_item_from_row(row)

    def list_rename_operation_items(
        self,
        operation_id: str,
    ) -> tuple[RenameOperationItemRecord, ...]:
        self._require_open_in_owner_thread()
        if not isinstance(operation_id, str) or not operation_id.strip():
            raise RepositoryDataError("operation_id 必须是非空字符串")
        rows = self._connection.execute(
            """
            SELECT * FROM operation_items
            WHERE operation_id = ?
            ORDER BY created_at, normalized_source_path, id
            """,
            (operation_id,),
        ).fetchall()
        return tuple(self._rename_item_from_row(row) for row in rows)

    def create_rename_operation(
        self,
        *,
        allowed_root: Path,
        items: Iterable[RenamePlanItem],
    ) -> tuple[RenameOperationRecord, tuple[RenameOperationItemRecord, ...]]:
        self._require_open_in_owner_thread()
        canonical_root, normalized_root = _canonicalize_path(
            allowed_root,
            field_name="allowed_root",
        )
        frozen_items = tuple(items)
        if not frozen_items:
            raise RepositoryDataError("重命名计划不能为空")

        prepared: list[tuple[RenamePlanItem, Path, str, Path, str]] = []
        asset_ids: set[str] = set()
        source_keys: set[str] = set()
        target_keys: set[str] = set()
        for item in frozen_items:
            if not isinstance(item, RenamePlanItem):
                raise RepositoryDataError("重命名计划必须使用 RenamePlanItem")
            if not isinstance(item.asset_id, str) or not item.asset_id.strip():
                raise RepositoryDataError("asset_id 必须是非空字符串")
            _require_non_negative_integer(
                item.expected_size_bytes,
                field_name="expected_size_bytes",
                optional=False,
            )
            _require_non_negative_integer(
                item.expected_mtime_ns,
                field_name="expected_mtime_ns",
                optional=True,
            )
            source_path, source_key = _windows_path_key(
                item.source_path,
                field_name="source_path",
            )
            _validate_windows_leaf_name(item.target_path, field_name="target_path")
            target_path, target_key = _windows_path_key(
                item.target_path,
                field_name="target_path",
            )
            if source_path.suffix.casefold() != target_path.suffix.casefold():
                raise RepositoryDataError("重命名不能更改音频文件扩展名")
            _, normalized_source_for_root = _canonicalize_path(
                source_path,
                field_name="source_path",
            )
            _, normalized_target_for_root = _canonicalize_path(
                target_path,
                field_name="target_path",
            )
            _require_path_within_root(
                normalized_source_for_root,
                normalized_root,
                field_name="source_path",
            )
            _require_path_within_root(
                normalized_target_for_root,
                normalized_root,
                field_name="target_path",
            )
            _, source_parent = _canonicalize_path(
                source_path.parent,
                field_name="source_path.parent",
            )
            _, target_parent = _canonicalize_path(
                target_path.parent,
                field_name="target_path.parent",
            )
            if source_parent != target_parent:
                raise RepositoryPathError("重命名目标必须与源文件位于同一目录")
            if source_key == target_key:
                raise RepositoryDataError("重命名目标不能与源文件相同")
            if item.asset_id in asset_ids:
                raise RepositoryDataError(f"重命名计划包含重复 asset_id：{item.asset_id}")
            if source_key in source_keys:
                raise RepositoryDataError(f"重命名计划包含 Windows 等价源路径：{source_path}")
            if target_key in target_keys:
                raise RepositoryDataError(f"重命名计划包含 Windows 等价目标路径：{target_path}")
            asset_ids.add(item.asset_id)
            source_keys.add(source_key)
            target_keys.add(target_key)
            prepared.append((item, source_path, source_key, target_path, target_key))

        operation_id = str(uuid4())
        now = _utc_now()
        summary_json = _strict_json_dumps(
            {
                "allowed_root": os.fspath(canonical_root),
                "item_count": len(prepared),
                "operation_type": "rename",
            },
            field_name="summary_json",
        )
        with self._transaction():
            asset_rows = self._connection.execute(
                "SELECT * FROM assets WHERE kind = 'audio'"
            ).fetchall()
            assets_by_id = {row["id"]: row for row in asset_rows}
            occupied_keys = {
                _windows_path_key(Path(row["canonical_path"]), field_name="assets.canonical_path")[1]: row["id"]
                for row in asset_rows
            }
            for item, source_path, source_key, target_path, target_key in prepared:
                asset = assets_by_id.get(item.asset_id)
                if asset is None:
                    raise RecordNotFoundError(f"音频资产不存在：{item.asset_id}")
                if asset["file_state"] != "active":
                    raise RepositoryDataError(f"只有 active 音频可以创建重命名计划：{item.asset_id}")
                asset_source_key = _windows_path_key(
                    Path(asset["canonical_path"]),
                    field_name="assets.canonical_path",
                )[1]
                if asset_source_key != source_key:
                    raise RepositoryDataError(f"计划源路径与资产索引不一致：{source_path}")
                if asset["size_bytes"] != item.expected_size_bytes or asset["mtime_ns"] != item.expected_mtime_ns:
                    raise RepositoryDataError(f"计划指纹与资产索引不一致：{source_path}")
                occupied_asset_id = occupied_keys.get(target_key)
                if occupied_asset_id is not None and occupied_asset_id != item.asset_id:
                    raise RepositoryDataError(f"重命名目标与现有资产冲突：{target_path}")

            self._connection.execute(
                """
                INSERT INTO operations(
                    id, operation_type, status, success_count, failure_count,
                    summary_json, created_at, started_at, completed_at
                ) VALUES (?, 'rename', 'planned', 0, 0, ?, ?, NULL, NULL)
                """,
                (
                    operation_id,
                    summary_json,
                    now,
                ),
            )
            for item, source_path, source_key, target_path, target_key in prepared:
                asset = assets_by_id[item.asset_id]
                before_json = _strict_json_dumps(
                    {
                        "asset_id": asset["id"],
                        "canonical_path": asset["canonical_path"],
                        "file_name": asset["file_name"],
                        "file_state": asset["file_state"],
                        "mtime_ns": asset["mtime_ns"],
                        "normalized_path": asset["normalized_path"],
                        "size_bytes": asset["size_bytes"],
                    },
                    field_name="before_json",
                )
                self._connection.execute(
                    """
                    INSERT INTO operation_items(
                        id, operation_id, asset_id, source_path, normalized_source_path,
                        target_path, normalized_target_path,
                        expected_size_bytes, expected_mtime_ns, result,
                        error_code, error_message, before_json, after_json,
                        created_at, completed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'planned',
                              NULL, NULL, ?, NULL, ?, NULL)
                    """,
                    (
                        str(uuid4()),
                        operation_id,
                        item.asset_id,
                        os.fspath(source_path),
                        source_key,
                        os.fspath(target_path),
                        target_key,
                        item.expected_size_bytes,
                        item.expected_mtime_ns,
                        before_json,
                        now,
                    ),
                )

        operation = self.get_rename_operation(operation_id)
        if operation is None:
            raise RepositoryDataError("重命名操作创建后无法读取")
        return operation, self.list_rename_operation_items(operation_id)

    def start_rename_operation(self, operation_id: str) -> RenameOperationRecord:
        self._require_open_in_owner_thread()
        now = _utc_now()
        with self._transaction():
            cursor = self._connection.execute(
                """
                UPDATE operations SET status = 'running', started_at = ?
                WHERE id = ? AND operation_type = 'rename' AND status = 'planned'
                """,
                (now, operation_id),
            )
            if cursor.rowcount != 1:
                raise RepositoryDataError("重命名操作不存在或不能启动")
        record = self.get_rename_operation(operation_id)
        if record is None:
            raise RecordNotFoundError(f"重命名操作不存在：{operation_id}")
        return record

    def start_rename_item(
        self,
        operation_id: str,
        item_id: str,
    ) -> RenameOperationItemRecord:
        self._require_open_in_owner_thread()
        with self._transaction():
            operation = self._connection.execute(
                "SELECT status FROM operations WHERE id = ? AND operation_type = 'rename'",
                (operation_id,),
            ).fetchone()
            if operation is None:
                raise RecordNotFoundError(f"重命名操作不存在：{operation_id}")
            if operation["status"] != "running":
                raise RepositoryDataError("只有 running 重命名操作可以启动文件项")
            cursor = self._connection.execute(
                """
                UPDATE operation_items SET result = 'running'
                WHERE id = ? AND operation_id = ? AND result = 'planned'
                """,
                (item_id, operation_id),
            )
            if cursor.rowcount != 1:
                raise RepositoryDataError("重命名文件项不存在或不能启动")
        record = self.get_rename_operation_item(item_id)
        if record is None:
            raise RecordNotFoundError(f"重命名文件项不存在：{item_id}")
        return record

    def commit_rename_item(
        self,
        operation_id: str,
        item_id: str,
        *,
        actual_size_bytes: int | None = None,
        actual_mtime_ns: int | None = None,
        metadata_audit: object | None = None,
    ) -> tuple[AssetRecord, RenameOperationItemRecord]:
        self._require_open_in_owner_thread()
        if (actual_size_bytes is None) != (actual_mtime_ns is None):
            raise RepositoryDataError("实际 size 与 mtime 必须同时提供或同时省略")
        if actual_size_bytes is not None:
            _require_non_negative_integer(
                actual_size_bytes,
                field_name="actual_size_bytes",
                optional=False,
            )
            _require_non_negative_integer(
                actual_mtime_ns,
                field_name="actual_mtime_ns",
                optional=False,
            )
        if metadata_audit is not None:
            # Validate before opening a transaction and normalize to a detached
            # standard-JSON value for the immutable operation snapshot.
            metadata_audit = _strict_json_loads(
                _strict_json_dumps(metadata_audit, field_name="metadata_audit")
            )
        now = _utc_now()
        with self._transaction(operation_id=operation_id, item_id=item_id):
            operation = self._connection.execute(
                "SELECT status FROM operations WHERE id = ? AND operation_type = 'rename'",
                (operation_id,),
            ).fetchone()
            if operation is None:
                raise RecordNotFoundError(f"重命名操作不存在：{operation_id}")
            if operation["status"] != "running":
                raise RepositoryDataError("只有 running 重命名操作可以提交文件项")
            item = self._connection.execute(
                "SELECT * FROM operation_items WHERE id = ? AND operation_id = ?",
                (item_id, operation_id),
            ).fetchone()
            if item is None:
                raise RecordNotFoundError(f"重命名文件项不存在：{item_id}")
            if item["result"] != "running":
                raise RepositoryDataError("只有 running 文件项可以提交成功")
            asset = self._connection.execute(
                "SELECT * FROM assets WHERE id = ?",
                (item["asset_id"],),
            ).fetchone()
            if asset is None:
                raise RecordNotFoundError(f"音频资产不存在：{item['asset_id']}")
            if asset["kind"] != "audio" or asset["file_state"] != "active":
                raise RepositoryDataError("提交重命名时资产必须仍为 active audio")
            asset_source_key = _windows_path_key(
                Path(asset["canonical_path"]),
                field_name="assets.canonical_path",
            )[1]
            if asset_source_key != item["normalized_source_path"]:
                raise RepositoryDataError("提交重命名时资产源路径已经变化")
            if asset["size_bytes"] != item["expected_size_bytes"] or asset["mtime_ns"] != item["expected_mtime_ns"]:
                raise RepositoryDataError("提交重命名时资产指纹已经变化")

            target_path, target_normalized = _canonicalize_path(
                Path(item["target_path"]),
                field_name="target_path",
            )
            conflict = self._connection.execute(
                "SELECT id FROM assets WHERE normalized_path = ? AND id != ?",
                (target_normalized, asset["id"]),
            ).fetchone()
            if conflict is not None:
                raise RepositoryDataError("提交重命名时目标索引已经被占用")
            committed_size = asset["size_bytes"] if actual_size_bytes is None else actual_size_bytes
            committed_mtime = asset["mtime_ns"] if actual_size_bytes is None else actual_mtime_ns
            after_value = {
                    "asset_id": asset["id"],
                    "canonical_path": os.fspath(target_path),
                    "file_name": target_path.name,
                    "file_state": "active",
                    "mtime_ns": committed_mtime,
                    "normalized_path": target_normalized,
                    "size_bytes": committed_size,
                }
            if metadata_audit is not None:
                after_value["metadata_sync"] = metadata_audit
            after_json = _strict_json_dumps(
                after_value,
                field_name="after_json",
            )
            asset_cursor = self._connection.execute(
                """
                UPDATE assets
                SET canonical_path = ?, normalized_path = ?, file_name = ?,
                    extension = ?, size_bytes = ?, mtime_ns = ?,
                    file_state = 'active', updated_at = ?
                WHERE id = ? AND normalized_path = ?
                """,
                (
                    os.fspath(target_path),
                    target_normalized,
                    target_path.name,
                    target_path.suffix.casefold(),
                    committed_size,
                    committed_mtime,
                    now,
                    asset["id"],
                    asset["normalized_path"],
                ),
            )
            if asset_cursor.rowcount != 1:
                raise RepositoryDataError("重命名资产更新失败")
            item_cursor = self._connection.execute(
                """
                UPDATE operation_items
                SET result = 'success', error_code = NULL,
                    error_message = NULL, after_json = ?, completed_at = ?
                WHERE id = ? AND operation_id = ? AND result = 'running'
                """,
                (after_json, now, item_id, operation_id),
            )
            if item_cursor.rowcount != 1:
                raise RepositoryDataError("重命名文件项成功状态更新失败")
            self._connection.execute(
                "UPDATE operations SET success_count = success_count + 1 WHERE id = ?",
                (operation_id,),
            )

        asset_record = self.get_asset_by_id(item["asset_id"])
        item_record = self.get_rename_operation_item(item_id)
        if asset_record is None or item_record is None:
            raise RepositoryDataError("重命名提交后无法读取结果")
        return asset_record, item_record

    def record_rename_item_outcome(
        self,
        operation_id: str,
        item_id: str,
        *,
        result: str,
        actual_path: Path | None,
        error_code: str | None,
        error_message: str | None,
    ) -> RenameOperationItemRecord:
        self._require_open_in_owner_thread()
        _require_choice(result, RENAME_ITEM_OUTCOMES, field_name="result")
        if actual_path is not None:
            actual_path, _ = _canonicalize_path(actual_path, field_name="actual_path")
        if error_code is not None and not isinstance(error_code, str):
            raise RepositoryDataError("error_code 必须是字符串或 None")
        if error_message is not None and not isinstance(error_message, str):
            raise RepositoryDataError("error_message 必须是字符串或 None")
        now = _utc_now()
        with self._transaction():
            operation = self._connection.execute(
                "SELECT status FROM operations WHERE id = ? AND operation_type = 'rename'",
                (operation_id,),
            ).fetchone()
            if operation is None:
                raise RecordNotFoundError(f"重命名操作不存在：{operation_id}")
            if operation["status"] != "running":
                raise RepositoryDataError("只有 running 重命名操作可以记录文件结果")
            item = self._connection.execute(
                "SELECT result FROM operation_items WHERE id = ? AND operation_id = ?",
                (item_id, operation_id),
            ).fetchone()
            if item is None:
                raise RecordNotFoundError(f"重命名文件项不存在：{item_id}")
            allowed_previous = {"planned"} if result == "cancelled" else {"running"}
            if item["result"] not in allowed_previous:
                raise RepositoryDataError("重命名文件项当前状态不能记录该结果")
            after_json = _strict_json_dumps(
                {
                    "actual_path": None if actual_path is None else os.fspath(actual_path),
                    "error_code": error_code,
                    "error_message": error_message,
                    "result": result,
                },
                field_name="after_json",
            )
            cursor = self._connection.execute(
                """
                UPDATE operation_items
                SET result = ?, error_code = ?, error_message = ?,
                    after_json = ?, completed_at = ?
                WHERE id = ? AND operation_id = ? AND result = ?
                """,
                (
                    result,
                    error_code,
                    error_message,
                    after_json,
                    now,
                    item_id,
                    operation_id,
                    item["result"],
                ),
            )
            if cursor.rowcount != 1:
                raise RepositoryDataError("重命名文件结果更新失败")
            if result in RENAME_FAILURE_RESULTS:
                self._connection.execute(
                    "UPDATE operations SET failure_count = failure_count + 1 WHERE id = ?",
                    (operation_id,),
                )
        record = self.get_rename_operation_item(item_id)
        if record is None:
            raise RecordNotFoundError(f"重命名文件项不存在：{item_id}")
        return record

    def finish_rename_operation(self, operation_id: str) -> RenameOperationRecord:
        self._require_open_in_owner_thread()
        now = _utc_now()
        with self._transaction():
            operation = self._connection.execute(
                "SELECT status FROM operations WHERE id = ? AND operation_type = 'rename'",
                (operation_id,),
            ).fetchone()
            if operation is None:
                raise RecordNotFoundError(f"重命名操作不存在：{operation_id}")
            if operation["status"] != "running":
                raise RepositoryDataError("只有 running 重命名操作可以终结")
            rows = self._connection.execute(
                """
                SELECT result, COUNT(*) AS count FROM operation_items
                WHERE operation_id = ? GROUP BY result
                """,
                (operation_id,),
            ).fetchall()
            counts = {row["result"]: int(row["count"]) for row in rows}
            if counts.get("planned", 0) or counts.get("running", 0):
                raise RepositoryDataError("存在 planned/running 文件项，不能终结重命名操作")
            success_count = counts.get("success", 0)
            failure_count = sum(counts.get(state, 0) for state in RENAME_FAILURE_RESULTS)
            cancelled_count = counts.get("cancelled", 0)
            if failure_count and success_count:
                final_status = "partial"
            elif failure_count:
                final_status = "failed"
            elif cancelled_count and success_count:
                final_status = "partial"
            elif cancelled_count:
                final_status = "cancelled"
            else:
                final_status = "success"
            cursor = self._connection.execute(
                """
                UPDATE operations
                SET status = ?, success_count = ?, failure_count = ?, completed_at = ?
                WHERE id = ? AND status = 'running'
                """,
                (final_status, success_count, failure_count, now, operation_id),
            )
            if cursor.rowcount != 1:
                raise RepositoryDataError("重命名操作终结失败")
        record = self.get_rename_operation(operation_id)
        if record is None:
            raise RecordNotFoundError(f"重命名操作不存在：{operation_id}")
        return record

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

    def _latest_completed_roots(
        self,
        asset_ids: Iterable[str],
        *,
        kind: str,
        mode: str,
    ) -> dict[str, Path]:
        """Return each asset's latest trustworthy completed scan root.

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
            if row["kind"] != kind:
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
            WHERE ss.mode = ?
              AND ss.status = 'completed'
              AND si.status = 'indexed'
            ORDER BY ss.completed_at DESC, ss.started_at DESC, ss.id DESC
            """,
            (mode,),
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

    def latest_completed_audio_roots(
        self,
        asset_ids: Iterable[str],
    ) -> dict[str, Path]:
        return self._latest_completed_roots(asset_ids, kind="audio", mode="audio")

    def latest_completed_lyric_roots(
        self,
        asset_ids: Iterable[str],
    ) -> dict[str, Path]:
        return self._latest_completed_roots(asset_ids, kind="lyric", mode="lyric")

    @staticmethod
    def _lyrics_match_from_row(row: sqlite3.Row) -> LyricsMatchRecord:
        return LyricsMatchRecord(
            id=row["id"],
            audio_asset_id=row["audio_asset_id"],
            lyric_asset_id=row["lyric_asset_id"],
            source_kind=row["source_kind"],
            confidence=int(row["confidence"]),
            method=row["method"],
            state=row["state"],
            is_current=bool(row["is_current"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def list_lyrics_matches(
        self,
        *,
        audio_asset_id: str | None = None,
        current_only: bool = False,
    ) -> tuple[LyricsMatchRecord, ...]:
        self._require_open_in_owner_thread()
        if audio_asset_id is not None and (
            not isinstance(audio_asset_id, str) or not audio_asset_id.strip()
        ):
            raise RepositoryDataError("audio_asset_id 必须是非空字符串或 None")
        clauses: list[str] = []
        parameters: list[object] = []
        if audio_asset_id is not None:
            clauses.append("audio_asset_id = ?")
            parameters.append(audio_asset_id)
        if current_only:
            clauses.append("is_current = 1")
        where = "" if not clauses else " WHERE " + " AND ".join(clauses)
        rows = self._connection.execute(
            "SELECT * FROM lyrics_matches"
            + where
            + " ORDER BY created_at ASC, id ASC",
            tuple(parameters),
        ).fetchall()
        return tuple(self._lyrics_match_from_row(row) for row in rows)

    def current_external_lyrics_for_audio_ids(
        self,
        audio_asset_ids: Iterable[str],
    ) -> tuple[LyricsMatchRecord, ...]:
        """Return authoritative current external relations for selected audio."""

        self._require_open_in_owner_thread()
        requested: list[str] = []
        seen: set[str] = set()
        for asset_id in audio_asset_ids:
            if not isinstance(asset_id, str) or not asset_id.strip():
                raise RepositoryDataError("audio_asset_id 必须是非空字符串")
            if asset_id not in seen:
                seen.add(asset_id)
                requested.append(asset_id)
        if not requested:
            return ()
        placeholders = ", ".join("?" for _ in requested)
        rows = self._connection.execute(
            f"""
            SELECT * FROM lyrics_matches
            WHERE is_current = 1
              AND state = 'matched'
              AND source_kind = 'external'
              AND audio_asset_id IN ({placeholders})
            ORDER BY audio_asset_id, created_at, id
            """,
            tuple(requested),
        ).fetchall()
        return tuple(self._lyrics_match_from_row(row) for row in rows)

    def current_audio_ids_for_external_lyric(self, lyric_asset_id: str) -> tuple[str, ...]:
        self._require_open_in_owner_thread()
        if not isinstance(lyric_asset_id, str) or not lyric_asset_id.strip():
            raise RepositoryDataError("lyric_asset_id 必须是非空字符串")
        rows = self._connection.execute(
            """
            SELECT audio_asset_id FROM lyrics_matches
            WHERE lyric_asset_id = ? AND source_kind = 'external'
              AND state = 'matched' AND is_current = 1
            ORDER BY audio_asset_id
            """,
            (lyric_asset_id,),
        ).fetchall()
        return tuple(row["audio_asset_id"] for row in rows)

    def reinstate_lyrics_match(self, match_id: str) -> LyricsMatchRecord:
        """Restore one cancelled historical relation after both files recover."""

        self._require_open_in_owner_thread()
        if not isinstance(match_id, str) or not match_id.strip():
            raise RepositoryDataError("match_id 不能为空")
        with self._transaction():
            self._reinstate_external_lyrics_match(match_id)
        restored = self._connection.execute(
            "SELECT * FROM lyrics_matches WHERE id = ?",
            (match_id,),
        ).fetchone()
        return self._lyrics_match_from_row(restored)

    def _reinstate_external_lyrics_match(self, match_id: str) -> sqlite3.Row:
        row = self._connection.execute(
            "SELECT * FROM lyrics_matches WHERE id = ?",
            (match_id,),
        ).fetchone()
        if row is None:
            raise RecordNotFoundError(f"歌词匹配历史不存在：{match_id}")
        if row["source_kind"] != "external" or row["lyric_asset_id"] is None:
            raise RepositoryDataError("只允许恢复外部歌词关系")
        if row["is_current"] or row["state"] != "cancelled":
            raise RepositoryDataError("歌词关系不是可恢复的已取消状态")
        audio = self._connection.execute(
            "SELECT kind, file_state FROM assets WHERE id = ?",
            (row["audio_asset_id"],),
        ).fetchone()
        lyric = self._connection.execute(
            "SELECT kind, file_state FROM assets WHERE id = ?",
            (row["lyric_asset_id"],),
        ).fetchone()
        if (
            audio is None or audio["kind"] != "audio" or audio["file_state"] != "active"
            or lyric is None or lyric["kind"] != "lyric" or lyric["file_state"] != "active"
        ):
            raise RepositoryDataError("音频或歌词资产不是 active，不能恢复关系")
        conflict = self._connection.execute(
            """
            SELECT 1 FROM lyrics_matches
            WHERE is_current = 1 AND state = 'matched'
              AND id != ? AND (audio_asset_id = ? OR lyric_asset_id = ?)
            """,
            (match_id, row["audio_asset_id"], row["lyric_asset_id"]),
        ).fetchone()
        if conflict is not None:
            raise RepositoryDataError("音频或歌词已有当前匹配，不能恢复旧关系")
        self._connection.execute(
            """
            UPDATE lyrics_matches
            SET state = 'matched', is_current = 1, updated_at = ?
            WHERE id = ? AND state = 'cancelled' AND is_current = 0
            """,
            (_utc_now(), match_id),
        )
        return row

    @staticmethod
    def _encode_setting_value(value: object) -> str:
        try:
            return json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        except (TypeError, ValueError) as exc:
            raise RepositoryDataError(
                "设置值必须兼容标准 JSON，且不能包含 NaN 或 Infinity"
            ) from exc

    def cancel_external_lyrics_match_with_journal(
        self,
        *,
        match_id: str,
        audio_asset_id: str,
        lyric_asset_id: str,
        journal_key: str,
        journal: dict[str, object],
    ) -> LyricsMatchRecord:
        """Atomically persist an exact relation snapshot and cancel that relation."""

        self._require_open_in_owner_thread()
        if any(
            not isinstance(value, str) or not value.strip()
            for value in (match_id, audio_asset_id, lyric_asset_id, journal_key)
        ) or not isinstance(journal, dict):
            raise RepositoryDataError("歌词清理日志参数无效")
        now = _utc_now()
        with self._transaction():
            row = self._connection.execute(
                """
                SELECT * FROM lyrics_matches
                WHERE id = ? AND audio_asset_id = ? AND lyric_asset_id = ?
                  AND source_kind = 'external' AND state = 'matched' AND is_current = 1
                """,
                (match_id, audio_asset_id, lyric_asset_id),
            ).fetchone()
            if row is None:
                raise RepositoryDataError("当前外部歌词关系与清理快照不一致")
            persisted = dict(journal)
            persisted["relation"] = {
                "id": row["id"],
                "audio_asset_id": row["audio_asset_id"],
                "lyric_asset_id": row["lyric_asset_id"],
                "source_kind": row["source_kind"],
                "confidence": int(row["confidence"]),
                "method": row["method"],
                "state": row["state"],
                "is_current": bool(row["is_current"]),
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }
            encoded = self._encode_setting_value(persisted)
            self._connection.execute(
                """
                INSERT INTO settings(key, value_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value_json = excluded.value_json,
                    updated_at = excluded.updated_at
                """,
                (journal_key, encoded, now),
            )
            cursor = self._connection.execute(
                """
                UPDATE lyrics_matches
                SET state = 'cancelled', is_current = 0, updated_at = ?
                WHERE id = ? AND audio_asset_id = ? AND lyric_asset_id = ?
                  AND source_kind = 'external' AND state = 'matched' AND is_current = 1
                """,
                (now, match_id, audio_asset_id, lyric_asset_id),
            )
            if cursor.rowcount != 1:
                raise RepositoryDataError("当前外部歌词关系取消失败")
        result = self._connection.execute(
            "SELECT * FROM lyrics_matches WHERE id = ?",
            (match_id,),
        ).fetchone()
        return self._lyrics_match_from_row(result)

    def finalize_external_lyrics_cleanup(
        self,
        *,
        match_id: str,
        journal_key: str,
        restore_relation: bool,
    ) -> LyricsMatchRecord:
        """Atomically keep/restore the exact relation and clear its recovery journal."""

        self._require_open_in_owner_thread()
        if (
            not isinstance(match_id, str)
            or not match_id.strip()
            or not isinstance(journal_key, str)
            or not journal_key.strip()
            or not isinstance(restore_relation, bool)
        ):
            raise RepositoryDataError("歌词清理收尾参数无效")
        with self._transaction():
            row = self._connection.execute(
                "SELECT * FROM lyrics_matches WHERE id = ?",
                (match_id,),
            ).fetchone()
            if row is None or row["source_kind"] != "external":
                raise RepositoryDataError("歌词清理关系快照不存在或类型错误")
            if restore_relation:
                if row["state"] == "cancelled" and not row["is_current"]:
                    self._reinstate_external_lyrics_match(match_id)
                elif row["state"] != "matched" or not row["is_current"]:
                    raise RepositoryDataError("歌词关系不是可恢复或已恢复状态")
            elif row["state"] != "cancelled" or row["is_current"]:
                raise RepositoryDataError("永久删除后歌词关系必须保持已取消")
            encoded = self._encode_setting_value([])
            self._connection.execute(
                """
                INSERT INTO settings(key, value_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value_json = excluded.value_json,
                    updated_at = excluded.updated_at
                """,
                (journal_key, encoded, _utc_now()),
            )
        result = self._connection.execute(
            "SELECT * FROM lyrics_matches WHERE id = ?",
            (match_id,),
        ).fetchone()
        return self._lyrics_match_from_row(result)

    def commit_lyrics_match(
        self,
        *,
        audio_asset_id: str,
        lyric_asset_id: str | None,
        source_kind: str,
        confidence: int,
        method: str,
    ) -> LyricsMatchRecord:
        """Replace one current match while retaining the previous row as history."""

        self._require_open_in_owner_thread()
        if not isinstance(audio_asset_id, str) or not audio_asset_id.strip():
            raise RepositoryDataError("audio_asset_id 不能为空")
        _require_choice(source_kind, LYRICS_MATCH_SOURCES, field_name="source_kind")
        _require_choice(method, LYRICS_MATCH_METHODS, field_name="method")
        if isinstance(confidence, bool) or not isinstance(confidence, int) or not 0 <= confidence <= 100:
            raise RepositoryDataError("confidence 必须是 0～100 的整数")
        if method == "automatic" and confidence < 95:
            raise RepositoryDataError("低置信度歌词禁止自动提交")
        if source_kind == "embedded":
            if lyric_asset_id is not None:
                raise RepositoryDataError("内嵌歌词不能绑定外部歌词资产")
            if confidence != 100:
                raise RepositoryDataError("内嵌歌词提交置信度必须为 100")
        elif not isinstance(lyric_asset_id, str) or not lyric_asset_id.strip():
            raise RepositoryDataError("外部歌词必须提供 lyric_asset_id")

        now = _utc_now()
        match_id = str(uuid4())
        with self._transaction():
            audio = self._connection.execute(
                "SELECT kind, file_state FROM assets WHERE id = ?",
                (audio_asset_id,),
            ).fetchone()
            if audio is None:
                raise RecordNotFoundError(f"音频资产不存在：{audio_asset_id}")
            if audio["kind"] != "audio" or audio["file_state"] != "active":
                raise RepositoryDataError("歌词匹配只允许 active audio")
            current = self._connection.execute(
                "SELECT source_kind FROM lyrics_matches WHERE audio_asset_id = ? AND is_current = 1",
                (audio_asset_id,),
            ).fetchone()
            if current is not None and current["source_kind"] == "embedded" and source_kind == "external":
                raise RepositoryDataError("已有内嵌歌词时禁止外部歌词覆盖")
            if source_kind == "external":
                lyric = self._connection.execute(
                    "SELECT kind, file_state FROM assets WHERE id = ?",
                    (lyric_asset_id,),
                ).fetchone()
                if lyric is None:
                    raise RecordNotFoundError(f"歌词资产不存在：{lyric_asset_id}")
                if lyric["kind"] != "lyric" or lyric["file_state"] != "active":
                    raise RepositoryDataError("外部歌词匹配只允许 active lyric")
                claimed = self._connection.execute(
                    """
                    SELECT audio_asset_id FROM lyrics_matches
                    WHERE lyric_asset_id = ? AND is_current = 1 AND audio_asset_id != ?
                    """,
                    (lyric_asset_id, audio_asset_id),
                ).fetchone()
                if claimed is not None:
                    raise RepositoryDataError("该外部 LRC 已被另一首音频占用")
            self._connection.execute(
                """
                UPDATE lyrics_matches SET is_current = 0, updated_at = ?
                WHERE audio_asset_id = ? AND is_current = 1
                """,
                (now, audio_asset_id),
            )
            self._connection.execute(
                """
                INSERT INTO lyrics_matches(
                    id, audio_asset_id, lyric_asset_id, source_kind,
                    confidence, method, state, is_current, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'matched', 1, ?, ?)
                """,
                (
                    match_id,
                    audio_asset_id,
                    lyric_asset_id,
                    source_kind,
                    confidence,
                    method,
                    now,
                    now,
                ),
            )
        rows = self._connection.execute(
            "SELECT * FROM lyrics_matches WHERE id = ?",
            (match_id,),
        ).fetchone()
        if rows is None:
            raise RepositoryDataError("歌词匹配提交后无法读取结果")
        return self._lyrics_match_from_row(rows)

    def cancel_current_lyrics_match(self, audio_asset_id: str) -> LyricsMatchRecord:
        self._require_open_in_owner_thread()
        if not isinstance(audio_asset_id, str) or not audio_asset_id.strip():
            raise RepositoryDataError("audio_asset_id 不能为空")
        now = _utc_now()
        with self._transaction():
            row = self._connection.execute(
                "SELECT id FROM lyrics_matches WHERE audio_asset_id = ? AND is_current = 1",
                (audio_asset_id,),
            ).fetchone()
            if row is None:
                raise RecordNotFoundError("当前歌词匹配不存在")
            self._connection.execute(
                """
                UPDATE lyrics_matches
                SET state = 'cancelled', is_current = 0, updated_at = ?
                WHERE id = ? AND is_current = 1
                """,
                (now, row["id"]),
            )
        result = self._connection.execute(
            "SELECT * FROM lyrics_matches WHERE id = ?",
            (row["id"],),
        ).fetchone()
        if result is None:
            raise RepositoryDataError("取消歌词匹配后无法读取结果")
        return self._lyrics_match_from_row(result)

    def set_setting(self, key: str, value: object) -> SettingRecord:
        self._require_open_in_owner_thread()
        if not isinstance(key, str) or not key.strip():
            raise RepositoryDataError("设置 key 必须是非空字符串")
        value_json = self._encode_setting_value(value)

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

    def finalize_import_journal(
        self,
        *,
        pending_key: str,
        history_key: str,
        batch_id: str,
        history_entry: dict[str, object],
    ) -> None:
        """Append one exact import history entry and clear its pending journal atomically."""
        self._require_open_in_owner_thread()
        if any(not isinstance(value, str) or not value.strip() for value in (
            pending_key, history_key, batch_id
        )):
            raise RepositoryDataError("导入日志 key 和批次编号必须是非空字符串")
        if not isinstance(history_entry, dict) or history_entry.get("id") != batch_id:
            raise RepositoryDataError("导入历史批次与待恢复日志不一致")
        encoded_entry = self._encode_setting_value(history_entry)
        exact_entry = _strict_json_loads(encoded_entry)
        now = _utc_now()
        with self._transaction(operation_id=batch_id, item_id=pending_key):
            pending_row = self._connection.execute(
                "SELECT value_json FROM settings WHERE key = ?", (pending_key,)
            ).fetchone()
            if pending_row is not None:
                pending = _strict_json_loads(pending_row["value_json"])
                if not isinstance(pending, dict) or pending.get("batch_id") != batch_id:
                    raise RepositoryDataError("待恢复日志批次与完成历史不一致")
            history_row = self._connection.execute(
                "SELECT value_json FROM settings WHERE key = ?", (history_key,)
            ).fetchone()
            history = [] if history_row is None else _strict_json_loads(history_row["value_json"])
            if not isinstance(history, list) or not all(isinstance(item, dict) for item in history):
                raise RepositoryDataError("导入历史格式损坏")
            matches = [item for item in history if item.get("id") == batch_id]
            encoded_matches = [self._encode_setting_value(item) for item in matches]
            if len(matches) > 1 or (encoded_matches and encoded_matches[0] != encoded_entry):
                raise RepositoryDataError("已存在同批次但内容不同的导入历史")
            if not matches:
                if pending_row is None:
                    raise RepositoryDataError("待恢复日志不存在，不能盲目补写历史")
                history = (history + [exact_entry])[-200:]
                encoded_history = self._encode_setting_value(history)
                self._connection.execute(
                    """
                    INSERT INTO settings(key, value_json, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(key) DO UPDATE SET
                        value_json = excluded.value_json,
                        updated_at = excluded.updated_at
                    """,
                    (history_key, encoded_history, now),
                )
            self._connection.execute("DELETE FROM settings WHERE key = ?", (pending_key,))

    def create_import_journal(
        self,
        *,
        pending_key: str,
        batch_id: str,
        journal: dict[str, object],
    ) -> None:
        """Persist a complete new import plan without overwriting unresolved work."""
        self._require_open_in_owner_thread()
        if (
            not isinstance(pending_key, str)
            or not pending_key.strip()
            or not isinstance(batch_id, str)
            or not batch_id.strip()
            or not isinstance(journal, dict)
            or journal.get("batch_id") != batch_id
        ):
            raise RepositoryDataError("待恢复导入日志参数损坏")
        encoded = self._encode_setting_value(journal)
        try:
            with self._transaction():
                self._connection.execute(
                    "INSERT INTO settings(key, value_json, updated_at) VALUES (?, ?, ?)",
                    (pending_key, encoded, _utc_now()),
                )
        except sqlite3.IntegrityError as error:
            raise RepositoryDataError("存在尚未处理的安全导入恢复日志") from error

    def read_import_finalize_state(
        self,
        *,
        pending_key: str,
        history_key: str,
        batch_id: str,
    ) -> tuple[object | None, dict[str, object] | None]:
        """Read back an uncertain import finalization without changing the database."""
        self._require_open_in_owner_thread()
        if any(not isinstance(value, str) or not value.strip() for value in (
            pending_key, history_key, batch_id
        )):
            raise RepositoryDataError("导入日志 key 和批次编号必须是非空字符串")
        pending = self.get_setting(pending_key)
        history = self.get_setting(history_key)
        value = [] if history is None else history.value
        if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
            raise RepositoryDataError("导入历史格式损坏")
        matches = [dict(item) for item in value if item.get("id") == batch_id]
        if len(matches) > 1:
            raise RepositoryDataError("导入历史包含重复批次")
        return (None if pending is None else pending.value, matches[0] if matches else None)

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
