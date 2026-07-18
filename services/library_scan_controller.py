"""UI-thread coordinator for the P1 read-only scan worker."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
import os
from pathlib import Path
import stat

from PySide6.QtCore import QObject, Signal, Slot

from database import DatabaseConfig
from repositories import AssetRecord, LibraryRepository
from services.file_safety import _is_reparse, _locked_directory_chain, _within_root
from services.scan_worker import ReadOnlyScanWorker


LAST_SUCCESSFUL_ROOT_KEY = "p1.last_successful_audio_root"


@dataclass(frozen=True, slots=True)
class AudioAssetSnapshot:
    """Untrusted UI snapshot used only to identify the selected indexed asset."""

    asset_id: str
    canonical_path: Path
    size_bytes: int
    mtime_ns: int | None
    allowed_root: Path | None = None


@dataclass(frozen=True, slots=True)
class RevalidatedAudioRecord:
    """Repository and filesystem facts revalidated at one point in time."""

    asset_id: str
    canonical_path: Path
    allowed_root: Path
    size_bytes: int
    mtime_ns: int | None
    file_name: str
    extension: str

    @property
    def id(self) -> str:
        """Match the read-only AssetRecord identifier interface used by workers."""

        return self.asset_id


def _path_key(path: Path) -> str:
    return os.path.normcase(os.path.normpath(os.path.abspath(os.fspath(path))))


def revalidate_audio_snapshots(
    repository: LibraryRepository,
    snapshots: Iterable[AudioAssetSnapshot],
) -> tuple[RevalidatedAudioRecord, ...]:
    """Fail closed unless every snapshot still matches active indexed disk facts."""

    requested = tuple(snapshots)
    if not requested:
        raise ValueError("请至少选择一首音乐")
    asset_ids: set[str] = set()
    path_keys: set[str] = set()
    for snapshot in requested:
        if not isinstance(snapshot, AudioAssetSnapshot):
            raise TypeError("音乐重验输入必须使用 AudioAssetSnapshot")
        if not snapshot.asset_id.strip():
            raise ValueError("音乐索引编号不能为空")
        if snapshot.asset_id in asset_ids:
            raise ValueError("所选音乐包含重复索引编号")
        asset_ids.add(snapshot.asset_id)
        if not isinstance(snapshot.canonical_path, Path) or not snapshot.canonical_path.is_absolute():
            raise ValueError("音乐路径必须是绝对 Path")
        path_key = _path_key(snapshot.canonical_path)
        if path_key in path_keys:
            raise ValueError("所选音乐包含重复路径")
        path_keys.add(path_key)
        if (
            isinstance(snapshot.size_bytes, bool)
            or not isinstance(snapshot.size_bytes, int)
            or snapshot.size_bytes < 0
        ):
            raise ValueError("音乐大小快照无效")
        if snapshot.mtime_ns is not None and (
            isinstance(snapshot.mtime_ns, bool)
            or not isinstance(snapshot.mtime_ns, int)
            or snapshot.mtime_ns < 0
        ):
            raise ValueError("音乐修改时间快照无效")
        if snapshot.allowed_root is not None and (
            not isinstance(snapshot.allowed_root, Path)
            or not snapshot.allowed_root.is_absolute()
        ):
            raise ValueError("音乐扫描来源快照无效")

    assets = []
    for snapshot in requested:
        asset = repository.get_asset_by_id(snapshot.asset_id)
        if asset is None or asset.kind != "audio":
            raise ValueError("所选音乐索引不存在或不是音频")
        if asset.file_state != "active":
            raise ValueError(f"只允许操作 active 音频：{asset.file_name}")
        if (
            _path_key(asset.canonical_path) != _path_key(snapshot.canonical_path)
            or asset.size_bytes != snapshot.size_bytes
            or asset.mtime_ns != snapshot.mtime_ns
        ):
            raise ValueError(f"音乐索引已变化，请刷新列表后重试：{asset.file_name}")
        assets.append(asset)

    roots = repository.latest_completed_audio_roots(asset.id for asset in assets)
    results: list[RevalidatedAudioRecord] = []
    for snapshot, asset in zip(requested, assets, strict=True):
        allowed_root = roots.get(asset.id)
        if (
            not isinstance(allowed_root, Path)
            or not allowed_root.is_absolute()
            or not _within_root(asset.canonical_path, allowed_root)
        ):
            raise ValueError(f"音乐缺少可信的已完成扫描来源：{asset.file_name}")
        if (
            snapshot.allowed_root is not None
            and _path_key(snapshot.allowed_root) != _path_key(allowed_root)
        ):
            raise ValueError(f"音乐扫描来源已变化，请刷新列表后重试：{asset.file_name}")
        try:
            with _locked_directory_chain(allowed_root, asset.canonical_path.parent):
                metadata = os.lstat(asset.canonical_path)
        except OSError as error:
            raise ValueError(f"音乐文件不存在或无法访问：{asset.file_name}") from error
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or _is_reparse(metadata)
        ):
            raise ValueError(f"音乐路径不是安全普通文件：{asset.file_name}")
        if metadata.st_size != asset.size_bytes or metadata.st_mtime_ns != asset.mtime_ns:
            raise ValueError(f"音乐文件已在外部变化，请重新扫描：{asset.file_name}")
        results.append(
            RevalidatedAudioRecord(
                asset.id,
                asset.canonical_path,
                allowed_root,
                asset.size_bytes,
                asset.mtime_ns,
                asset.file_name,
                asset.extension,
            )
        )
    return tuple(results)


def _human_size(size_bytes: int) -> str:
    value = float(size_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            if unit == "B":
                return f"{int(value)} B"
            text = f"{value:.1f}".rstrip("0").rstrip(".")
            return f"{text} {unit}"
        value /= 1024
    return f"{size_bytes} B"


def asset_to_music_record(
    asset: AssetRecord,
    *,
    allowed_root: Path | None = None,
) -> dict[str, object]:
    stem = Path(asset.file_name).stem
    title = stem
    artist = "待识别"
    if "-" in stem:
        parsed_title, parsed_artist = (part.strip() for part in stem.rsplit("-", 1))
        if parsed_title and parsed_artist:
            title = parsed_title
            artist = parsed_artist
    status = {
        "active": "未检查",
        "missing": "文件缺失",
        "external_changed": "外部变化",
    }.get(asset.file_state, "未检查")
    return {
        "_asset_id": asset.id,
        "_canonical_path": asset.canonical_path,
        "_file_state": asset.file_state,
        "_size_bytes": asset.size_bytes,
        "_mtime_ns": asset.mtime_ns,
        "_allowed_root": allowed_root,
        "title": title,
        "artist": artist,
        "duration": "—",
        "format": asset.extension.lstrip(".").upper(),
        "size": _human_size(asset.size_bytes),
        "status": status,
    }


class LibraryScanController(QObject):
    batch_committed = Signal(object)
    library_changed = Signal(object)
    completed = Signal(int)
    cancelled = Signal(int)
    failed = Signal(str)
    warning = Signal(str)
    running_changed = Signal(bool)

    def __init__(self, database_config: DatabaseConfig, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._database_config = database_config
        self._worker: ReadOnlyScanWorker | None = None
        self._terminal: tuple[str, int | str] | None = None
        self._active_root: Path | None = None

    @property
    def running(self) -> bool:
        return self._worker is not None

    def _open_repository(self) -> LibraryRepository:
        return LibraryRepository(self._database_config)

    def load_library(self) -> tuple[dict[str, object], ...]:
        repository = self._open_repository()
        try:
            assets = repository.list_assets(kind="audio")
            roots = repository.latest_completed_audio_roots(asset.id for asset in assets)
            return tuple(
                asset_to_music_record(asset, allowed_root=roots.get(asset.id))
                for asset in assets
            )
        finally:
            repository.close()

    def revalidate_audio_records(
        self,
        snapshots: Iterable[AudioAssetSnapshot],
    ) -> tuple[RevalidatedAudioRecord, ...]:
        """Revalidate a copied UI selection using one short-lived repository connection."""

        repository = self._open_repository()
        try:
            return revalidate_audio_snapshots(repository, snapshots)
        finally:
            repository.close()

    def remembered_root(self) -> Path | None:
        repository: LibraryRepository | None = None
        try:
            repository = self._open_repository()
            setting = repository.get_setting(LAST_SUCCESSFUL_ROOT_KEY)
        except Exception as error:
            self.warning.emit(f"无法读取上次扫描目录：{error}")
            return None
        finally:
            if repository is not None:
                try:
                    repository.close()
                except Exception as error:
                    self.warning.emit(f"关闭音乐索引失败：{error}")
        if setting is None or not isinstance(setting.value, str):
            return None
        path = Path(setting.value)
        if not path.is_absolute():
            self.warning.emit("已忽略数据库中的无效相对扫描目录")
            return None
        return path

    def start_scan(self, root: Path) -> None:
        if not isinstance(root, Path) or not root.is_absolute():
            raise ValueError("扫描目录必须是绝对 Path")
        if self.running:
            raise RuntimeError("已有只读扫描正在运行")

        config = self._database_config

        def repository_factory() -> LibraryRepository:
            return LibraryRepository(config)

        worker = ReadOnlyScanWorker(
            root=root,
            allowed_root=root,
            repository_factory=repository_factory,
        )
        worker.batch_ready.connect(self.batch_committed)
        worker.completed.connect(self._cache_completed)
        worker.cancelled.connect(self._cache_cancelled)
        worker.failed.connect(self._cache_failed)
        worker.finished.connect(self._worker_finished)
        self._worker = worker
        self._terminal = None
        self._active_root = root
        self.running_changed.emit(True)
        try:
            worker.start()
        except Exception:
            self._worker = None
            self._active_root = None
            self.running_changed.emit(False)
            raise

    def request_cancel(self) -> None:
        if self._worker is not None:
            self._worker.request_cancel()

    def _cache_terminal(self, kind: str, payload: int | str) -> None:
        if self._terminal is None:
            self._terminal = (kind, payload)

    @Slot(int)
    def _cache_completed(self, count: int) -> None:
        self._cache_terminal("completed", count)

    @Slot(int)
    def _cache_cancelled(self, count: int) -> None:
        self._cache_terminal("cancelled", count)

    @Slot(str)
    def _cache_failed(self, message: str) -> None:
        self._cache_terminal("failed", message)

    def _remember_successful_root(self, root: Path) -> None:
        repository = self._open_repository()
        try:
            repository.set_setting(LAST_SUCCESSFUL_ROOT_KEY, str(root))
        finally:
            repository.close()

    def _worker_finished(self) -> None:
        worker = self._worker
        if worker is None:
            return
        terminal = self._terminal or ("failed", "扫描线程结束但没有终态")
        root = self._active_root

        if terminal[0] == "completed" and root is not None:
            try:
                self._remember_successful_root(root)
            except Exception as error:
                self.warning.emit(f"扫描成功，但无法记住扫描目录：{error}")

        reload_warning: str | None = None
        try:
            records = self.load_library()
        except Exception as error:
            reload_warning = f"无法重新读取音乐索引：{error}"
        else:
            self.library_changed.emit(records)

        kind, payload = terminal
        if kind == "completed":
            self.completed.emit(int(payload))
        elif kind == "cancelled":
            self.cancelled.emit(int(payload))
        else:
            self.failed.emit(str(payload))
        if reload_warning is not None:
            self.warning.emit(reload_warning)

        self._worker = None
        self._active_root = None
        self._terminal = None
        worker.deleteLater()
        self.running_changed.emit(False)
