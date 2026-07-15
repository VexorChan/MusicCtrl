"""Background read-only scanning with thread-owned repository writes."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
import threading
from typing import Protocol

from PySide6.QtCore import QThread, Signal

from repositories import IndexBatchItem, LibraryRepository
from services.read_only_scanner import AudioFileEntry, ScanCancelled, iter_audio_files


class RepositoryFactory(Protocol):
    def __call__(self) -> LibraryRepository: ...


class ReadOnlyScanWorker(QThread):
    """Enumerate files and index immutable batches outside the Qt main thread."""

    batch_ready = Signal(object)
    completed = Signal(int)
    cancelled = Signal(int)
    failed = Signal(str)

    def __init__(
        self,
        *,
        root: Path,
        allowed_root: Path,
        repository_factory: RepositoryFactory,
        batch_size: int = 100,
        parent=None,
    ) -> None:
        super().__init__(parent)
        if not callable(repository_factory):
            raise TypeError("repository_factory 必须可调用")
        if isinstance(batch_size, bool) or not isinstance(batch_size, int) or batch_size <= 0:
            raise ValueError("batch_size 必须是正整数")
        self._root = root
        self._allowed_root = allowed_root
        self._repository_factory = repository_factory
        self._batch_size = batch_size
        self._cancel_event = threading.Event()
        self._lifecycle_lock = threading.Lock()
        self._start_called = False
        self._run_entered = False

    def start(self, priority=QThread.InheritPriority) -> None:
        with self._lifecycle_lock:
            if self._start_called:
                raise RuntimeError("ReadOnlyScanWorker 是 one-shot，不能重复启动")
            self._start_called = True
        super().start(priority)

    def request_cancel(self) -> None:
        """Thread-safely request cancellation without relying on a queued slot."""

        self._cancel_event.set()
        self.requestInterruption()

    def cancel(self) -> None:
        self.request_cancel()

    def _check_cancelled(self) -> None:
        if self._cancel_event.is_set():
            raise ScanCancelled("扫描已取消")

    @staticmethod
    def _error_message(error: BaseException) -> str:
        detail = str(error).strip()
        return detail or error.__class__.__name__

    def _write_batch(
        self,
        repository: LibraryRepository,
        session_id: str,
        entries: tuple[AudioFileEntry, ...],
    ) -> int:
        self._check_cancelled()
        records = repository.index_scan_batch(
            session_id,
            tuple(
                IndexBatchItem(
                    canonical_path=entry.path,
                    size_bytes=entry.size_bytes,
                    mtime_ns=entry.mtime_ns,
                )
                for entry in entries
            ),
        )
        return len(records)

    def run(self) -> None:
        with self._lifecycle_lock:
            if self._run_entered:
                self.failed.emit("ReadOnlyScanWorker 是 one-shot，不能重复运行")
                return
            self._run_entered = True

        repository: LibraryRepository | None = None
        session_id: str | None = None
        indexed_count = 0
        terminal: tuple[str, int | str] | None = None

        try:
            self._check_cancelled()
            repository = self._repository_factory()
            self._check_cancelled()
            session = repository.create_scan_session(mode="audio", source_folder=self._root)
            session_id = session.id
            self._check_cancelled()

            pending: list[AudioFileEntry] = []
            for entry in iter_audio_files(
                self._root,
                allowed_root=self._allowed_root,
                cancel_requested=self._cancel_event.is_set,
            ):
                self._check_cancelled()
                pending.append(entry)
                if len(pending) < self._batch_size:
                    continue
                batch = tuple(pending)
                pending.clear()
                indexed_count += self._write_batch(repository, session_id, batch)
                self._check_cancelled()
                self.batch_ready.emit(batch)

            self._check_cancelled()
            if pending:
                batch = tuple(pending)
                indexed_count += self._write_batch(repository, session_id, batch)
                self._check_cancelled()
                self.batch_ready.emit(batch)

            self._check_cancelled()
            repository.finish_scan_session(session_id, status="completed")
            terminal = ("completed", indexed_count)
        except ScanCancelled:
            if repository is not None and session_id is not None:
                try:
                    repository.finish_scan_session(session_id, status="cancelled")
                except Exception as finish_error:
                    terminal = (
                        "failed",
                        f"取消扫描后无法终结会话：{self._error_message(finish_error)}",
                    )
            if terminal is None:
                terminal = ("cancelled", indexed_count)
        except Exception as error:
            message = self._error_message(error)
            if repository is not None and session_id is not None:
                try:
                    repository.finish_scan_session(session_id, status="failed")
                except Exception as finish_error:
                    message = (
                        f"{message}；同时无法把扫描会话标记为失败："
                        f"{self._error_message(finish_error)}"
                    )
            terminal = ("failed", message)
        finally:
            if repository is not None:
                try:
                    repository.close()
                except Exception as close_error:
                    close_message = f"关闭 repository 失败：{self._error_message(close_error)}"
                    if terminal is not None and terminal[0] == "failed":
                        terminal = ("failed", f"{terminal[1]}；{close_message}")
                    else:
                        terminal = ("failed", close_message)

            if terminal is None:
                terminal = ("failed", "扫描线程未产生终态")
            kind, payload = terminal
            if kind == "completed":
                self.completed.emit(int(payload))
            elif kind == "cancelled":
                self.cancelled.emit(int(payload))
            else:
                self.failed.emit(str(payload))
