"""P7 verified backup, restore and retention for user-confirmed deletions."""

from __future__ import annotations

from dataclasses import dataclass, asdict, replace
from datetime import datetime, timedelta, timezone
import hashlib
import os
from pathlib import Path
import stat
import threading
from uuid import uuid4

from PySide6.QtCore import QObject, QThread, Signal

from repositories import LibraryRepository
from services.file_safety import (
    _is_reparse,
    _locked_directory_chain,
    _path_key,
    _validate_directory_chain,
    _within_root,
)
from services.safe_import import import_one


BACKUP_MANIFEST_KEY = "p7.backup_entries"
BACKUP_RETENTION_KEY = "p7.retention_days"
BACKUP_HISTORY_KEY = "p7.operation_history"
_BACKUP_HISTORY_LIMIT = 200
_BACKUP_ACTIONS = {"backup", "restore", "cleanup"}
_BACKUP_HISTORY_STATUSES = {"completed", "cancelled", "failed"}
_BACKUP_ITEM_RESULTS = {"success", "failed", "cancelled"}


class BackupError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class BackupInput:
    asset_id: str
    source_path: Path
    allowed_root: Path
    kind: str


@dataclass(frozen=True, slots=True)
class BackupEntry:
    id: str
    asset_id: str
    kind: str
    original_path: Path
    backup_path: Path
    sha256: str
    created_at: str
    restored_at: str | None = None
    allowed_root: Path | None = None


@dataclass(frozen=True, slots=True)
class BackupRunResult:
    action: str
    success_count: int
    failure_count: int
    messages: tuple[str, ...]
    affected_roots: tuple[tuple[str, Path], ...] = ()
    status: str = "completed"
    history_id: str | None = None
    items: tuple[BackupHistoryItem, ...] = ()


@dataclass(frozen=True, slots=True)
class BackupHistoryItem:
    entry_id: str | None
    asset_id: str
    kind: str
    source_path: Path
    backup_path: Path | None
    restore_target: Path | None
    result: str
    message: str
    completed_at: str


@dataclass(frozen=True, slots=True)
class BackupHistoryRecord:
    id: str
    action: str
    status: str
    created_at: str
    success_count: int
    failure_count: int
    items: tuple[BackupHistoryItem, ...]

    @property
    def restore_ids(self) -> tuple[str, ...]:
        """Return historical candidates; current manifest still decides eligibility."""

        if self.action != "backup":
            return ()
        return tuple(
            item.entry_id
            for item in self.items
            if item.result == "success" and item.entry_id is not None
        )


@dataclass(frozen=True, slots=True)
class BackupCleanupPreview:
    backup_root: Path
    retention_days: int | None
    eligible_count: int


