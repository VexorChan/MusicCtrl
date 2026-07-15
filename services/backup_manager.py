"""P7 verified backup, restore and retention for user-confirmed deletions."""

from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import stat
import threading
from uuid import uuid4

from PySide6.QtCore import QObject, QThread, Signal

from repositories import LibraryRepository
from services.file_safety import _is_reparse, _within_root
from services.safe_import import import_one


BACKUP_MANIFEST_KEY = "p7.backup_entries"
BACKUP_RETENTION_KEY = "p7.retention_days"


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


@dataclass(frozen=True, slots=True)
class BackupRunResult:
    action: str
    success_count: int
    failure_count: int
    messages: tuple[str, ...]


def _entry_to_json(entry: BackupEntry) -> dict[str, object]:
    value = asdict(entry)
    value["original_path"] = str(entry.original_path)
    value["backup_path"] = str(entry.backup_path)
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
        )
    except Exception as error:
        raise BackupError("备份清单项字段损坏") from error
    if not entry.id or not entry.original_path.is_absolute() or not entry.backup_path.is_absolute():
        raise BackupError("备份清单包含无效路径或标识")
    return entry


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

    def request_cancel(self) -> None:
        self._cancel.set()

    def run(self) -> None:
        repository = None
        try:
            repository = self._repository_factory()
            entries = self._load(repository)
            if self._action == "backup":
                result = self._backup(repository, entries)
            elif self._action == "restore":
                result = self._restore(repository, entries)
            elif self._action == "cleanup":
                result = self._cleanup(repository, entries)
            else:
                raise BackupError("未知备份操作")
            self.completed.emit(result)
        except Exception as error:
            self.failed.emit(str(error).strip() or error.__class__.__name__)
        finally:
            if repository is not None:
                repository.close()

    @staticmethod
    def _load(repository: LibraryRepository) -> list[BackupEntry]:
        setting = repository.get_setting(BACKUP_MANIFEST_KEY)
        if setting is None:
            return []
        if not isinstance(setting.value, list):
            raise BackupError("备份清单不是列表")
        return [_entry_from_json(value) for value in setting.value]

    @staticmethod
    def _save(repository: LibraryRepository, entries: list[BackupEntry]) -> None:
        repository.set_setting(BACKUP_MANIFEST_KEY, [_entry_to_json(entry) for entry in entries])

    def _backup(self, repository: LibraryRepository, entries: list[BackupEntry]) -> BackupRunResult:
        successes = failures = 0
        messages: list[str] = []
        current_matches = repository.list_lyrics_matches(current_only=True)
        referenced_lyrics = {match.lyric_asset_id for match in current_matches if match.lyric_asset_id}
        for value in self._payload:
            if self._cancel.is_set():
                break
            if not isinstance(value, BackupInput):
                failures += 1
                messages.append("备份输入格式无效")
                continue
            if value.kind == "lyric" and value.asset_id in referenced_lyrics:
                failures += 1
                messages.append(f"歌词仍被音乐引用，拒绝删除：{value.source_path.name}")
                continue
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
                entry = BackupEntry(
                    entry_id,
                    value.asset_id,
                    value.kind,
                    value.source_path,
                    moved.target_path,
                    moved.sha256,
                    datetime.now(timezone.utc).isoformat(),
                )
                candidate_entries = entries + [entry]
                try:
                    self._save(repository, candidate_entries)
                except Exception:
                    import_one(moved.target_path, source_root=directory, target_root=value.source_path.parent)
                    directory.rmdir()
                    raise
                entries.append(entry)
                successes += 1
            except Exception as error:
                failures += 1
                messages.append(f"{value.source_path.name}：{error}")
        return BackupRunResult("backup", successes, failures, tuple(messages))

    def _restore(self, repository: LibraryRepository, entries: list[BackupEntry]) -> BackupRunResult:
        wanted = {str(value) for value in self._payload}
        successes = failures = 0
        messages: list[str] = []
        updated = list(entries)
        for index, entry in enumerate(entries):
            if entry.id not in wanted or entry.restored_at is not None:
                continue
            try:
                if entry.original_path.exists():
                    raise BackupError("原路径已存在，禁止覆盖")
                result = import_one(
                    entry.backup_path,
                    source_root=entry.backup_path.parent,
                    target_root=entry.original_path.parent,
                )
                if result.status != "success" or result.sha256 != entry.sha256:
                    raise BackupError("恢复文件哈希不一致")
                updated[index] = BackupEntry(**{**asdict(entry), "restored_at": datetime.now(timezone.utc).isoformat()})
                try:
                    self._save(repository, updated)
                except Exception:
                    import_one(entry.original_path, source_root=entry.original_path.parent, target_root=entry.backup_path.parent)
                    raise
                successes += 1
            except Exception as error:
                failures += 1
                messages.append(f"{entry.original_path.name}：{error}")
        return BackupRunResult("restore", successes, failures, tuple(messages))

    def _cleanup(self, repository: LibraryRepository, entries: list[BackupEntry]) -> BackupRunResult:
        threshold = datetime.now(timezone.utc) - timedelta(days=self._retention_days)
        kept: list[BackupEntry] = []
        successes = failures = 0
        messages: list[str] = []
        for entry in entries:
            created = datetime.fromisoformat(entry.created_at)
            if entry.restored_at is not None or created > threshold:
                kept.append(entry)
                continue
            try:
                metadata = os.lstat(entry.backup_path)
                if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode) or _is_reparse(metadata):
                    raise BackupError("备份路径不是普通文件")
                os.unlink(entry.backup_path)
                entry.backup_path.parent.rmdir()
                successes += 1
            except Exception as error:
                kept.append(entry)
                failures += 1
                messages.append(f"{entry.backup_path.name}：{error}")
        self._save(repository, kept)
        return BackupRunResult("cleanup", successes, failures, tuple(messages))


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

    def list_entries(self) -> tuple[BackupEntry, ...]:
        with self._repository_factory() as repository:
            return tuple(BackupWorker._load(repository))

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
