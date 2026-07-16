"""Read-only aggregation for the five user-visible operation history sources."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable


_CATEGORY_ORDER = {"import": 0, "rename": 1, "delete": 2, "playlist": 3, "lyrics": 4}


@dataclass(frozen=True, slots=True)
class HistoryDetail:
    file_name: str
    source_path: Path | None
    target_path: Path | None
    result: str
    reason: str
    completed_at: str


@dataclass(frozen=True, slots=True)
class HistoryRecord:
    id: str
    category: str
    action: str
    created_at: str
    status: str
    success_count: int
    failure_count: int
    items: tuple[HistoryDetail, ...]
    restore_ids: tuple[str, ...] = ()
    undoable: bool = False


@dataclass(frozen=True, slots=True)
class HistorySnapshot:
    records: tuple[HistoryRecord, ...]
    warnings: tuple[str, ...] = ()


def _value(record: object, name: str, default: object = None) -> object:
    if isinstance(record, dict):
        return record.get(name, default)
    return getattr(record, name, default)


def _required_text(record: object, name: str) -> str:
    value = _value(record, name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"历史字段 {name} 缺失或损坏")
    return value


def _optional_path(value: object) -> Path | None:
    if value is None or value == "":
        return None
    path = value if isinstance(value, Path) else Path(value) if isinstance(value, str) else None
    if path is None or not path.is_absolute():
        raise ValueError("历史包含无效路径")
    return path


def _utc_text(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("历史时间缺失")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("历史时间损坏") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("历史时间缺少时区")
    return parsed.astimezone(timezone.utc).isoformat()


def _sort_key(record: HistoryRecord) -> tuple[datetime, int, str]:
    return (
        datetime.fromisoformat(record.created_at),
        -_CATEGORY_ORDER[record.category],
        record.id,
    )


def _item_name(source: Path | None, target: Path | None, fallback: str = "") -> str:
    path = target or source
    return path.name if path is not None else fallback


class HistoryService:
    """Merge controller-owned histories without exposing SQLite to the UI."""

    def __init__(
        self,
        *,
        import_controller: object | None = None,
        rename_controller: object | None = None,
        backup_controller: object | None = None,
        playlist_controller: object | None = None,
        lyrics_controller: object | None = None,
    ) -> None:
        self._import_controller = import_controller
        self._rename_controller = rename_controller
        self._backup_controller = backup_controller
        self._playlist_controller = playlist_controller
        self._lyrics_controller = lyrics_controller

    def load(self) -> HistorySnapshot:
        records: list[HistoryRecord] = []
        warnings: list[str] = []
        sources: tuple[tuple[str, object | None, Callable[[object], Iterable[HistoryRecord]]], ...] = (
            ("导入", self._import_controller, self._imports),
            ("重命名", self._rename_controller, self._renames),
            ("删除", self._backup_controller, self._deletes),
            ("歌单", self._playlist_controller, self._playlists),
            ("歌词匹配", self._lyrics_controller, self._lyrics),
        )
        for label, controller, loader in sources:
            if controller is None:
                continue
            try:
                if label == "删除":
                    source_records, source_warnings = self._deletes(controller)
                else:
                    source_records = tuple(loader(controller))
                    source_warnings = ()
            except Exception as error:
                warnings.append(f"{label}历史读取失败：{error}")
            else:
                records.extend(source_records)
                warnings.extend(source_warnings)

        imports = [record for record in records if record.category == "import" and record.undoable]
        if imports:
            # SafeImportController appends completed batches and its undo API
            # targets the last still-eligible entry from that same source order.
            newest = imports[-1]
            records = [
                record if record.category != "import" or record.id == newest.id else replace(record, undoable=False)
                for record in records
            ]
        records.sort(key=_sort_key, reverse=True)
        return HistorySnapshot(tuple(records), tuple(warnings))

    @staticmethod
    def _imports(controller: object) -> Iterable[HistoryRecord]:
        raw_records = controller.list_history()
        for raw in raw_records:
            identifier = _required_text(raw, "id")
            created_at = _utc_text(_value(raw, "created_at"))
            mode = _required_text(raw, "mode")
            if mode not in {"audio", "lyrics"}:
                raise ValueError("导入历史模式损坏")
            raw_items = _value(raw, "items")
            if not isinstance(raw_items, (list, tuple)):
                raise ValueError("导入历史明细损坏")
            details: list[HistoryDetail] = []
            for item in raw_items:
                source = _optional_path(_value(item, "source_path"))
                target = _optional_path(_value(item, "target_path"))
                status = _required_text(item, "status")
                message = str(_value(item, "message", ""))
                details.append(HistoryDetail(_item_name(source, target), source, target, status, message, created_at))
            success = sum(item.result == "success" for item in details)
            failures = sum(item.result in {"failed", "conflict"} for item in details)
            complete = _value(raw, "complete") is True
            undone = _value(raw, "undone_at") is not None
            status = "已撤销" if undone else "成功" if complete else "部分成功" if success else "失败"
            yield HistoryRecord(
                identifier,
                "import",
                "导入音乐" if mode == "audio" else "导入歌词",
                created_at,
                status,
                success,
                failures,
                tuple(details),
                undoable=complete and not undone,
            )

    @staticmethod
    def _renames(controller: object) -> Iterable[HistoryRecord]:
        for operation, raw_items in controller.list_history():
            identifier = _required_text(operation, "id")
            created_at = _utc_text(_value(operation, "created_at"))
            details: list[HistoryDetail] = []
            for item in raw_items:
                source = _optional_path(_value(item, "source_path"))
                target = _optional_path(_value(item, "target_path"))
                result = _required_text(item, "result")
                reason = str(_value(item, "error_message", "") or "")
                completed = _value(item, "completed_at")
                details.append(
                    HistoryDetail(
                        _item_name(source, target),
                        source,
                        target,
                        result,
                        reason,
                        created_at if completed is None else _utc_text(completed),
                    )
                )
            yield HistoryRecord(
                identifier,
                "rename",
                "重命名",
                created_at,
                _required_text(operation, "status"),
                int(_value(operation, "success_count", 0)),
                int(_value(operation, "failure_count", 0)),
                tuple(details),
            )

    def _deletes(
        self, controller: object
    ) -> tuple[tuple[HistoryRecord, ...], tuple[str, ...]]:
        history_loader = getattr(controller, "list_operation_history", None)
        if not callable(history_loader):
            history_loader = getattr(controller, "list_history", None)
        if callable(history_loader):
            # The permanent audit is authoritative and must remain readable even
            # when the mutable restore manifest is damaged.  The manifest can
            # only reduce restore eligibility; it can never create it.
            operations = tuple(history_loader())
            warnings: tuple[str, ...] = ()
            try:
                current_entries = {
                    str(_value(entry, "id")): entry
                    for entry in controller.list_entries()
                    if _value(entry, "restored_at") is None
                }
            except Exception as error:
                current_entries = {}
                warnings = (f"删除历史恢复资格读取失败：{error}",)
            records: list[HistoryRecord] = []
            for operation in operations:
                identifier = _required_text(operation, "id")
                action = _required_text(operation, "action")
                if action not in {"backup", "restore", "cleanup"}:
                    raise ValueError("删除历史动作损坏")
                created_at = _utc_text(_value(operation, "created_at"))
                details: list[HistoryDetail] = []
                restore_ids: list[str] = []
                raw_items = _value(operation, "items", ())
                if not isinstance(raw_items, (list, tuple)):
                    raise ValueError("删除历史明细损坏")
                for item in raw_items:
                    source = _optional_path(_value(item, "source_path"))
                    backup = _optional_path(_value(item, "backup_path"))
                    restore_target = _optional_path(_value(item, "restore_target"))
                    result = _required_text(item, "result")
                    entry_id = str(_value(item, "entry_id", ""))
                    target = restore_target if action == "restore" else backup
                    completed = _value(item, "completed_at")
                    details.append(
                        HistoryDetail(
                            _item_name(source, target, entry_id),
                            source,
                            target,
                            result,
                            str(_value(item, "message", "")),
                            created_at if completed is None else _utc_text(completed),
                        )
                    )
                    if action == "backup" and result == "success" and entry_id in current_entries:
                        restore_ids.append(entry_id)
                action_text = {"backup": "删除到备份", "restore": "恢复备份", "cleanup": "永久清理"}[action]
                records.append(
                    HistoryRecord(
                        identifier,
                        "delete",
                        action_text,
                        created_at,
                        _required_text(operation, "status"),
                        int(_value(operation, "success_count", 0)),
                        int(_value(operation, "failure_count", 0)),
                        tuple(details),
                        tuple(restore_ids),
                    )
                )
            return tuple(records), warnings

        # Compatibility for older controller doubles: the manifest is not mixed
        # with permanent history when the public audit API exists.
        records = []
        for entry in controller.list_entries():
            identifier = _required_text(entry, "id")
            created_at = _utc_text(_value(entry, "created_at"))
            original = _optional_path(_value(entry, "original_path"))
            backup = _optional_path(_value(entry, "backup_path"))
            restored = _value(entry, "restored_at") is not None
            detail = HistoryDetail(
                _item_name(original, backup, identifier),
                original,
                backup,
                "restored" if restored else "success",
                "",
                created_at,
            )
            records.append(
                HistoryRecord(
                    f"backup:{identifier}",
                    "delete",
                    "恢复备份" if restored else "删除到备份",
                    created_at,
                    "已恢复" if restored else "成功",
                    1,
                    0,
                    (detail,),
                    () if restored else (identifier,),
                )
            )
        return tuple(records), ()

    @staticmethod
    def _playlists(controller: object) -> Iterable[HistoryRecord]:
        for index, operation in enumerate(controller.list_history()):
            created_at = _utc_text(_value(operation, "created_at"))
            action = _required_text(operation, "action")
            playlist_name = _required_text(operation, "playlist_name")
            details: list[HistoryDetail] = []
            for item in _value(operation, "items", ()):
                source = _optional_path(_value(item, "source_path"))
                target = _optional_path(_value(item, "target_path"))
                details.append(
                    HistoryDetail(
                        _item_name(source, target, playlist_name),
                        source,
                        target,
                        _required_text(item, "result"),
                        str(_value(item, "message", "")),
                        created_at,
                    )
                )
            action_text = {"create": "创建歌单", "add": "添加到歌单", "remove": "从歌单移除", "retarget": "更新快捷方式"}.get(action, action)
            identifier = f"playlist:{created_at}:{action}:{playlist_name}:{index}"
            yield HistoryRecord(
                identifier,
                "playlist",
                action_text,
                created_at,
                _required_text(operation, "status"),
                int(_value(operation, "success_count", 0)),
                int(_value(operation, "failure_count", 0)),
                tuple(details),
            )

    @staticmethod
    def _lyrics(controller: object) -> Iterable[HistoryRecord]:
        for raw in controller.list_history():
            identifier = _required_text(raw, "id")
            created_at = _utc_text(_value(raw, "created_at"))
            audio = _optional_path(_value(raw, "audio_path"))
            lyric = _optional_path(_value(raw, "lyric_path"))
            state = _required_text(raw, "state")
            detail = HistoryDetail(
                _item_name(audio, lyric, identifier),
                audio,
                lyric,
                state,
                f"来源：{_value(raw, 'source_kind', '')}；方式：{_value(raw, 'method', '')}",
                _utc_text(_value(raw, "updated_at", created_at)),
            )
            yield HistoryRecord(
                identifier,
                "lyrics",
                "取消歌词匹配" if state == "cancelled" else "歌词匹配",
                created_at,
                state,
                0 if state == "cancelled" else 1,
                1 if state == "cancelled" else 0,
                (detail,),
            )