def _aware_datetime(value: object, *, label: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise BackupError(f"{label}损坏")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise BackupError(f"{label}损坏") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise BackupError(f"{label}缺少时区")
    return parsed


def _history_item_to_json(item: BackupHistoryItem) -> dict[str, object]:
    return {
        "entry_id": item.entry_id,
        "asset_id": item.asset_id,
        "kind": item.kind,
        "source_path": str(item.source_path),
        "backup_path": None if item.backup_path is None else str(item.backup_path),
        "restore_target": None if item.restore_target is None else str(item.restore_target),
        "result": item.result,
        "message": item.message,
        "completed_at": item.completed_at,
    }


def _history_item_from_json(value: object) -> BackupHistoryItem:
    if not isinstance(value, dict):
        raise BackupError("备份操作历史明细格式损坏")
    entry_id = value.get("entry_id")
    asset_id = value.get("asset_id")
    kind = value.get("kind")
    source_value = value.get("source_path")
    backup_value = value.get("backup_path")
    restore_value = value.get("restore_target")
    result = value.get("result")
    message = value.get("message")
    completed_at = value.get("completed_at")
    if (
        (
            entry_id is not None
            and (
                not isinstance(entry_id, str)
                or not entry_id
                or Path(entry_id).name != entry_id
                or entry_id in {".", ".."}
            )
        )
        or not isinstance(asset_id, str)
        or not asset_id
        or kind not in {"audio", "lyric"}
        or not isinstance(source_value, str)
        or not Path(source_value).is_absolute()
        or (backup_value is not None and (not isinstance(backup_value, str) or not Path(backup_value).is_absolute()))
        or (restore_value is not None and (not isinstance(restore_value, str) or not Path(restore_value).is_absolute()))
        or result not in _BACKUP_ITEM_RESULTS
        or not isinstance(message, str)
    ):
        raise BackupError("备份操作历史明细字段损坏")
    _aware_datetime(completed_at, label="备份操作历史明细时间")
    return BackupHistoryItem(
        entry_id,
        asset_id,
        str(kind),
        Path(source_value),
        None if backup_value is None else Path(backup_value),
        None if restore_value is None else Path(restore_value),
        str(result),
        message,
        str(completed_at),
    )


def _history_to_json(record: BackupHistoryRecord) -> dict[str, object]:
    return {
        "id": record.id,
        "action": record.action,
        "status": record.status,
        "created_at": record.created_at,
        "success_count": record.success_count,
        "failure_count": record.failure_count,
        "items": [_history_item_to_json(item) for item in record.items],
    }


def _history_from_json(value: object) -> BackupHistoryRecord:
    if not isinstance(value, dict):
        raise BackupError("备份操作历史格式损坏")
    record_id = value.get("id")
    action = value.get("action")
    status = value.get("status")
    created_at = value.get("created_at")
    success_count = value.get("success_count")
    failure_count = value.get("failure_count")
    raw_items = value.get("items")
    if (
        not isinstance(record_id, str)
        or not record_id
        or Path(record_id).name != record_id
        or record_id in {".", ".."}
        or action not in _BACKUP_ACTIONS
        or status not in _BACKUP_HISTORY_STATUSES
        or isinstance(success_count, bool)
        or not isinstance(success_count, int)
        or success_count < 0
        or isinstance(failure_count, bool)
        or not isinstance(failure_count, int)
        or failure_count < 0
        or not isinstance(raw_items, list)
    ):
        raise BackupError("备份操作历史字段损坏")
    _aware_datetime(created_at, label="备份操作历史时间")
    items = tuple(_history_item_from_json(item) for item in raw_items)
    actual_successes = sum(item.result == "success" for item in items)
    actual_failures = sum(item.result == "failed" for item in items)
    if (actual_successes, actual_failures) != (success_count, failure_count):
        raise BackupError("备份操作历史计数与明细不一致")
    has_cancelled = any(item.result == "cancelled" for item in items)
    if status == "cancelled" and not has_cancelled:
        raise BackupError("备份取消历史缺少取消明细")
    if status != "cancelled" and has_cancelled:
        raise BackupError("备份非取消历史混入取消明细")
    for item in items:
        if action == "backup":
            if item.restore_target is not None:
                raise BackupError("备份历史不应包含恢复目标")
            if item.result == "success" and (
                item.entry_id is None or item.backup_path is None
            ):
                raise BackupError("成功备份历史缺少条目或备份路径")
        elif action == "restore":
            if (
                item.entry_id is None
                or item.backup_path is None
                or item.restore_target is None
                or _path_key(item.source_path) != _path_key(item.backup_path)
            ):
                raise BackupError("恢复历史路径或条目标识损坏")
        elif action == "cleanup":
            if (
                item.entry_id is None
                or item.backup_path is None
                or item.restore_target is not None
                or _path_key(item.source_path) != _path_key(item.backup_path)
            ):
                raise BackupError("永久清理历史路径或条目标识损坏")
    return BackupHistoryRecord(
        record_id,
        str(action),
        str(status),
        str(created_at),
        success_count,
        failure_count,
        items,
    )


def _load_history(repository: LibraryRepository) -> list[BackupHistoryRecord]:
    setting = repository.get_setting(BACKUP_HISTORY_KEY)
    if setting is None:
        return []
    if not isinstance(setting.value, list):
        raise BackupError("备份操作历史不是列表")
    if len(setting.value) > _BACKUP_HISTORY_LIMIT:
        raise BackupError("备份操作历史超过 200 条，拒绝静默截断损坏数据")
    records = [_history_from_json(value) for value in setting.value]
    ids = {record.id for record in records}
    if len(ids) != len(records):
        raise BackupError("备份操作历史包含重复编号")
    return records


def _sorted_history(records: list[BackupHistoryRecord]) -> list[BackupHistoryRecord]:
    return sorted(
        records,
        key=lambda record: (
            _aware_datetime(record.created_at, label="备份操作历史时间").astimezone(timezone.utc),
            record.id,
        ),
        reverse=True,
    )


def _entry_to_json(entry: BackupEntry) -> dict[str, object]:
    value = asdict(entry)
    value["original_path"] = str(entry.original_path)
    value["backup_path"] = str(entry.backup_path)
    value["allowed_root"] = None if entry.allowed_root is None else str(entry.allowed_root)
    return value


def _entry_from_json(value: object) -> BackupEntry:
    if not isinstance(value, dict):
        raise BackupError("备份清单项格式损坏")
    try:
        entry = BackupEntry(
            id=str(value["id"]),
            asset_id=str(value["asset_id"]),
            kind=str(value["kind"]),
            original_path=Path(str(value["original_path"])),
            backup_path=Path(str(value["backup_path"])),
            sha256=str(value["sha256"]),
            created_at=str(value["created_at"]),
            restored_at=None if value.get("restored_at") is None else str(value["restored_at"]),
            allowed_root=None
            if value.get("allowed_root") is None
            else Path(str(value["allowed_root"])),
        )
    except Exception as error:
        raise BackupError("备份清单项字段损坏") from error
    if (
        not entry.id
        or Path(entry.id).name != entry.id
        or entry.id in {".", ".."}
        or not entry.asset_id
        or entry.kind not in {"audio", "lyric"}
        or not entry.original_path.is_absolute()
        or not entry.backup_path.is_absolute()
        or (entry.allowed_root is not None and not entry.allowed_root.is_absolute())
        or len(entry.sha256) != 64
        or any(character not in "0123456789abcdefABCDEF" for character in entry.sha256)
    ):
        raise BackupError("备份清单包含无效路径或标识")
    return entry


def _file_identity(metadata: os.stat_result) -> tuple[int, int, int, int]:
    return (
        int(metadata.st_dev),
        int(metadata.st_ino),
        int(metadata.st_size),
        int(metadata.st_mtime_ns),
    )


def _verified_file_sha256(path: Path) -> str:
    """Hash one unchanged, ordinary file without following a link/reparse point."""

    before = os.lstat(path)
    if not stat.S_ISREG(before.st_mode) or stat.S_ISLNK(before.st_mode) or _is_reparse(before):
        raise BackupError("备份路径不是普通文件")
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        opened = os.fstat(handle.fileno())
        if _file_identity(opened) != _file_identity(before):
            raise BackupError("备份文件在打开前发生变化")
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
        if _file_identity(os.fstat(handle.fileno())) != _file_identity(opened):
            raise BackupError("备份文件在校验期间发生变化")
    if _file_identity(os.lstat(path)) != _file_identity(opened):
        raise BackupError("备份路径在校验期间发生变化")
    return digest.hexdigest()


class BackupWorker(QThread):
    completed = Signal(object)
    failed = Signal(str)

    def __init__(self, *, action: str, payload: tuple[object, ...], backup_root: Path, repository_factory, retention_days: int = 7, parent=None) -> None:
        super().__init__(parent)
        self._action = action
        self._payload = payload
        self._backup_root = backup_root
        self._repository_factory = repository_factory
        self._retention_days = retention_days
        self._cancel = threading.Event()
        self._history_id = str(uuid4())
        self._created_at = datetime.now(timezone.utc).isoformat()

    def request_cancel(self) -> None:
        self._cancel.set()

    def run(self) -> None:
        repository = None
        try:
            repository = self._repository_factory()
            self._validate_backup_root()
            entries = self._load(repository, self._backup_root)
            if self._action == "backup":
                result = self._backup(repository, entries)
            elif self._action == "restore":
                result = self._restore(repository, entries)
            elif self._action == "cleanup":
                result = self._cleanup(repository, entries)
            else:
                raise BackupError("未知备份操作")
            self.completed.emit(self._save_history(repository, result))
        except Exception as error:
            message = str(error).strip() or error.__class__.__name__
            if repository is not None and self._action in _BACKUP_ACTIONS:
                failed_record = BackupHistoryRecord(
                    self._history_id,
                    self._action,
                    "failed",
                    self._created_at,
                    0,
                    0,
                    (),
                )
                try:
                    self._append_history(repository, failed_record)
                except Exception as history_error:
                    message += f"；警告：永久操作历史保存失败：{history_error}"
            self.failed.emit(message)
        finally:
            if repository is not None:
                repository.close()

    @staticmethod
    def _append_history(
        repository: LibraryRepository,
        record: BackupHistoryRecord,
    ) -> None:
        history = _load_history(repository)
        if any(existing.id == record.id for existing in history):
            raise BackupError("备份操作历史编号重复")
        persisted = _sorted_history(history + [record])[:_BACKUP_HISTORY_LIMIT]
        repository.set_setting(
            BACKUP_HISTORY_KEY,
            [_history_to_json(existing) for existing in persisted],
        )

    def _save_history(
        self,
        repository: LibraryRepository,
        result: BackupRunResult,
    ) -> BackupRunResult:
        record = BackupHistoryRecord(
            self._history_id,
            result.action,
            result.status,
            self._created_at,
            result.success_count,
            result.failure_count,
            result.items,
        )
        try:
            self._append_history(repository, record)
        except Exception as error:
            warning = f"警告：文件操作已完成，但永久操作历史保存失败：{error}"
            return replace(result, messages=result.messages + (warning,))
        return replace(result, history_id=self._history_id)

    @staticmethod
    def _history_item(
        *,
        entry_id: str | None,
        asset_id: str,
        kind: str,
        source_path: Path,
        backup_path: Path | None,
        restore_target: Path | None,
        result: str,
        message: str,
    ) -> BackupHistoryItem:
        return BackupHistoryItem(
            entry_id,
            asset_id,
            kind,
            source_path,
            backup_path,
            restore_target,
            result,
            message,
            datetime.now(timezone.utc).isoformat(),
        )

    @staticmethod
    def _validate_backup_root_path(backup_root: Path) -> None:
        if not isinstance(backup_root, Path) or not backup_root.is_absolute():
            raise BackupError("备份根目录必须是绝对 Path")
        try:
            _validate_directory_chain(backup_root, backup_root)
        except (OSError, RuntimeError) as error:
            raise BackupError(f"备份根目录不安全：{error}") from error

    def _validate_backup_root(self) -> None:
        self._validate_backup_root_path(self._backup_root)

    @staticmethod
    def _validate_entry(
        repository: LibraryRepository,
        backup_root: Path,
        entry: BackupEntry,
    ) -> None:
        expected_parent = backup_root / entry.id
        if (
            _path_key(entry.backup_path.parent) != _path_key(expected_parent)
            or not _within_root(entry.backup_path, expected_parent)
            or entry.backup_path.name != entry.original_path.name
        ):
            raise BackupError("备份清单路径与条目标识不一致")
        if entry.allowed_root is not None and not _within_root(
            entry.original_path, entry.allowed_root
        ):
            raise BackupError("备份清单的扫描来源根与原路径不一致")
        asset = repository.get_asset_by_id(entry.asset_id)
        if (
            asset is None
            or asset.kind != entry.kind
            or _path_key(asset.canonical_path) != _path_key(entry.original_path)
        ):
            raise BackupError("备份清单与索引资产不一致")
        if expected_parent.exists():
            try:
                _validate_directory_chain(backup_root, expected_parent)
            except (OSError, RuntimeError) as error:
                raise BackupError(f"备份目录链不安全：{error}") from error

    @classmethod
    def _load(
        cls,
        repository: LibraryRepository,
        backup_root: Path,
    ) -> list[BackupEntry]:
        setting = repository.get_setting(BACKUP_MANIFEST_KEY)
        if setting is None:
            return []
        if not isinstance(setting.value, list):
            raise BackupError("备份清单不是列表")
        cls._validate_backup_root_path(backup_root)
        entries = [_entry_from_json(value) for value in setting.value]
        ids: set[str] = set()
        paths: set[str] = set()
        for entry in entries:
            if entry.id in ids or _path_key(entry.backup_path) in paths:
                raise BackupError("备份清单包含重复条目")
            ids.add(entry.id)
            paths.add(_path_key(entry.backup_path))
            cls._validate_entry(repository, backup_root, entry)
        return entries

    @staticmethod
    def _save(repository: LibraryRepository, entries: list[BackupEntry]) -> None:
        repository.set_setting(
            BACKUP_MANIFEST_KEY,
            [
                _entry_to_json(entry)
                for entry in entries
                if entry.restored_at is None
            ],
        )

    def _backup(self, repository: LibraryRepository, entries: list[BackupEntry]) -> BackupRunResult:
        successes = failures = 0
        messages: list[str] = []
        affected_roots: list[tuple[str, Path]] = []
        history_items: list[BackupHistoryItem] = []
        cancelled = False
        current_matches = repository.list_lyrics_matches(current_only=True)
        referenced_lyrics = {match.lyric_asset_id for match in current_matches if match.lyric_asset_id}
        for index, value in enumerate(self._payload):
            if self._cancel.is_set():
                cancelled = True
                for pending in self._payload[index:]:
                    if isinstance(pending, BackupInput):
                        history_items.append(
                            self._history_item(
                                entry_id=None,
                                asset_id=pending.asset_id,
                                kind=pending.kind,
                                source_path=pending.source_path,
                                backup_path=None,
                                restore_target=None,
                                result="cancelled",
                                message="操作已取消，未开始备份",
                            )
                        )
                break
            if not isinstance(value, BackupInput):
                raise BackupError("备份输入格式无效")
            if value.kind == "lyric" and value.asset_id in referenced_lyrics:
                failures += 1
                message = f"歌词仍被音乐引用，拒绝删除：{value.source_path.name}"
                messages.append(message)
                history_items.append(
                    self._history_item(
                        entry_id=None,
                        asset_id=value.asset_id,
                        kind=value.kind,
                        source_path=value.source_path,
                        backup_path=None,
                        restore_target=None,
                        result="failed",
                        message=message,
                    )
                )
                continue
            entry_id: str | None = None
            backup_path: Path | None = None
            try:
                asset = repository.get_asset_by_id(value.asset_id)
                if asset is None or asset.file_state != "active" or asset.canonical_path != value.source_path:
                    raise BackupError("索引快照已变化，请重新扫描")
                if not _within_root(value.source_path, value.allowed_root):
                    raise BackupError("源文件超出授权根")
                entry_id = str(uuid4())
                directory = self._backup_root / entry_id
                directory.mkdir()
                moved = import_one(
                    value.source_path,
                    source_root=value.allowed_root,
                    target_root=directory,
                )
                if moved.status != "success" or moved.sha256 is None:
                    raise BackupError(moved.message)
                backup_path = moved.target_path
                entry = BackupEntry(
                    id=entry_id,
                    asset_id=value.asset_id,
                    kind=value.kind,
                    original_path=value.source_path,
                    backup_path=moved.target_path,
                    sha256=moved.sha256,
                    created_at=datetime.now(timezone.utc).isoformat(),
                    allowed_root=value.allowed_root,
                )
                self._validate_entry(repository, self._backup_root, entry)
                candidate_entries = entries + [entry]
                try:
                    self._save(repository, candidate_entries)
                except Exception as save_error:
                    rollback = import_one(
                        moved.target_path,
                        source_root=directory,
                        target_root=value.source_path.parent,
                    )
                    if rollback.status != "success" or _path_key(rollback.target_path) != _path_key(value.source_path):
                        raise BackupError(
                            f"备份清单保存失败且源文件恢复失败：{rollback.message}"
                        ) from save_error
                    try:
                        directory.rmdir()
                    except OSError:
                        pass
                    raise
                entries.append(entry)
                successes += 1
                root_key = (value.kind, value.allowed_root)
                if root_key not in affected_roots:
                    affected_roots.append(root_key)
                history_items.append(
                    self._history_item(
                        entry_id=entry.id,
                        asset_id=value.asset_id,
                        kind=value.kind,
                        source_path=value.source_path,
                        backup_path=entry.backup_path,
                        restore_target=None,
                        result="success",
                        message="已安全移入备份目录",
                    )
                )
            except Exception as error:
                failures += 1
                message = f"{value.source_path.name}：{error}"
                messages.append(message)
                history_items.append(
                    self._history_item(
                        entry_id=entry_id,
                        asset_id=value.asset_id,
                        kind=value.kind,
                        source_path=value.source_path,
                        backup_path=backup_path,
                        restore_target=None,
                        result="failed",
                        message=str(error).strip() or error.__class__.__name__,
                    )
                )
        return BackupRunResult(
            "backup",
            successes,
            failures,
            tuple(messages),
            tuple(affected_roots),
            "cancelled" if cancelled else "completed",
            None,
            tuple(history_items),
        )

    def _restore(self, repository: LibraryRepository, entries: list[BackupEntry]) -> BackupRunResult:
        wanted_order = tuple(str(value) for value in self._payload)
        wanted = set(wanted_order)
        if len(wanted) != len(wanted_order):
            raise BackupError("恢复条目编号重复")
        by_id = {entry.id: entry for entry in entries}
        missing_ids = wanted.difference(by_id)
        if missing_ids:
            raise BackupError("所选备份条目不存在或已不可恢复")
        selected = [by_id[entry_id] for entry_id in wanted_order]
        successes = failures = 0
        messages: list[str] = []
        affected_roots: list[tuple[str, Path]] = []
        history_items: list[BackupHistoryItem] = []
        cancelled = False
        current = list(entries)
        for position, entry in enumerate(selected):
            if self._cancel.is_set():
                cancelled = True
                for pending in selected[position:]:
                    history_items.append(
                        self._history_item(
                            entry_id=pending.id,
                            asset_id=pending.asset_id,
                            kind=pending.kind,
                            source_path=pending.backup_path,
                            backup_path=pending.backup_path,
                            restore_target=pending.original_path,
                            result="cancelled",
                            message="操作已取消，未开始恢复",
                        )
                    )
                break
            if entry.restored_at is not None:
                failures += 1
                message = f"{entry.original_path.name}：备份已经恢复，不能重复恢复"
                messages.append(message)
                history_items.append(
                    self._history_item(
                        entry_id=entry.id,
                        asset_id=entry.asset_id,
                        kind=entry.kind,
                        source_path=entry.backup_path,
                        backup_path=entry.backup_path,
                        restore_target=entry.original_path,
                        result="failed",
                        message="备份已经恢复，不能重复恢复",
                    )
                )
                continue
            try:
                self._validate_entry(repository, self._backup_root, entry)
                if entry.original_path.exists():
                    raise BackupError("原路径已存在，禁止覆盖")
                with _locked_directory_chain(self._backup_root, entry.backup_path.parent):
                    if _verified_file_sha256(entry.backup_path) != entry.sha256:
                        raise BackupError("备份文件 SHA-256 与清单不一致，拒绝恢复")
                result = import_one(
                    entry.backup_path,
                    source_root=entry.backup_path.parent,
                    target_root=entry.original_path.parent,
                )
                try:
                    if (
                        result.status != "success"
                        or result.sha256 != entry.sha256
                        or _path_key(result.target_path) != _path_key(entry.original_path)
                    ):
                        raise BackupError("恢复文件落位结果与清单不一致")
                    with _locked_directory_chain(entry.original_path.parent, entry.original_path.parent):
                        if _verified_file_sha256(entry.original_path) != entry.sha256:
                            raise BackupError("恢复文件落位后 SHA-256 校验失败")
                    candidate = [value for value in current if value.id != entry.id]
                    self._save(repository, candidate)
                except Exception as restore_error:
                    rollback = import_one(
                        entry.original_path,
                        source_root=entry.original_path.parent,
                        target_root=entry.backup_path.parent,
                    )
                    if (
                        rollback.status != "success"
                        or _path_key(rollback.target_path) != _path_key(entry.backup_path)
                    ):
                        raise BackupError(
                            f"恢复失败且备份文件回滚失败：{rollback.message}"
                        ) from restore_error
                    raise
                current = candidate
                successes += 1
                if entry.allowed_root is not None:
                    root_key = (entry.kind, entry.allowed_root)
                    if root_key not in affected_roots:
                        affected_roots.append(root_key)
                history_items.append(
                    self._history_item(
                        entry_id=entry.id,
                        asset_id=entry.asset_id,
                        kind=entry.kind,
                        source_path=entry.backup_path,
                        backup_path=entry.backup_path,
                        restore_target=entry.original_path,
                        result="success",
                        message="已恢复到原路径",
                    )
                )
            except Exception as error:
                failures += 1
                message = f"{entry.original_path.name}：{error}"
                messages.append(message)
                history_items.append(
                    self._history_item(
                        entry_id=entry.id,
                        asset_id=entry.asset_id,
                        kind=entry.kind,
                        source_path=entry.backup_path,
                        backup_path=entry.backup_path,
                        restore_target=entry.original_path,
                        result="failed",
                        message=str(error).strip() or error.__class__.__name__,
                    )
                )
        return BackupRunResult(
            "restore",
            successes,
            failures,
            tuple(messages),
            tuple(affected_roots),
            "cancelled" if cancelled else "completed",
            None,
            tuple(history_items),
        )

    def _cleanup(self, repository: LibraryRepository, entries: list[BackupEntry]) -> BackupRunResult:
        threshold = datetime.now(timezone.utc) - timedelta(days=self._retention_days)
        current = list(entries)
        successes = failures = 0
        messages: list[str] = []
        history_items: list[BackupHistoryItem] = []
        eligible: list[BackupEntry] = []
        for entry in entries:
            created = _aware_datetime(entry.created_at, label="备份清单创建时间")
            if entry.restored_at is None and created <= threshold:
                eligible.append(entry)
        cancelled = False
        for position, entry in enumerate(eligible):
            if self._cancel.is_set():
                cancelled = True
                for pending in eligible[position:]:
                    history_items.append(
                        self._history_item(
                            entry_id=pending.id,
                            asset_id=pending.asset_id,
                            kind=pending.kind,
                            source_path=pending.backup_path,
                            backup_path=pending.backup_path,
                            restore_target=None,
                            result="cancelled",
                            message="操作已取消，未开始永久清理",
                        )
                    )
                break
            try:
                self._validate_entry(repository, self._backup_root, entry)
                tombstone = entry.backup_path.parent / f".{entry.backup_path.name}.{uuid4().hex}.cleanup"
                with _locked_directory_chain(self._backup_root, entry.backup_path.parent):
                    if _verified_file_sha256(entry.backup_path) != entry.sha256:
                        raise BackupError("备份文件 SHA-256 与清单不一致，拒绝清理")
                    os.rename(entry.backup_path, tombstone)
                    candidate = [value for value in current if value.id != entry.id]
                    try:
                        self._save(repository, candidate)
                    except Exception:
                        os.rename(tombstone, entry.backup_path)
                        raise
                    try:
                        os.unlink(tombstone)
                    except Exception as unlink_error:
                        os.rename(tombstone, entry.backup_path)
                        try:
                            self._save(repository, current)
                        except Exception as manifest_error:
                            raise BackupError(
                                f"清理失败且清单恢复失败：{manifest_error}"
                            ) from unlink_error
                        raise
                    current = candidate
                try:
                    entry.backup_path.parent.rmdir()
                except OSError:
                    pass
                successes += 1
                history_items.append(
                    self._history_item(
                        entry_id=entry.id,
                        asset_id=entry.asset_id,
                        kind=entry.kind,
                        source_path=entry.backup_path,
                        backup_path=entry.backup_path,
                        restore_target=None,
                        result="success",
                        message="已永久清理备份文件",
                    )
                )
            except Exception as error:
                failures += 1
                message = f"{entry.backup_path.name}：{error}"
                messages.append(message)
                history_items.append(
                    self._history_item(
                        entry_id=entry.id,
                        asset_id=entry.asset_id,
                        kind=entry.kind,
                        source_path=entry.backup_path,
                        backup_path=entry.backup_path,
                        restore_target=None,
                        result="failed",
                        message=str(error).strip() or error.__class__.__name__,
                    )
                )
        return BackupRunResult(
            "cleanup",
            successes,
            failures,
            tuple(messages),
            (),
            "cancelled" if cancelled else "completed",
            None,
            tuple(history_items),
        )


class BackupController(QObject):
    completed = Signal(object)
    failed = Signal(str)
    running_changed = Signal(bool)

    def __init__(self, *, backup_root: Path, repository_factory, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._backup_root = backup_root
        self._repository_factory = repository_factory
        self._worker: BackupWorker | None = None
        self._terminal: tuple[str, object] | None = None

    @property
    def running(self) -> bool:
        return self._worker is not None

    @property
    def backup_root(self) -> Path:
        return self._backup_root

    def prepare_backup_root(self) -> Path:
        if not isinstance(self._backup_root, Path) or not self._backup_root.is_absolute():
            raise BackupError("备份目录必须是绝对 Path")
        try:
            self._backup_root.mkdir(parents=True, exist_ok=True)
            BackupWorker._validate_backup_root_path(self._backup_root)
        except BackupError:
            raise
        except Exception as error:
            raise BackupError(f"无法准备备份目录：{error}") from error
        return self._backup_root

    def list_entries(self) -> tuple[BackupEntry, ...]:
        with self._repository_factory() as repository:
            return tuple(
                entry
                for entry in BackupWorker._load(repository, self._backup_root)
                if entry.restored_at is None
            )

    def list_history(self) -> tuple[BackupHistoryRecord, ...]:
        """Return permanent, read-only operation history newest first."""

        with self._repository_factory() as repository:
            return tuple(_sorted_history(_load_history(repository)))

    def list_operation_history(self) -> tuple[BackupHistoryRecord, ...]:
        """Compatibility alias used by history aggregation services."""

        return self.list_history()

    def retention_days(self) -> int | None:
        with self._repository_factory() as repository:
            setting = repository.get_setting(BACKUP_RETENTION_KEY)
        value = 7 if setting is None else setting.value
        if value not in {7, 15, 30, None}:
            raise BackupError("备份保留时间设置损坏")
        return value

    def set_retention_days(self, days: int | None) -> None:
        if days not in {7, 15, 30, None}:
            raise BackupError("备份保留时间只支持 7、15、30 天或永久")
        with self._repository_factory() as repository:
            repository.set_setting(BACKUP_RETENTION_KEY, days)

    def cleanup_preview(self) -> BackupCleanupPreview:
        retention_days = self.retention_days()
        if retention_days is None:
            return BackupCleanupPreview(self._backup_root, None, 0)
        threshold = datetime.now(timezone.utc) - timedelta(days=retention_days)
        eligible_count = 0
        for entry in self.list_entries():
            try:
                created_at = datetime.fromisoformat(entry.created_at)
            except (TypeError, ValueError) as error:
                raise BackupError("备份清单包含无效创建时间") from error
            if created_at.tzinfo is None:
                raise BackupError("备份清单创建时间缺少时区")
            if entry.restored_at is None and created_at <= threshold:
                eligible_count += 1
        return BackupCleanupPreview(self._backup_root, retention_days, eligible_count)

    def start_backup(self, items: tuple[BackupInput, ...]) -> None:
        self._start("backup", items)

    def start_restore(self, entry_ids: tuple[str, ...]) -> None:
        self._start("restore", entry_ids)

    def start_cleanup(self, *, retention_days: int | None = None) -> None:
        effective = self.retention_days() if retention_days is None else retention_days
        if effective is None:
            raise BackupError("当前设置为永久保留，没有到期备份可清理")
        self._start("cleanup", (), retention_days=effective)

    def _start(self, action: str, payload: tuple[object, ...], *, retention_days: int = 7) -> None:
        if self.running:
            raise RuntimeError("已有备份任务正在运行")
        if action != "cleanup" and not payload:
            raise BackupError("至少选择一项")
        if action == "backup":
            for value in payload:
                if (
                    not isinstance(value, BackupInput)
                    or not value.asset_id
                    or value.kind not in {"audio", "lyric"}
                    or not isinstance(value.source_path, Path)
                    or not value.source_path.is_absolute()
                    or not isinstance(value.allowed_root, Path)
                    or not value.allowed_root.is_absolute()
                ):
                    raise BackupError("备份输入格式无效")
        elif action == "restore":
            if (
                any(not isinstance(value, str) or not value for value in payload)
                or len(set(payload)) != len(payload)
            ):
                raise BackupError("恢复条目编号无效或重复")
        elif action == "cleanup":
            if isinstance(retention_days, bool) or not isinstance(retention_days, int) or retention_days < 0:
                raise BackupError("清理保留天数必须是非负整数")
        else:
            raise BackupError("未知备份操作")
        self._backup_root.mkdir(parents=True, exist_ok=True)
        worker = BackupWorker(action=action, payload=payload, backup_root=self._backup_root, repository_factory=self._repository_factory, retention_days=retention_days)
        worker.completed.connect(lambda value: self._cache("completed", value))
        worker.failed.connect(lambda value: self._cache("failed", value))
        worker.finished.connect(self._finished)
        self._worker = worker
        self._terminal = None
        self.running_changed.emit(True)
        worker.start()

    def request_cancel(self) -> None:
        if self._worker is not None:
            self._worker.request_cancel()

    def _cache(self, kind: str, value: object) -> None:
        if self._terminal is None:
            self._terminal = (kind, value)

    def _finished(self) -> None:
        worker = self._worker
        if worker is None:
            return
        kind, value = self._terminal or ("failed", "备份线程结束但没有终态")
        self._worker = None
        self._terminal = None
        worker.deleteLater()
        self.running_changed.emit(False)
        if kind == "completed":
            self.completed.emit(value)
        else:
            self.failed.emit(str(value))
