"""P5 playlist directory discovery and background shortcut operations."""

from __future__ import annotations

from dataclasses import dataclass
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
class PlaylistOperationResult:
    playlist_name: str
    success_count: int
    skipped_count: int
    failure_count: int
    messages: tuple[str, ...]
    affected_playlists: tuple[str, ...] = ()


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

    def request_cancel(self) -> None:
        self._cancel.set()
        self.requestInterruption()

    def run(self) -> None:
        success = skipped = failures = 0
        messages: list[str] = []
        affected: set[str] = set()
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
            for item in items:
                if self._cancel.is_set():
                    result = PlaylistOperationResult(
                        self._playlist_name,
                        success,
                        skipped,
                        failures,
                        tuple(messages),
                        tuple(sorted(affected)),
                    )
                    self.cancelled.emit(result)
                    return
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
                            messages.append(f"未发现引用：{item.source_path.name}")
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
                    messages.append(f"已跳过重复项：{item_path.name}")
                except Exception as error:
                    failures += 1
                    messages.append(str(error).strip() or error.__class__.__name__)
                else:
                    success += 1
            self.completed.emit(
                PlaylistOperationResult(
                    self._playlist_name,
                    success,
                    skipped,
                    failures,
                    tuple(messages),
                    tuple(sorted(affected)),
                )
            )
        except Exception as error:
            self.failed.emit(str(error).strip() or error.__class__.__name__)


class PlaylistController(QObject):
    playlists_changed = Signal(object)
    playlist_changed = Signal(str, object)
    completed = Signal(object)
    cancelled = Signal(object)
    failed = Signal(str)
    running_changed = Signal(bool)

    def __init__(self, database_config: DatabaseConfig, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._database_config = database_config
        self._worker: PlaylistShortcutWorker | None = None
        self._terminal: tuple[str, object] | None = None

    @property
    def running(self) -> bool:
        return self._worker is not None

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
        worker.deleteLater()
        self.running_changed.emit(False)
