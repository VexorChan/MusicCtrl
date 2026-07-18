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
PENDING_CLEANUP_KEY = "p7.pending_cleanup"
PENDING_LINKED_BACKUP_KEY = "p7.pending_linked_backup"
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
    include_linked_lyrics: bool = False
    expected_size_bytes: int | None = None
    expected_mtime_ns: int | None = None
    link_group_id: str | None = None
    lyrics_match_id: str | None = None
    linked_audio_asset_id: str | None = None


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
    linked_entry_id: str | None = None
    link_group_id: str | None = None
    lyrics_match_id: str | None = None
    linked_audio_asset_id: str | None = None


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
            linked_entry_id=None
            if value.get("linked_entry_id") is None
            else str(value["linked_entry_id"]),
            link_group_id=None
            if value.get("link_group_id") is None
            else str(value["link_group_id"]),
            lyrics_match_id=None
            if value.get("lyrics_match_id") is None
            else str(value["lyrics_match_id"]),
            linked_audio_asset_id=None
            if value.get("linked_audio_asset_id") is None
            else str(value["linked_audio_asset_id"]),
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
        or (
            entry.linked_entry_id is not None
            and (
                not entry.linked_entry_id
                or Path(entry.linked_entry_id).name != entry.linked_entry_id
            )
        )
        or any(
            value is not None and (not value or Path(value).name != value)
            for value in (
                entry.link_group_id,
                entry.lyrics_match_id,
                entry.linked_audio_asset_id,
            )
        )
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
            self._recover_pending_linked_backup(repository)
            self._recover_pending_cleanup(repository)
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

    def _build_linked_backup_plan(
        self,
        audio: BackupInput,
        lyric: BackupInput,
        relation,
    ) -> dict[str, object]:
        if (
            audio.link_group_id is None
            or audio.link_group_id != lyric.link_group_id
            or audio.lyrics_match_id is None
            or relation.id != audio.lyrics_match_id
        ):
            raise BackupError("关联备份计划与歌词关系不一致")
        members: list[dict[str, object]] = []
        created_at = datetime.now(timezone.utc).isoformat()
        for item in (audio, lyric):
            metadata = os.lstat(item.source_path)
            entry_id = str(uuid4())
            members.append({
                "entry_id": entry_id,
                "asset_id": item.asset_id,
                "kind": item.kind,
                "source_path": str(item.source_path),
                "allowed_root": str(item.allowed_root),
                "backup_path": str(
                    self._backup_root / entry_id / item.source_path.name
                ),
                "expected_size_bytes": int(metadata.st_size),
                "expected_mtime_ns": int(metadata.st_mtime_ns),
                "created_at": created_at,
                "link_group_id": item.link_group_id,
                "lyrics_match_id": item.lyrics_match_id,
                "linked_audio_asset_id": item.linked_audio_asset_id,
                "sha256": None,
            })
        return {
            "version": 2,
            "state": "planned",
            "group_id": audio.link_group_id,
            "members": members,
            "relation": {
                "id": relation.id,
                "audio_asset_id": relation.audio_asset_id,
                "lyric_asset_id": relation.lyric_asset_id,
                "source_kind": relation.source_kind,
                "confidence": relation.confidence,
                "method": relation.method,
                "state": relation.state,
                "is_current": relation.is_current,
                "created_at": relation.created_at,
                "updated_at": relation.updated_at,
            },
        }

    @staticmethod
    def _planned_member(
        plan: dict[str, object],
        kind: str,
    ) -> dict[str, object]:
        raw_members = plan.get("members")
        if not isinstance(raw_members, list):
            raise BackupError("关联备份计划成员损坏")
        matches = [
            member
            for member in raw_members
            if isinstance(member, dict) and member.get("kind") == kind
        ]
        if len(matches) != 1:
            raise BackupError("关联备份计划缺少唯一成员")
        return matches[0]

    def _advance_linked_backup_plan(
        self,
        repository: LibraryRepository,
        plan: dict[str, object],
        *,
        state: str,
        kind: str | None = None,
        sha256: str | None = None,
    ) -> None:
        plan["state"] = state
        if kind is not None:
            member = self._planned_member(plan, kind)
            member["sha256"] = sha256
        repository.set_setting(PENDING_LINKED_BACKUP_KEY, plan)

    def _recover_pending_linked_backup(self, repository: LibraryRepository) -> None:
        setting = repository.get_setting(PENDING_LINKED_BACKUP_KEY)
        if setting is None or setting.value == []:
            return
        value = setting.value
        if (
            not isinstance(value, dict)
            or value.get("version") != 2
            or not isinstance(value.get("group_id"), str)
            or not value.get("group_id")
        ):
            raise BackupError("关联备份回滚日志损坏")
        group_id = str(value["group_id"])
        raw_members = value.get("members")
        relation = value.get("relation")
        if (
            not isinstance(raw_members, list)
            or len(raw_members) != 2
            or not isinstance(relation, dict)
            or relation.get("id") is None
            or relation.get("source_kind") != "external"
            or relation.get("state") != "matched"
            or relation.get("is_current") is not True
        ):
            raise BackupError("关联备份计划关系或成员损坏")
        plans: dict[str, dict[str, object]] = {}
        for raw_member in raw_members:
            if not isinstance(raw_member, dict):
                raise BackupError("关联备份计划成员格式损坏")
            kind = raw_member.get("kind")
            entry_id = raw_member.get("entry_id")
            source_path = Path(str(raw_member.get("source_path", "")))
            allowed_root = Path(str(raw_member.get("allowed_root", "")))
            backup_path = Path(str(raw_member.get("backup_path", "")))
            expected_size = raw_member.get("expected_size_bytes")
            expected_mtime = raw_member.get("expected_mtime_ns")
            if (
                kind not in {"audio", "lyric"}
                or kind in plans
                or not isinstance(entry_id, str)
                or not entry_id
                or Path(entry_id).name != entry_id
                or not source_path.is_absolute()
                or not allowed_root.is_absolute()
                or not backup_path.is_absolute()
                or not _within_root(source_path, allowed_root)
                or backup_path
                != self._backup_root / entry_id / source_path.name
                or isinstance(expected_size, bool)
                or not isinstance(expected_size, int)
                or expected_size < 0
                or (
                    expected_mtime is not None
                    and (
                        isinstance(expected_mtime, bool)
                        or not isinstance(expected_mtime, int)
                        or expected_mtime < 0
                    )
                )
                or raw_member.get("link_group_id") != group_id
                or raw_member.get("lyrics_match_id") != relation.get("id")
                or raw_member.get("linked_audio_asset_id")
                != relation.get("audio_asset_id")
            ):
                raise BackupError("关联备份计划路径或事实损坏")
            plans[str(kind)] = raw_member
        if set(plans) != {"audio", "lyric"}:
            raise BackupError("关联备份计划必须包含音乐和歌词")
        if (
            plans["audio"].get("asset_id") != relation.get("audio_asset_id")
            or plans["lyric"].get("asset_id") != relation.get("lyric_asset_id")
        ):
            raise BackupError("关联备份计划资产与关系快照不一致")
        manifest = self._load_unchecked(repository, self._backup_root)
        group = [item for item in manifest if item.link_group_id == group_id]
        if len(group) == 2:
            self._validate_manifest_groups(manifest)
            for entry in group:
                plan = plans[entry.kind]
                if (
                    entry.id != plan["entry_id"]
                    or _path_key(entry.backup_path)
                    != _path_key(Path(str(plan["backup_path"])))
                    or not entry.backup_path.exists()
                    or entry.original_path.exists()
                    or _verified_file_sha256(entry.backup_path) != entry.sha256
                ):
                    raise BackupError("已提交关联备份与计划或文件事实不一致")
            repository.set_setting(PENDING_LINKED_BACKUP_KEY, [])
            return
        if len(group) > 1 or (group and group[0].kind != "audio"):
            raise BackupError("关联备份回滚日志与清单不一致")
        for kind in ("lyric", "audio"):
            plan = plans[kind]
            source_path = Path(str(plan["source_path"]))
            backup_path = Path(str(plan["backup_path"]))
            source_exists = source_path.exists()
            backup_exists = backup_path.exists()
            if source_exists and backup_exists:
                raise BackupError("关联备份恢复时源文件与备份同时存在")
            if not source_exists and not backup_exists:
                raise BackupError("关联备份恢复时源文件与备份均不存在")
            if not backup_exists:
                continue
            backup_metadata = os.lstat(backup_path)
            if backup_metadata.st_size != plan["expected_size_bytes"]:
                raise BackupError("关联备份恢复时文件大小与计划不一致")
            actual_sha = _verified_file_sha256(backup_path)
            planned_sha = plan.get("sha256")
            if planned_sha is not None and planned_sha != actual_sha:
                raise BackupError("关联备份恢复时 SHA-256 与计划不一致")
            restored = import_one(
                backup_path,
                source_root=backup_path.parent,
                target_root=source_path.parent,
            )
            if (
                restored.status != "success"
                or restored.sha256 != actual_sha
                or _path_key(restored.target_path) != _path_key(source_path)
            ):
                raise BackupError(f"关联文件自动回滚失败：{restored.message}")
        candidate = [item for item in manifest if item.link_group_id != group_id]
        self._save(repository, candidate)
        repository.set_setting(PENDING_LINKED_BACKUP_KEY, [])
        for plan in plans.values():
            try:
                Path(str(plan["backup_path"])).parent.rmdir()
            except OSError:
                pass

    def _rollback_incomplete_linked_backup(
        self,
        repository: LibraryRepository,
        entries: list[BackupEntry],
        audio_entry: BackupEntry,
        *,
        reason: str,
    ) -> list[BackupEntry]:
        setting = repository.get_setting(PENDING_LINKED_BACKUP_KEY)
        if setting is None or not isinstance(setting.value, dict):
            raise BackupError("关联歌词失败但缺少可恢复计划日志")
        plan = setting.value
        plan["state"] = "rollback_required"
        plan["reason"] = reason
        repository.set_setting(PENDING_LINKED_BACKUP_KEY, plan)
        self._recover_pending_linked_backup(repository)
        return self._load(repository, self._backup_root)

    def _recover_pending_cleanup(self, repository: LibraryRepository) -> None:
        setting = repository.get_setting(PENDING_CLEANUP_KEY)
        if setting is None or setting.value == []:
            return
        value = setting.value
        if not isinstance(value, dict):
            raise BackupError("永久清理恢复日志损坏")
        raw_members = value.get("members")
        relation = value.get("relation")
        if not isinstance(raw_members, list) or not raw_members:
            raise BackupError("永久清理恢复日志成员损坏")
        members: list[tuple[BackupEntry, Path]] = []
        for raw_member in raw_members:
            if not isinstance(raw_member, dict):
                raise BackupError("永久清理恢复日志成员格式损坏")
            entry = _entry_from_json(raw_member.get("entry"))
            tombstone = Path(str(raw_member.get("tombstone_path", "")))
            if (
                not tombstone.is_absolute()
                or not _within_root(tombstone, self._backup_root)
                or tombstone.parent != entry.backup_path.parent
            ):
                raise BackupError("永久清理恢复日志路径损坏")
            members.append((entry, tombstone))
        member_entries = [entry for entry, _tombstone in members]
        if len({entry.id for entry in member_entries}) != len(member_entries):
            raise BackupError("永久清理恢复日志成员重复")
        if any(entry.link_group_id is not None for entry in member_entries):
            self._validate_manifest_groups(member_entries)
        relation_id: str | None = None
        if relation is not None:
            if (
                not isinstance(relation, dict)
                or relation.get("source_kind") != "external"
                or relation.get("state") != "matched"
                or relation.get("is_current") is not True
                or not isinstance(relation.get("id"), str)
                or not relation.get("id")
                or not isinstance(relation.get("audio_asset_id"), str)
                or not isinstance(relation.get("lyric_asset_id"), str)
            ):
                raise BackupError("永久清理恢复日志关系快照损坏")
            relation_id = str(relation["id"])
            if any(
                entry.lyrics_match_id != relation_id
                for entry in member_entries
                if entry.link_group_id is not None
            ):
                raise BackupError("永久清理恢复日志关系与组不一致")

        deleted_ids: set[str] = set()
        recoverable: list[BackupEntry] = []
        for entry, tombstone in members:
            backup_exists = entry.backup_path.exists()
            tombstone_exists = tombstone.exists()
            if backup_exists and tombstone_exists:
                raise BackupError("永久清理恢复时备份与墓碑同时存在")
            if tombstone_exists:
                os.rename(tombstone, entry.backup_path)
                recoverable.append(entry)
            elif backup_exists:
                recoverable.append(entry)
            else:
                deleted_ids.add(entry.id)

        manifest = self._load_unchecked(repository, self._backup_root)
        member_ids = {entry.id for entry in member_entries}
        candidate = [entry for entry in manifest if entry.id not in member_ids]
        if deleted_ids:
            candidate.extend(
                replace(
                    entry,
                    linked_entry_id=None,
                    link_group_id=None,
                    lyrics_match_id=None,
                    linked_audio_asset_id=None,
                )
                for entry in recoverable
            )
        else:
            candidate.extend(member_entries)
        self._validate_manifest_groups(candidate)
        self._save(repository, candidate)
        if relation_id is not None:
            repository.finalize_external_lyrics_cleanup(
                match_id=relation_id,
                journal_key=PENDING_CLEANUP_KEY,
                restore_relation=not deleted_ids,
            )
        else:
            repository.set_setting(PENDING_CLEANUP_KEY, [])

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
    def _load_unchecked(
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

    @classmethod
    def _load(
        cls,
        repository: LibraryRepository,
        backup_root: Path,
    ) -> list[BackupEntry]:
        entries = cls._load_unchecked(repository, backup_root)
        cls._validate_manifest_groups(entries)
        return entries

    @staticmethod
    def _validate_manifest_groups(entries: list[BackupEntry]) -> None:
        grouped: dict[str, list[BackupEntry]] = {}
        for entry in entries:
            linked_fields = (
                entry.link_group_id,
                entry.lyrics_match_id,
                entry.linked_audio_asset_id,
            )
            if entry.link_group_id is None:
                if any(value is not None for value in linked_fields[1:]):
                    raise BackupError("非关联备份条目混入关联字段")
                continue
            if any(value is None for value in linked_fields):
                raise BackupError("关联备份组字段不完整")
            grouped.setdefault(entry.link_group_id, []).append(entry)
        for group_id, members in grouped.items():
            if len(members) != 2 or {entry.kind for entry in members} != {"audio", "lyric"}:
                raise BackupError(f"关联备份组损坏：{group_id}")
            audio = next(entry for entry in members if entry.kind == "audio")
            lyric = next(entry for entry in members if entry.kind == "lyric")
            if (
                audio.lyrics_match_id != lyric.lyrics_match_id
                or audio.linked_audio_asset_id != audio.asset_id
                or lyric.linked_audio_asset_id != audio.asset_id
            ):
                raise BackupError(f"关联备份组关系字段不一致：{group_id}")

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

    def _prepare_backup_inputs(
        self,
        repository: LibraryRepository,
    ) -> tuple[BackupInput, ...]:
        requested = tuple(self._payload)
        if not requested or not all(isinstance(item, BackupInput) for item in requested):
            raise BackupError("备份输入格式无效")
        asset_ids = [item.asset_id for item in requested]
        if len(asset_ids) != len(set(asset_ids)):
            raise BackupError("备份输入包含重复资产")
        audio_roots = repository.latest_completed_audio_roots(
            item.asset_id for item in requested if item.kind == "audio"
        )
        lyric_roots = repository.latest_completed_lyric_roots(
            item.asset_id for item in requested if item.kind == "lyric"
        )
        links = {
            item.audio_asset_id: item
            for item in repository.current_external_lyrics_for_audio_ids(
                item.asset_id
                for item in requested
                if item.kind == "audio" and item.include_linked_lyrics
            )
        }
        expanded: list[BackupInput] = []
        linked_lyric_ids: set[str] = set()
        for item in requested:
            # 普通单文件删除保持既有“逐文件执行、逐文件审计”的部分成功语义。
            # 只有显式勾选关联歌词时，才必须在移动音乐前完成整个组合的预检。
            if item.kind != "audio" or not item.include_linked_lyrics:
                expanded.append(item)
                continue
            asset = repository.get_asset_by_id(item.asset_id)
            expected_root = (
                audio_roots.get(item.asset_id)
                if item.kind == "audio"
                else lyric_roots.get(item.asset_id)
            )
            if (
                asset is None
                or asset.kind != item.kind
                or asset.file_state != "active"
                or _path_key(asset.canonical_path) != _path_key(item.source_path)
                or expected_root is None
                or _path_key(expected_root) != _path_key(item.allowed_root)
            ):
                raise BackupError("索引、类型、状态或完成扫描来源已变化")
            metadata = os.lstat(item.source_path)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or stat.S_ISLNK(metadata.st_mode)
                or _is_reparse(metadata)
                or metadata.st_size != asset.size_bytes
                or metadata.st_mtime_ns != asset.mtime_ns
                or (
                    item.expected_size_bytes is not None
                    and item.expected_size_bytes != asset.size_bytes
                )
                or (
                    item.expected_mtime_ns is not None
                    and item.expected_mtime_ns != asset.mtime_ns
                )
            ):
                raise BackupError("源文件事实已变化，请重新扫描")
            expanded.append(item)
            relation = links.get(item.asset_id)
            if relation is None or relation.lyric_asset_id is None:
                continue
            lyric_id = relation.lyric_asset_id
            if lyric_id in linked_lyric_ids:
                continue
            references = repository.current_audio_ids_for_external_lyric(lyric_id)
            if references != (item.asset_id,):
                continue
            lyric = repository.get_asset_by_id(lyric_id)
            roots = repository.latest_completed_lyric_roots((lyric_id,))
            lyric_root = roots.get(lyric_id)
            if lyric is None or lyric.kind != "lyric" or lyric.file_state != "active" or lyric_root is None:
                raise BackupError("关联外部歌词不是 active 或缺少完成扫描来源")
            lyric_meta = os.lstat(lyric.canonical_path)
            if (
                not stat.S_ISREG(lyric_meta.st_mode)
                or stat.S_ISLNK(lyric_meta.st_mode)
                or _is_reparse(lyric_meta)
                or lyric_meta.st_size != lyric.size_bytes
                or lyric_meta.st_mtime_ns != lyric.mtime_ns
            ):
                raise BackupError("关联外部歌词文件事实已变化")
            group_id = str(uuid4())
            expanded[-1] = replace(
                item,
                link_group_id=group_id,
                lyrics_match_id=relation.id,
                linked_audio_asset_id=item.asset_id,
            )
            expanded.append(
                BackupInput(
                    lyric.id,
                    lyric.canonical_path,
                    lyric_root,
                    "lyric",
                    False,
                    lyric.size_bytes,
                    lyric.mtime_ns,
                    group_id,
                    relation.id,
                    item.asset_id,
                )
            )
            linked_lyric_ids.add(lyric_id)
        return tuple(expanded)

    def _backup(self, repository: LibraryRepository, entries: list[BackupEntry]) -> BackupRunResult:
        successes = failures = 0
        messages: list[str] = []
        affected_roots: list[tuple[str, Path]] = []
        history_items: list[BackupHistoryItem] = []
        cancelled = False
        successful_audio_groups: set[str] = set()
        group_audio_entries: dict[str, BackupEntry] = {}
        linked_plans: dict[str, dict[str, object]] = {}
        prepared_payload = self._prepare_backup_inputs(repository)
        current_matches = repository.list_lyrics_matches(current_only=True)
        current_matches_by_id = {match.id: match for match in current_matches}
        referenced_lyrics = {match.lyric_asset_id for match in current_matches if match.lyric_asset_id}
        for index, value in enumerate(prepared_payload):
            if self._cancel.is_set():
                cancelled = True
                if (
                    isinstance(value, BackupInput)
                    and value.kind == "lyric"
                    and value.link_group_id in group_audio_entries
                ):
                    audio_entry = group_audio_entries[value.link_group_id]
                    history_items = [
                        item for item in history_items if item.entry_id != audio_entry.id
                    ]
                    successes -= 1
                    try:
                        entries[:] = self._rollback_incomplete_linked_backup(
                            repository,
                            entries,
                            audio_entry,
                            reason="关联歌词备份前观察到取消",
                        )
                        history_items.append(self._history_item(
                            entry_id=None,
                            asset_id=audio_entry.asset_id,
                            kind="audio",
                            source_path=audio_entry.original_path,
                            backup_path=None,
                            restore_target=None,
                            result="cancelled",
                            message="关联歌词备份前取消，音乐已完整恢复",
                        ))
                    except Exception as rollback_error:
                        failures += 1
                        messages.append(str(rollback_error))
                        history_items.append(self._history_item(
                            entry_id=audio_entry.id,
                            asset_id=audio_entry.asset_id,
                            kind="audio",
                            source_path=audio_entry.original_path,
                            backup_path=audio_entry.backup_path,
                            restore_target=audio_entry.original_path,
                            result="failed",
                            message=str(rollback_error),
                        ))
                for pending in prepared_payload[index:]:
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
            if (
                value.kind == "lyric"
                and value.link_group_id is not None
                and value.link_group_id not in successful_audio_groups
            ):
                failures += 1
                message = f"{value.source_path.name}：关联音乐未成功备份，歌词未处理"
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
            if (
                value.kind == "lyric"
                and value.asset_id in referenced_lyrics
                and value.link_group_id is None
            ):
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
                plan: dict[str, object] | None = None
                if value.link_group_id is not None:
                    plan = linked_plans.get(value.link_group_id)
                    if value.kind == "audio":
                        lyric_input = next(
                            (
                                candidate
                                for candidate in prepared_payload
                                if isinstance(candidate, BackupInput)
                                and candidate.kind == "lyric"
                                and candidate.link_group_id == value.link_group_id
                            ),
                            None,
                        )
                        relation = current_matches_by_id.get(value.lyrics_match_id)
                        if lyric_input is None or relation is None:
                            raise BackupError("关联备份缺少歌词输入或当前关系")
                        plan = self._build_linked_backup_plan(
                            value,
                            lyric_input,
                            relation,
                        )
                        repository.set_setting(PENDING_LINKED_BACKUP_KEY, plan)
                        linked_plans[value.link_group_id] = plan
                    if plan is None:
                        raise BackupError("关联备份计划不存在")
                    planned_member = self._planned_member(plan, value.kind)
                    entry_id = str(planned_member["entry_id"])
                else:
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
                if plan is not None:
                    self._advance_linked_backup_plan(
                        repository,
                        plan,
                        state=f"{value.kind}_moved",
                        kind=value.kind,
                        sha256=moved.sha256,
                    )
                    planned_member = self._planned_member(plan, value.kind)
                    if _path_key(moved.target_path) != _path_key(
                        Path(str(planned_member["backup_path"]))
                    ):
                        raise BackupError("关联备份实际路径与预定路径不一致")
                entry = BackupEntry(
                    id=entry_id,
                    asset_id=value.asset_id,
                    kind=value.kind,
                    original_path=value.source_path,
                    backup_path=moved.target_path,
                    sha256=moved.sha256,
                    created_at=(
                        str(self._planned_member(plan, value.kind)["created_at"])
                        if plan is not None
                        else datetime.now(timezone.utc).isoformat()
                    ),
                    allowed_root=value.allowed_root,
                    link_group_id=value.link_group_id,
                    lyrics_match_id=value.lyrics_match_id,
                    linked_audio_asset_id=value.linked_audio_asset_id,
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
                if plan is not None:
                    if value.kind == "audio":
                        self._advance_linked_backup_plan(
                            repository,
                            plan,
                            state="audio_manifest_saved",
                        )
                    else:
                        try:
                            self._advance_linked_backup_plan(
                                repository,
                                plan,
                                state="group_committed",
                            )
                        except Exception as journal_error:
                            messages.append(
                                "关联备份组已提交，但状态日志更新失败，"
                                f"将在下次启动核对：{journal_error}"
                            )
                successes += 1
                if value.kind == "audio" and value.link_group_id is not None:
                    successful_audio_groups.add(value.link_group_id)
                    group_audio_entries[value.link_group_id] = entry
                elif value.kind == "lyric" and value.link_group_id is not None:
                    try:
                        repository.set_setting(PENDING_LINKED_BACKUP_KEY, [])
                    except Exception as journal_error:
                        messages.append(
                            f"关联备份已完成，但回滚日志清除失败，将在下次启动核对：{journal_error}"
                        )
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
                if (
                    value.kind == "audio"
                    and value.link_group_id is not None
                    and value.link_group_id in linked_plans
                    and value.link_group_id not in group_audio_entries
                ):
                    try:
                        self._recover_pending_linked_backup(repository)
                    except Exception as recovery_error:
                        error = BackupError(
                            f"{error}；关联备份自动恢复失败：{recovery_error}"
                        )
                linked_audio = (
                    group_audio_entries.get(value.link_group_id)
                    if value.kind == "lyric" and value.link_group_id is not None
                    else None
                )
                message = f"{value.source_path.name}：{error}"
                if linked_audio is not None:
                    history_items = [
                        item for item in history_items if item.entry_id != linked_audio.id
                    ]
                    successes -= 1
                    rollback_message = "关联歌词失败，音乐已完整恢复"
                    try:
                        entries[:] = self._rollback_incomplete_linked_backup(
                            repository,
                            entries,
                            linked_audio,
                            reason=message,
                        )
                    except Exception as rollback_error:
                        rollback_message = str(rollback_error)
                    failures += 2
                    message += f"；{rollback_message}"
                    messages.append(message)
                    history_items.append(self._history_item(
                        entry_id=linked_audio.id,
                        asset_id=linked_audio.asset_id,
                        kind="audio",
                        source_path=linked_audio.original_path,
                        backup_path=(
                            linked_audio.backup_path
                            if linked_audio.backup_path.exists()
                            else None
                        ),
                        restore_target=linked_audio.original_path,
                        result="failed",
                        message=rollback_message,
                    ))
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
                    continue
                failures += 1
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
        successful_entry_ids = {
            item.entry_id
            for item in history_items
            if item.result == "success" and item.entry_id is not None
        }
        affected_roots = [
            (entry.kind, entry.allowed_root)
            for entry in entries
            if entry.id in successful_entry_ids
            and entry.allowed_root is not None
        ]
        affected_roots = list(dict.fromkeys(affected_roots))
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
        requested = tuple(str(value) for value in self._payload)
        if len(requested) != len(set(requested)):
            raise BackupError("恢复条目编号重复")
        by_id = {entry.id: entry for entry in entries}
        if any(entry_id not in by_id for entry_id in requested):
            raise BackupError("所选备份条目不存在或已不可恢复")
        selected_ids = set(requested)
        selected_groups = {
            by_id[entry_id].link_group_id
            for entry_id in requested
            if by_id[entry_id].link_group_id is not None
        }
        if not selected_groups:
            return self._restore_legacy(repository, entries)
        for entry in entries:
            if entry.link_group_id in selected_groups:
                selected_ids.add(entry.id)
        ordered = [entry for entry in entries if entry.id in selected_ids]
        units: list[list[BackupEntry]] = []
        seen_groups: set[str] = set()
        for entry in ordered:
            if entry.link_group_id is None:
                units.append([entry])
            elif entry.link_group_id not in seen_groups:
                seen_groups.add(entry.link_group_id)
                group = [value for value in entries if value.link_group_id == entry.link_group_id]
                group.sort(key=lambda value: 0 if value.kind == "audio" else 1)
                if len(group) != 2 or {value.kind for value in group} != {"audio", "lyric"}:
                    raise BackupError("关联备份组损坏，拒绝部分恢复")
                units.append(group)
        current = list(entries)
        successes = failures = 0
        messages: list[str] = []
        history_items: list[BackupHistoryItem] = []
        affected_roots: list[tuple[str, Path]] = []
        cancelled = False
        for unit_index, unit in enumerate(units):
            if self._cancel.is_set():
                cancelled = True
                for pending in units[unit_index:]:
                    for entry in pending:
                        history_items.append(self._history_item(
                            entry_id=entry.id, asset_id=entry.asset_id, kind=entry.kind,
                            source_path=entry.backup_path, backup_path=entry.backup_path,
                            restore_target=entry.original_path, result="cancelled",
                            message="操作已取消，未开始恢复",
                        ))
                break
            moved: list[BackupEntry] = []
            try:
                for entry in unit:
                    self._validate_entry(repository, self._backup_root, entry)
                    if entry.original_path.exists():
                        raise BackupError(f"原路径已存在，禁止覆盖：{entry.original_path.name}")
                    with _locked_directory_chain(self._backup_root, entry.backup_path.parent):
                        if _verified_file_sha256(entry.backup_path) != entry.sha256:
                            raise BackupError("备份文件 SHA-256 与清单不一致，拒绝恢复")
                for entry in unit:
                    result = import_one(
                        entry.backup_path,
                        source_root=entry.backup_path.parent,
                        target_root=entry.original_path.parent,
                    )
                    if (
                        result.status != "success"
                        or result.sha256 != entry.sha256
                        or _path_key(result.target_path) != _path_key(entry.original_path)
                    ):
                        raise BackupError("关联恢复落位结果与清单不一致")
                    moved.append(entry)
                candidate = [value for value in current if value not in unit]
                self._save(repository, candidate)
                current = candidate
                for entry in unit:
                    successes += 1
                    if entry.allowed_root is not None:
                        key = (entry.kind, entry.allowed_root)
                        if key not in affected_roots:
                            affected_roots.append(key)
                    history_items.append(self._history_item(
                        entry_id=entry.id, asset_id=entry.asset_id, kind=entry.kind,
                        source_path=entry.backup_path, backup_path=entry.backup_path,
                        restore_target=entry.original_path, result="success",
                        message="已按关联组恢复到原路径",
                    ))
            except Exception as error:
                rollback_errors: list[str] = []
                for entry in reversed(moved):
                    rollback = import_one(
                        entry.original_path,
                        source_root=entry.original_path.parent,
                        target_root=entry.backup_path.parent,
                    )
                    if (
                        rollback.status != "success"
                        or _path_key(rollback.target_path) != _path_key(entry.backup_path)
                    ):
                        rollback_errors.append(rollback.message)
                failures += len(unit)
                message = str(error).strip() or error.__class__.__name__
                if rollback_errors:
                    message += "；关联组回滚失败：" + "；".join(rollback_errors)
                messages.append(message)
                for entry in unit:
                    history_items.append(self._history_item(
                        entry_id=entry.id, asset_id=entry.asset_id, kind=entry.kind,
                        source_path=entry.backup_path, backup_path=entry.backup_path,
                        restore_target=entry.original_path, result="failed", message=message,
                    ))
        return BackupRunResult(
            "restore", successes, failures, tuple(messages), tuple(affected_roots),
            "cancelled" if cancelled else "completed", None, tuple(history_items),
        )

    def _restore_legacy(self, repository: LibraryRepository, entries: list[BackupEntry]) -> BackupRunResult:
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
        eligible_ids: set[str] = set()
        for entry in entries:
            created = _aware_datetime(entry.created_at, label="备份清单创建时间")
            if entry.restored_at is None and created <= threshold:
                eligible_ids.add(entry.id)
        units: list[list[BackupEntry]] = []
        seen_groups: set[str] = set()
        for entry in entries:
            if entry.link_group_id is None:
                if entry.id in eligible_ids:
                    units.append([entry])
                continue
            if entry.link_group_id in seen_groups:
                continue
            seen_groups.add(entry.link_group_id)
            group = [
                member for member in entries
                if member.link_group_id == entry.link_group_id
            ]
            group.sort(key=lambda member: 0 if member.kind == "audio" else 1)
            if all(member.id in eligible_ids for member in group):
                units.append(group)
        cancelled = False
        for position, unit in enumerate(units):
            if self._cancel.is_set():
                cancelled = True
                for pending_unit in units[position:]:
                    for pending in pending_unit:
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
            tombstones = {
                entry.id: entry.backup_path.parent
                / f".{entry.backup_path.name}.{uuid4().hex}.cleanup"
                for entry in unit
            }
            journal = {
                "version": 2,
                "state": "planned",
                "members": [
                    {
                        "entry": _entry_to_json(entry),
                        "tombstone_path": str(tombstones[entry.id]),
                    }
                    for entry in unit
                ],
                "deleted_entry_ids": [],
            }
            renamed: list[BackupEntry] = []
            relation_id = (
                unit[0].lyrics_match_id
                if len(unit) == 2 and unit[0].link_group_id is not None
                else None
            )
            try:
                for entry in unit:
                    self._validate_entry(repository, self._backup_root, entry)
                    with _locked_directory_chain(
                        self._backup_root,
                        entry.backup_path.parent,
                    ):
                        if _verified_file_sha256(entry.backup_path) != entry.sha256:
                            raise BackupError("备份文件 SHA-256 与清单不一致，拒绝清理")
                repository.set_setting(PENDING_CLEANUP_KEY, journal)
                for entry in unit:
                    os.rename(entry.backup_path, tombstones[entry.id])
                    renamed.append(entry)
                journal["state"] = "tombstoned"
                if relation_id is not None:
                    audio = next(entry for entry in unit if entry.kind == "audio")
                    lyric = next(entry for entry in unit if entry.kind == "lyric")
                    repository.cancel_external_lyrics_match_with_journal(
                        match_id=relation_id,
                        audio_asset_id=audio.asset_id,
                        lyric_asset_id=lyric.asset_id,
                        journal_key=PENDING_CLEANUP_KEY,
                        journal=journal,
                    )
                else:
                    repository.set_setting(PENDING_CLEANUP_KEY, journal)
            except Exception as error:
                for entry in reversed(renamed):
                    tombstone = tombstones[entry.id]
                    if tombstone.exists() and not entry.backup_path.exists():
                        os.rename(tombstone, entry.backup_path)
                try:
                    if relation_id is not None:
                        repository.finalize_external_lyrics_cleanup(
                            match_id=relation_id,
                            journal_key=PENDING_CLEANUP_KEY,
                            restore_relation=True,
                        )
                    else:
                        repository.set_setting(PENDING_CLEANUP_KEY, [])
                except Exception as compensation_error:
                    error = BackupError(f"{error}；清理前补偿失败：{compensation_error}")
                failures += len(unit)
                message = str(error).strip() or error.__class__.__name__
                messages.append(message)
                for entry in unit:
                    history_items.append(self._history_item(
                        entry_id=entry.id,
                        asset_id=entry.asset_id,
                        kind=entry.kind,
                        source_path=entry.backup_path,
                        backup_path=entry.backup_path,
                        restore_target=None,
                        result="failed",
                        message=message,
                    ))
                continue

            deleted: list[BackupEntry] = []
            unlink_error: Exception | None = None
            for entry in unit:
                try:
                    os.unlink(tombstones[entry.id])
                    deleted.append(entry)
                except Exception as error:
                    unlink_error = error
                    break
            remaining = [entry for entry in unit if entry not in deleted]
            for entry in remaining:
                tombstone = tombstones[entry.id]
                if tombstone.exists() and not entry.backup_path.exists():
                    os.rename(tombstone, entry.backup_path)
            if not deleted:
                candidate = list(current)
            else:
                candidate = [entry for entry in current if entry not in unit]
                candidate.extend(
                    replace(
                        entry,
                        linked_entry_id=None,
                        link_group_id=None,
                        lyrics_match_id=None,
                        linked_audio_asset_id=None,
                    )
                    for entry in remaining
                )
            persistence_error: Exception | None = None
            try:
                self._validate_manifest_groups(candidate)
                self._save(repository, candidate)
                if relation_id is not None:
                    repository.finalize_external_lyrics_cleanup(
                        match_id=relation_id,
                        journal_key=PENDING_CLEANUP_KEY,
                        restore_relation=not deleted,
                    )
                else:
                    repository.set_setting(PENDING_CLEANUP_KEY, [])
                current = candidate
            except Exception as error:
                persistence_error = error

            if unlink_error is None:
                successes += len(unit)
                for entry in unit:
                    history_items.append(self._history_item(
                        entry_id=entry.id,
                        asset_id=entry.asset_id,
                        kind=entry.kind,
                        source_path=entry.backup_path,
                        backup_path=entry.backup_path,
                        restore_target=None,
                        result="success",
                        message=(
                            "已永久清理备份文件"
                            if persistence_error is None
                            else f"文件已永久清理，恢复日志待下次核对：{persistence_error}"
                        ),
                    ))
            elif deleted:
                successes += len(deleted)
                failures += len(remaining)
                message = (
                    f"关联组部分清理：已永久删除 {len(deleted)} 项，"
                    f"仍可恢复 {len(remaining)} 项；{unlink_error}"
                )
                if persistence_error is not None:
                    message += f"；恢复日志待下次核对：{persistence_error}"
                messages.append(message)
                for entry in deleted:
                    history_items.append(self._history_item(
                        entry_id=entry.id, asset_id=entry.asset_id, kind=entry.kind,
                        source_path=entry.backup_path, backup_path=entry.backup_path,
                        restore_target=None, result="success",
                        message="已永久清理；关联关系保持取消",
                    ))
                for entry in remaining:
                    history_items.append(self._history_item(
                        entry_id=entry.id, asset_id=entry.asset_id, kind=entry.kind,
                        source_path=entry.backup_path, backup_path=entry.backup_path,
                        restore_target=None, result="failed", message=message,
                    ))
            else:
                failures += len(unit)
                message = f"永久清理未删除任何文件，关联组已补偿：{unlink_error}"
                if persistence_error is not None:
                    message += f"；补偿日志待下次核对：{persistence_error}"
                messages.append(message)
                for entry in unit:
                    history_items.append(self._history_item(
                        entry_id=entry.id, asset_id=entry.asset_id, kind=entry.kind,
                        source_path=entry.backup_path, backup_path=entry.backup_path,
                        restore_target=None, result="failed", message=message,
                    ))
            for entry in deleted:
                try:
                    entry.backup_path.parent.rmdir()
                except OSError:
                    pass
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
