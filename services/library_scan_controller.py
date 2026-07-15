"""UI-thread coordinator for the P1 read-only scan worker."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QObject, Signal, Slot

from database import DatabaseConfig
from repositories import AssetRecord, LibraryRepository
from services.scan_worker import ReadOnlyScanWorker


LAST_SUCCESSFUL_ROOT_KEY = "p1.last_successful_audio_root"


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
        "title": Path(asset.file_name).stem,
        "artist": "待识别",
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
