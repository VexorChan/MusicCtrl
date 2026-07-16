"""P5 playlist directory discovery and background shortcut operations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import os
from pathlib import Path
import stat
import threading

from PySide6.QtCore import QObject, QThread, Signal

from database import DatabaseConfig
from repositories import LibraryRepository
from services.file_safety import _is_reparse
from services.windows_shortcuts import (
    ShortcutConflictError,
    create_playlist_directory,
    create_shortcut,
    read_shortcut,
    remove_shortcut,
)


PLAYLIST_ROOT_KEY = "p5.playlist_root"
PLAYLIST_HISTORY_KEY = "p5.operation_history"
_PLAYLIST_HISTORY_LIMIT = 200
_PLAYLIST_ACTIONS = {"create", "add", "remove", "retarget"}
_PLAYLIST_TERMINALS = {"completed", "cancelled", "failed"}
_PLAYLIST_ITEM_RESULTS = {"success", "skipped", "failed", "cancelled"}


@dataclass(frozen=True, slots=True)
class PlaylistAudioInput:
    asset_id: str
    target_path: Path
    audio_root: Path
    file_state: str


@dataclass(frozen=True, slots=True)
class PlaylistRemovalInput:
    shortcut_path: Path
    expected_target: Path


@dataclass(frozen=True, slots=True)
class PlaylistRetargetInput:
    source_path: Path
    target_path: Path
    audio_root: Path


@dataclass(frozen=True, slots=True)
class PlaylistItemResult:
    source_path: Path | None
    target_path: Path | None
    result: str
    message: str


@dataclass(frozen=True, slots=True)
class PlaylistOperationResult:
    playlist_name: str
    success_count: int
    skipped_count: int
    failure_count: int
    messages: tuple[str, ...]
    affected_playlists: tuple[str, ...] = ()
    action: str = "add"
    status: str = "completed"
    created_at: str = ""
    items: tuple[PlaylistItemResult, ...] = ()


def _item_paths(item: object, *, playlist_root: Path, playlist_name: str) -> tuple[Path | None, Path | None]:
    if isinstance(item, PlaylistAudioInput):
        return item.target_path, playlist_root / playlist_name / f"{item.target_path.name}.lnk"
    if isinstance(item, PlaylistRemovalInput):
        return item.shortcut_path, item.expected_target
    if isinstance(item, PlaylistRetargetInput):
        return item.source_path, item.target_path
    return None, None


def _history_to_json(result: PlaylistOperationResult) -> dict[str, object]:
    return {
        "playlist_name": result.playlist_name,
        "success_count": result.success_count,
        "skipped_count": result.skipped_count,
        "failure_count": result.failure_count,
        "messages": list(result.messages),
        "affected_playlists": list(result.affected_playlists),
        "action": result.action,
        "status": result.status,
        "created_at": result.created_at,
        "items": [
            {
                "source_path": None if item.source_path is None else str(item.source_path),
                "target_path": None if item.target_path is None else str(item.target_path),
                "result": item.result,
                "message": item.message,
            }
            for item in result.items
        ],
    }


def _history_from_json(value: object) -> PlaylistOperationResult:
    if not isinstance(value, dict):
        raise ValueError("歌单操作历史格式损坏")
    try:
        playlist_name = value["playlist_name"]
        action = value["action"]
        status = value["status"]
        created_at = value["created_at"]
        success_count = value["success_count"]
        skipped_count = value["skipped_count"]
        failure_count = value["failure_count"]
        messages = value["messages"]
        affected = value["affected_playlists"]
        raw_items = value["items"]
    except KeyError as error:
        raise ValueError("歌单操作历史字段缺失") from error
    if (
        not isinstance(playlist_name, str)
        or not playlist_name
        or action not in _PLAYLIST_ACTIONS
        or status not in _PLAYLIST_TERMINALS
        or not isinstance(created_at, str)
        or not created_at
        or any(isinstance(count, bool) or not isinstance(count, int) or count < 0 for count in (success_count, skipped_count, failure_count))
        or not isinstance(messages, list)
        or not all(isinstance(message, str) for message in messages)
        or not isinstance(affected, list)
        or not all(isinstance(name, str) and name for name in affected)
        or not isinstance(raw_items, list)
    ):
        raise ValueError("歌单操作历史字段损坏")
    try:
        parsed_time = datetime.fromisoformat(created_at)
    except ValueError as error:
        raise ValueError("歌单操作历史时间损坏") from error
    if parsed_time.tzinfo is None or parsed_time.utcoffset() is None:
        raise ValueError("歌单操作历史时间缺少时区")
    items: list[PlaylistItemResult] = []
    for raw_item in raw_items:
        if not isinstance(raw_item, dict):
            raise ValueError("歌单操作历史明细损坏")
        source_value = raw_item.get("source_path")
        target_value = raw_item.get("target_path")
        result = raw_item.get("result")
        message = raw_item.get("message")
        source = None if source_value is None else Path(source_value) if isinstance(source_value, str) else None
        target = None if target_value is None else Path(target_value) if isinstance(target_value, str) else None
        if (
            (source_value is not None and (source is None or not source.is_absolute()))
            or (target_value is not None and (target is None or not target.is_absolute()))
            or result not in _PLAYLIST_ITEM_RESULTS
            or not isinstance(message, str)
        ):
            raise ValueError("歌单操作历史明细损坏")
        items.append(PlaylistItemResult(source, target, str(result), message))
    actual_counts = (
        sum(item.result == "success" for item in items),
        sum(item.result == "skipped" for item in items),
        sum(item.result == "failed" for item in items),
    )
    if actual_counts != (success_count, skipped_count, failure_count):
        raise ValueError("歌单操作历史计数与明细不一致")
    item_results = {item.result for item in items}
    if status == "completed" and "cancelled" in item_results:
        raise ValueError("歌单完成历史混入取消明细")
    if status == "cancelled" and "cancelled" not in item_results:
        raise ValueError("歌单取消历史缺少取消明细")
    if status == "failed" and "failed" not in item_results:
        raise ValueError("歌单失败历史缺少失败明细")
    return PlaylistOperationResult(
        playlist_name,
        success_count,
        skipped_count,
        failure_count,
        tuple(messages),
        tuple(affected),
        str(action),
        str(status),
        created_at,
        tuple(items),
    )


def _load_history(repository: LibraryRepository) -> list[PlaylistOperationResult]:
    setting = repository.get_setting(PLAYLIST_HISTORY_KEY)
    if setting is None:
        return []
    if not isinstance(setting.value, list):
        raise ValueError("歌单操作历史不是列表")
    if len(setting.value) > _PLAYLIST_HISTORY_LIMIT:
        raise ValueError("歌单操作历史超过 200 条，拒绝隐藏或截断异常数据")
    return [_history_from_json(value) for value in setting.value]


def _name_key(value: str) -> str:
    return value.rstrip(" .").casefold()


def _path_key(path: Path) -> str:
    return os.path.normcase(os.path.normpath(os.path.abspath(os.fspath(path))))


def _safe_playlist_directories(root: Path) -> tuple[Path, ...]:
    directories: list[Path] = []
    for entry in os.scandir(root):
        metadata = entry.stat(follow_symlinks=False)
        if entry.is_dir(follow_symlinks=False) and not entry.is_symlink() and not _is_reparse(metadata):
            directories.append(root / entry.name)
    return tuple(sorted(directories, key=lambda path: (path.name.casefold(), path.name)))


def _human_size(size_bytes: int) -> str:
    value = float(size_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{int(value)} B" if unit == "B" else f"{value:.1f}".rstrip("0").rstrip(".") + f" {unit}"
        value /= 1024
    return f"{size_bytes} B"


class PlaylistShortcutWorker(QThread):
    completed = Signal(object)
    cancelled = Signal(object)
    failed = Signal(str)

    def __init__(
        self,
        *,
        playlist_root: Path,
        playlist_name: str,
        add_items: tuple[PlaylistAudioInput, ...] = (),
        remove_items: tuple[PlaylistRemovalInput, ...] = (),
        retarget_items: tuple[PlaylistRetargetInput, ...] = (),
        parent=None,
    ) -> None:
        super().__init__(parent)
        if sum(bool(value) for value in (add_items, remove_items, retarget_items)) != 1:
            raise ValueError("必须且只能选择一种歌单操作")
        self._playlist_root = playlist_root
        self._playlist_name = playlist_name
        self._add_items = add_items
        self._remove_items = remove_items
        self._retarget_items = retarget_items
        self._cancel = threading.Event()
        self._action = "retarget" if retarget_items else "add" if add_items else "remove"
        self._created_at = datetime.now(timezone.utc).isoformat()

    def request_cancel(self) -> None:
        self._cancel.set()
        self.requestInterruption()

    def run(self) -> None:
        success = skipped = failures = 0
        messages: list[str] = []
        affected: set[str] = set()
        details: list[PlaylistItemResult] = []

        def build_result(status: str) -> PlaylistOperationResult:
            return PlaylistOperationResult(
                self._playlist_name,
                success,
                skipped,
                failures,
                tuple(messages),
                tuple(sorted(affected)),
                self._action,
                status,
                self._created_at,
                tuple(details),
            )

        try:
            if self._retarget_items:
                folder = None
            elif self._add_items:
                folder = create_playlist_directory(
                    playlist_root=self._playlist_root,
                    name=self._playlist_name,
                )
            else:
                matches = [
                    entry
                    for entry in os.scandir(self._playlist_root)
                    if _name_key(entry.name) == _name_key(self._playlist_name)
                    and entry.is_dir(follow_symlinks=False)
                    and not entry.is_symlink()
                    and not _is_reparse(entry.stat(follow_symlinks=False))
                ]
                if len(matches) != 1:
                    raise ValueError("要移除快捷方式的歌单不存在或不安全")
                folder = self._playlist_root / matches[0].name
            items = self._add_items or self._remove_items or self._retarget_items
            for index, item in enumerate(items):
                if self._cancel.is_set():
                    for remaining in items[index:]:
                        source_path, target_path = _item_paths(
                            remaining,
                            playlist_root=self._playlist_root,
                            playlist_name=self._playlist_name,
                        )
                        details.append(
                            PlaylistItemResult(
                                source_path,
                                target_path,
                                "cancelled",
                                "未执行：操作已取消",
                            )
                        )
                    self.cancelled.emit(build_result("cancelled"))
                    return
                source_path, target_path = _item_paths(
                    item,
                    playlist_root=self._playlist_root,
                    playlist_name=self._playlist_name,
                )
                try:
                    if isinstance(item, PlaylistRetargetInput):
                        updated = 0
                        for playlist in _safe_playlist_directories(self._playlist_root):
                            for shortcut_path in sorted(playlist.glob("*.lnk")):
                                try:
                                    info = read_shortcut(shortcut_path, playlist_root=self._playlist_root)
                                except Exception:
                                    continue
                                if _path_key(info.target_path) != _path_key(item.source_path):
                                    continue
                                destination = playlist / f"{item.target_path.name}.lnk"
                                create_shortcut(
                                    target_path=item.target_path,
                                    audio_root=item.audio_root,
                                    shortcut_path=destination,
                                    playlist_root=self._playlist_root,
                                )
                                try:
                                    remove_shortcut(
                                        shortcut_path=shortcut_path,
                                        playlist_root=self._playlist_root,
                                        expected_target=item.source_path,
                                    )
                                except Exception:
                                    remove_shortcut(
                                        shortcut_path=destination,
                                        playlist_root=self._playlist_root,
                                        expected_target=item.target_path,
                                    )
                                    raise
                                affected.add(playlist.name)
                                updated += 1
                        if updated == 0:
                            skipped += 1
                            message = f"未发现引用：{item.source_path.name}"
                            messages.append(message)
                            details.append(
                                PlaylistItemResult(source_path, target_path, "skipped", message)
                            )
                            continue
                    elif isinstance(item, PlaylistAudioInput):
                        if item.file_state != "active":
                            raise ValueError("只允许添加 active 音频")
                        create_shortcut(
                            target_path=item.target_path,
                            audio_root=item.audio_root,
                            shortcut_path=folder / f"{item.target_path.name}.lnk",
                            playlist_root=self._playlist_root,
                        )
                    else:
                        remove_shortcut(
                            shortcut_path=item.shortcut_path,
                            playlist_root=self._playlist_root,
                            expected_target=item.expected_target,
                        )
                except ShortcutConflictError:
                    skipped += 1
                    item_path = (
                        item.target_path
                        if isinstance(item, (PlaylistAudioInput, PlaylistRetargetInput))
                        else item.shortcut_path
                    )
                    message = f"已跳过重复项：{item_path.name}"
                    messages.append(message)
                    details.append(PlaylistItemResult(source_path, target_path, "skipped", message))
                except Exception as error:
                    failures += 1
                    message = str(error).strip() or error.__class__.__name__
                    messages.append(message)
                    details.append(PlaylistItemResult(source_path, target_path, "failed", message))
                else:
                    success += 1
                    details.append(PlaylistItemResult(source_path, target_path, "success", "已完成"))
            self.completed.emit(build_result("completed"))
        except Exception as error:
            self.failed.emit(str(error).strip() or error.__class__.__name__)


class PlaylistController(QObject):
    playlists_changed = Signal(object)
    playlist_changed = Signal(str, object)
    completed = Signal(object)
    cancelled = Signal(object)
    failed = Signal(str)
    warning = Signal(str)
    running_changed = Signal(bool)

    def __init__(self, database_config: DatabaseConfig, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._database_config = database_config
        self._worker: PlaylistShortcutWorker | None = None
        self._terminal: tuple[str, object] | None = None
        self._owner_thread_id = threading.get_ident()
        self._active_action = ""
        self._active_playlist_name = ""
        self._active_created_at = ""
        self._active_items: tuple[PlaylistItemResult, ...] = ()

    @property
    def running(self) -> bool:
        return self._worker is not None

    def _require_owner_thread(self) -> None:
        if threading.get_ident() != self._owner_thread_id:
            raise RuntimeError("歌单历史只能在创建 controller 的线程读取")

    def list_history(self) -> tuple[PlaylistOperationResult, ...]:
        self._require_owner_thread()
        with LibraryRepository(self._database_config) as repository:
            history = _load_history(repository)
        return tuple(reversed(history))

    def _append_history(self, result: PlaylistOperationResult) -> None:
        with LibraryRepository(self._database_config) as repository:
            history = _load_history(repository)
            history.append(result)
            repository.set_setting(
                PLAYLIST_HISTORY_KEY,
                [_history_to_json(item) for item in history[-_PLAYLIST_HISTORY_LIMIT:]],
            )

    def remembered_root(self) -> Path | None:
        try:
            with LibraryRepository(self._database_config) as repository:
                setting = repository.get_setting(PLAYLIST_ROOT_KEY)
        except Exception as error:
            self.failed.emit(f"无法读取歌单目录设置：{error}")
            return None
        if setting is None or not isinstance(setting.value, str):
            return None
        path = Path(setting.value)
        return path if path.is_absolute() else None

    def set_root(self, root: Path) -> None:
        if not isinstance(root, Path) or not root.is_absolute():
            raise ValueError("歌单根必须是绝对 Path")
        metadata = os.lstat(root)
        if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode) or _is_reparse(metadata):
            raise ValueError("歌单根不能是链接或重解析点")
        with LibraryRepository(self._database_config) as repository:
            repository.set_setting(PLAYLIST_ROOT_KEY, str(root))
        self.playlists_changed.emit(self.list_playlists())

    def _require_root(self) -> Path:
        root = self.remembered_root()
        if root is None:
            raise ValueError("请先选择歌单根目录")
        return root

    def list_playlists(self) -> tuple[str, ...]:
        root = self.remembered_root()
        if root is None:
            return ()
        names: list[str] = []
        for entry in os.scandir(root):
            metadata = entry.stat(follow_symlinks=False)
            if entry.is_dir(follow_symlinks=False) and not entry.is_symlink() and not _is_reparse(metadata):
                names.append(entry.name)
        return tuple(sorted(names, key=lambda name: (name.casefold(), name)))

    def create_playlist(self, name: str) -> str:
        folder = create_playlist_directory(playlist_root=self._require_root(), name=name)
        result = PlaylistOperationResult(
            folder.name,
            1,
            0,
            0,
            (),
            (folder.name,),
            "create",
            "completed",
            datetime.now(timezone.utc).isoformat(),
            (PlaylistItemResult(None, folder, "success", "已创建歌单"),),
        )
        try:
            self._append_history(result)
        except Exception as error:
            self.warning.emit(f"歌单已创建，但操作历史保存失败：{error}")
        self.playlists_changed.emit(self.list_playlists())
        return folder.name

    def load_playlist(self, name: str) -> tuple[dict[str, object], ...]:
        root = self._require_root()
        matching = [item for item in self.list_playlists() if _name_key(item) == _name_key(name)]
        if len(matching) != 1:
            raise ValueError("歌单不存在或名称不唯一")
        folder = root / matching[0]
        with LibraryRepository(self._database_config) as repository:
            assets = repository.list_assets(kind="audio")
        by_path = {
            os.path.normcase(os.path.normpath(os.fspath(asset.canonical_path))): asset
            for asset in assets
        }
        rows: list[dict[str, object]] = []
        for path in sorted(folder.glob("*.lnk"), key=lambda item: (item.name.casefold(), item.name)):
            try:
                info = read_shortcut(path, playlist_root=root)
                asset = by_path.get(os.path.normcase(os.path.normpath(os.fspath(info.target_path))))
            except Exception as error:
                rows.append(
                    {
                        "_shortcut_path": path,
                        "_target_path": None,
                        "title": path.stem,
                        "artist": "待修复",
                        "duration": "—",
                        "format": "LNK",
                        "size": "—",
                        "status": f"损坏：{error}",
                    }
                )
                continue
            if asset is None:
                status = "目标未索引"
                title, artist = info.target_path.stem, "待识别"
                size = "—"
                extension = info.target_path.suffix.lstrip(".").upper()
            else:
                stem = Path(asset.file_name).stem
                title, artist = (stem.rsplit("-", 1) + ["待识别"])[:2] if "-" in stem else (stem, "待识别")
                status = "正常" if asset.file_state == "active" else asset.file_state
                size = _human_size(asset.size_bytes)
                extension = asset.extension.lstrip(".").upper()
            rows.append(
                {
                    "_shortcut_path": path,
                    "_target_path": info.target_path,
                    "title": title.strip(),
                    "artist": artist.strip(),
                    "duration": "—",
                    "format": extension,
                    "size": size,
                    "status": status,
                }
            )
        return tuple(rows)

    def start_add(self, playlist_name: str, items: tuple[PlaylistAudioInput, ...]) -> None:
        self._start_worker(playlist_name, add_items=items)

    def start_remove(self, playlist_name: str, items: tuple[PlaylistRemovalInput, ...]) -> None:
        self._start_worker(playlist_name, remove_items=items)

    def start_retarget(self, items: tuple[PlaylistRetargetInput, ...]) -> None:
        self._start_worker("受管歌单", retarget_items=items)

    def _start_worker(self, playlist_name: str, **items) -> None:
        if self.running:
            raise RuntimeError("歌单操作已经在运行")
        payload = tuple(
            items.get("add_items")
            or items.get("remove_items")
            or items.get("retarget_items")
            or ()
        )
        if not payload:
            raise ValueError("至少选择一个歌单项")
        worker = PlaylistShortcutWorker(
            playlist_root=self._require_root(),
            playlist_name=playlist_name,
            **items,
        )
        worker.completed.connect(lambda result: self._cache("completed", result))
        worker.cancelled.connect(lambda result: self._cache("cancelled", result))
        worker.failed.connect(lambda message: self._cache("failed", message))
        worker.finished.connect(self._finished)
        self._worker = worker
        self._terminal = None
        self._active_action = worker._action
        self._active_playlist_name = playlist_name
        self._active_created_at = worker._created_at
        self._active_items = tuple(
            PlaylistItemResult(
                *_item_paths(
                    item,
                    playlist_root=worker._playlist_root,
                    playlist_name=playlist_name,
                ),
                "failed",
                "",
            )
            for item in payload
        )
        self.running_changed.emit(True)
        worker.start()

    def request_cancel(self) -> None:
        if self._worker is not None:
            self._worker.request_cancel()

    def _cache(self, kind: str, payload: object) -> None:
        if self._terminal is None:
            self._terminal = (kind, payload)

    def _finished(self) -> None:
        worker = self._worker
        if worker is None:
            return
        kind, payload = self._terminal or ("failed", "歌单线程结束但没有终态")
        if isinstance(payload, PlaylistOperationResult):
            history_result = payload
        else:
            message = str(payload)
            failed_items = tuple(
                PlaylistItemResult(
                    item.source_path,
                    item.target_path,
                    "failed",
                    message,
                )
                for item in self._active_items
            )
            history_result = PlaylistOperationResult(
                self._active_playlist_name or "未知歌单",
                0,
                0,
                len(failed_items) or 1,
                (message,),
                (),
                self._active_action or "add",
                "failed",
                self._active_created_at or datetime.now(timezone.utc).isoformat(),
                failed_items,
            )
        try:
            self._append_history(history_result)
        except Exception as error:
            self.warning.emit(f"歌单操作已结束，但历史保存失败：{error}")
        if isinstance(payload, PlaylistOperationResult):
            try:
                names = payload.affected_playlists
                if not names and payload.playlist_name != "受管歌单":
                    names = (payload.playlist_name,)
                for name in names:
                    self.playlist_changed.emit(name, self.load_playlist(name))
            except Exception as error:
                self.failed.emit(f"操作完成但歌单刷新失败：{error}")
                kind = "failed"
                payload = str(error)
        if kind == "completed":
            self.completed.emit(payload)
        elif kind == "cancelled":
            self.cancelled.emit(payload)
        else:
            self.failed.emit(str(payload))
        self._worker = None
        self._terminal = None
        self._active_action = ""
        self._active_playlist_name = ""
        self._active_created_at = ""
        self._active_items = ()
        worker.deleteLater()
        self.running_changed.emit(False)
