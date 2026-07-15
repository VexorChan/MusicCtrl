"""P6 verified move-import for explicitly selected roots."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import stat
import tempfile
import threading
import time
from uuid import uuid4

from PySide6.QtCore import QObject, QThread, Signal

from services.file_safety import _is_reparse, _locked_directory_chain, _within_root
from repositories import LibraryRepository


SUPPORTED_AUDIO = {".mp3", ".flac", ".wav", ".m4a", ".ogg", ".aac"}
_CANDIDATE_PREFIX = ".musicctrl-import-"
IMPORT_HISTORY_KEY = "p6.import_history"


class SafeImportError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ImportItemResult:
    source_path: Path
    target_path: Path
    status: str
    message: str
    sha256: str | None = None


@dataclass(frozen=True, slots=True)
class ImportRunResult:
    source_root: Path
    target_root: Path
    items: tuple[ImportItemResult, ...]
    success_count: int
    duplicate_count: int
    conflict_count: int
    failure_count: int
    action: str = "import"


def _result_to_history(result: ImportRunResult) -> dict[str, object]:
    return {
        "id": str(uuid4()),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source_root": str(result.source_root),
        "target_root": str(result.target_root),
        "undone_at": None,
        "complete": bool(result.items) and result.success_count == len(result.items),
        "items": [
            {
                "source_path": str(item.source_path),
                "target_path": str(item.target_path),
                "status": item.status,
                "sha256": item.sha256,
                "message": item.message,
            }
            for item in result.items
        ],
    }


def _load_history(repository: LibraryRepository) -> list[dict[str, object]]:
    setting = repository.get_setting(IMPORT_HISTORY_KEY)
    if setting is None:
        return []
    if not isinstance(setting.value, list) or not all(isinstance(item, dict) for item in setting.value):
        raise SafeImportError("导入历史格式损坏")
    return [dict(item) for item in setting.value]


def _validate_root(root: Path, *, label: str) -> None:
    if not isinstance(root, Path) or not root.is_absolute():
        raise SafeImportError(f"{label}必须是绝对 Path")
    try:
        metadata = os.lstat(root)
    except OSError as error:
        raise SafeImportError(f"{label}不存在或无法访问：{root}") from error
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode) or _is_reparse(metadata):
        raise SafeImportError(f"{label}不能是链接或重解析点：{root}")


def _file_identity(path: Path) -> tuple[int, int, int, int]:
    metadata = os.lstat(path)
    if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode) or _is_reparse(metadata):
        raise SafeImportError(f"源路径不是普通文件：{path}")
    return (metadata.st_dev, metadata.st_ino, metadata.st_size, metadata.st_mtime_ns)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def enumerate_import_files(root: Path, *, mode: str) -> tuple[Path, ...]:
    _validate_root(root, label="源目录")
    extensions = SUPPORTED_AUDIO if mode == "audio" else {".lrc"} if mode == "lyrics" else set()
    if not extensions:
        raise SafeImportError("导入模式必须是 audio 或 lyrics")
    found: list[Path] = []

    def visit(folder: Path) -> None:
        with os.scandir(folder) as entries:
            ordered = sorted(entries, key=lambda entry: (entry.name.casefold(), entry.name))
        for entry in ordered:
            metadata = entry.stat(follow_symlinks=False)
            if entry.is_symlink() or _is_reparse(metadata):
                continue
            path = folder / entry.name
            if entry.is_dir(follow_symlinks=False):
                visit(path)
            elif entry.is_file(follow_symlinks=False) and path.suffix.casefold() in extensions:
                found.append(path)

    visit(root)
    return tuple(found)


def cleanup_stale_candidates(target_root: Path, *, min_age_seconds: float = 24 * 60 * 60) -> int:
    _validate_root(target_root, label="目标目录")
    removed = 0
    for entry in os.scandir(target_root):
        if not entry.name.startswith(_CANDIDATE_PREFIX):
            continue
        metadata = entry.stat(follow_symlinks=False)
        if (
            entry.is_file(follow_symlinks=False)
            and not entry.is_symlink()
            and not _is_reparse(metadata)
            and time.time() - metadata.st_mtime >= min_age_seconds
        ):
            os.unlink(target_root / entry.name)
            removed += 1
    return removed


def import_one(
    source: Path,
    *,
    source_root: Path,
    target_root: Path,
    cancel_event: threading.Event | None = None,
) -> ImportItemResult:
    _validate_root(source_root, label="源目录")
    _validate_root(target_root, label="目标目录")
    if not _within_root(source, source_root):
        raise SafeImportError("源文件超出已选择源目录")
    if _within_root(target_root, source_root) or _within_root(source_root, target_root):
        raise SafeImportError("源目录和目标目录不能相同或互相包含")
    identity = _file_identity(source)
    target = target_root / source.name
    source_hash = _sha256(source)
    if target.exists():
        if _file_identity(source) != identity:
            raise SafeImportError("源文件在冲突检查期间发生变化")
        target_identity = _file_identity(target)
        target_hash = _sha256(target)
        if target_hash == source_hash and target_identity[2] == identity[2]:
            return ImportItemResult(source, target, "duplicate", "目标已有相同内容，已保留源文件", source_hash)
        return ImportItemResult(source, target, "conflict", "同名目标内容不同，禁止覆盖", source_hash)

    candidate: Path | None = None
    try:
        with _locked_directory_chain(target_root, target_root):
            handle = tempfile.NamedTemporaryFile(
                mode="wb",
                prefix=_CANDIDATE_PREFIX,
                suffix=".tmp",
                dir=target_root,
                delete=False,
            )
            candidate = Path(handle.name)
            with handle, source.open("rb") as source_handle:
                while True:
                    if cancel_event is not None and cancel_event.is_set():
                        raise InterruptedError("用户取消导入")
                    chunk = source_handle.read(1024 * 1024)
                    if not chunk:
                        break
                    handle.write(chunk)
                handle.flush()
                os.fsync(handle.fileno())
            if _file_identity(source) != identity:
                raise SafeImportError("源文件在复制期间发生变化")
            if candidate.stat().st_size != identity[2] or _sha256(candidate) != source_hash:
                raise SafeImportError("目标临时文件大小或 SHA-256 校验失败")
            if target.exists():
                raise SafeImportError("目标在导入期间出现，禁止覆盖")
            os.rename(candidate, target)
            candidate = None
            if target.stat().st_size != identity[2] or _sha256(target) != source_hash:
                raise SafeImportError("目标落位后校验失败")
            if _file_identity(source) != identity:
                raise SafeImportError("删除源文件前源文件发生变化")
            try:
                os.unlink(source)
            except OSError as error:
                try:
                    os.unlink(target)
                except OSError as rollback_error:
                    raise SafeImportError(
                        f"源文件删除失败且目标副本回滚失败，需要人工处理：{rollback_error}"
                    ) from error
                raise SafeImportError("源文件删除失败，已移除目标副本") from error
        return ImportItemResult(source, target, "success", "大小和 SHA-256 校验通过，已安全移动", source_hash)
    finally:
        if candidate is not None and candidate.exists():
            try:
                os.unlink(candidate)
            except OSError:
                pass


class SafeImportWorker(QThread):
    completed = Signal(object)
    cancelled = Signal(object)
    failed = Signal(str)

    def __init__(self, *, source_root: Path, target_root: Path, mode: str, parent=None) -> None:
        super().__init__(parent)
        self._source_root = source_root
        self._target_root = target_root
        self._mode = mode
        self._cancel = threading.Event()

    def request_cancel(self) -> None:
        self._cancel.set()

    def run(self) -> None:
        results: list[ImportItemResult] = []
        try:
            cleanup_stale_candidates(self._target_root)
            for source in enumerate_import_files(self._source_root, mode=self._mode):
                if self._cancel.is_set():
                    self.cancelled.emit(self._result(results))
                    return
                try:
                    result = import_one(
                        source,
                        source_root=self._source_root,
                        target_root=self._target_root,
                        cancel_event=self._cancel,
                    )
                except InterruptedError:
                    self.cancelled.emit(self._result(results))
                    return
                except Exception as error:
                    result = ImportItemResult(source, self._target_root / source.name, "failed", str(error))
                results.append(result)
            self.completed.emit(self._result(results))
        except Exception as error:
            self.failed.emit(str(error).strip() or error.__class__.__name__)

    def _result(self, items: list[ImportItemResult]) -> ImportRunResult:
        return ImportRunResult(
            self._source_root,
            self._target_root,
            tuple(items),
            sum(item.status == "success" for item in items),
            sum(item.status == "duplicate" for item in items),
            sum(item.status == "conflict" for item in items),
            sum(item.status == "failed" for item in items),
        )


class SafeImportUndoWorker(QThread):
    completed = Signal(object)
    failed = Signal(str)

    def __init__(self, *, batch: dict[str, object], repository_factory, parent=None) -> None:
        super().__init__(parent)
        self._batch = batch
        self._repository_factory = repository_factory

    def run(self) -> None:
        restored: list[tuple[Path, Path]] = []
        try:
            source_root = Path(str(self._batch["source_root"]))
            target_root = Path(str(self._batch["target_root"]))
            raw_items = self._batch.get("items")
            if not source_root.is_absolute() or not target_root.is_absolute() or not isinstance(raw_items, list):
                raise SafeImportError("导入历史路径损坏")
            items = [item for item in raw_items if isinstance(item, dict) and item.get("status") == "success"]
            if not items or len(items) != len(raw_items):
                raise SafeImportError("只有完整成功的导入批次可以撤销")
            for item in items:
                source = Path(str(item["source_path"]))
                target = Path(str(item["target_path"]))
                expected_hash = str(item.get("sha256") or "")
                if source.exists() or not target.is_file() or _sha256(target) != expected_hash:
                    raise SafeImportError(f"文件已变化，不能撤销：{target.name}")
            for item in reversed(items):
                source = Path(str(item["source_path"]))
                target = Path(str(item["target_path"]))
                moved = import_one(target, source_root=target_root, target_root=source.parent)
                if moved.status != "success" or moved.sha256 != item.get("sha256"):
                    raise SafeImportError(f"撤销校验失败：{target.name}")
                restored.append((source, target))
            repository = self._repository_factory()
            try:
                history = _load_history(repository)
                matching = [entry for entry in history if entry.get("id") == self._batch.get("id")]
                if len(matching) != 1:
                    raise SafeImportError("导入历史已变化")
                matching[0]["undone_at"] = datetime.now(timezone.utc).isoformat()
                repository.set_setting(IMPORT_HISTORY_KEY, history)
            except Exception:
                for source, target in reversed(restored):
                    import_one(source, source_root=source_root, target_root=target.parent)
                raise
            finally:
                repository.close()
            results = tuple(
                ImportItemResult(target, source, "success", "已撤销导入并恢复源路径", str(item["sha256"]))
                for item, (source, target) in zip(reversed(items), restored)
            )
            self.completed.emit(
                ImportRunResult(source_root, target_root, results, len(results), 0, 0, 0, "undo")
            )
        except Exception as error:
            if restored:
                try:
                    source_root = Path(str(self._batch["source_root"]))
                    for source, target in reversed(restored):
                        if source.exists() and not target.exists():
                            import_one(source, source_root=source_root, target_root=target.parent)
                except Exception as rollback_error:
                    self.failed.emit(f"撤销失败且补偿失败，需要人工处理：{rollback_error}")
                    return
            self.failed.emit(str(error).strip() or error.__class__.__name__)


class SafeImportController(QObject):
    completed = Signal(object)
    cancelled = Signal(object)
    failed = Signal(str)
    running_changed = Signal(bool)

    warning = Signal(str)

    def __init__(self, repository_factory=None, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._repository_factory = repository_factory
        self._worker: SafeImportWorker | None = None
        self._terminal: tuple[str, object] | None = None

    @property
    def running(self) -> bool:
        return self._worker is not None

    def start(self, source_root: Path, target_root: Path, mode: str) -> None:
        if self.running:
            raise RuntimeError("已有安全导入任务正在运行")
        worker = SafeImportWorker(source_root=source_root, target_root=target_root, mode=mode)
        worker.completed.connect(lambda value: self._cache("completed", value))
        worker.cancelled.connect(lambda value: self._cache("cancelled", value))
        worker.failed.connect(lambda value: self._cache("failed", value))
        worker.finished.connect(self._finished)
        self._worker = worker
        self._terminal = None
        self.running_changed.emit(True)
        worker.start()

    def list_history(self) -> tuple[dict[str, object], ...]:
        if self._repository_factory is None:
            return ()
        with self._repository_factory() as repository:
            return tuple(_load_history(repository))

    def undo_last_complete(self) -> None:
        if self.running:
            raise RuntimeError("已有安全导入任务正在运行")
        if self._repository_factory is None:
            raise SafeImportError("没有可用的导入历史仓库")
        history = self.list_history()
        candidates = [item for item in history if item.get("complete") is True and item.get("undone_at") is None]
        if not candidates:
            raise SafeImportError("没有可撤销的完整导入批次")
        batch = candidates[-1]
        worker = SafeImportUndoWorker(batch=batch, repository_factory=self._repository_factory)
        worker.completed.connect(lambda value: self._cache("completed", value))
        worker.failed.connect(lambda value: self._cache("failed", value))
        worker.finished.connect(self._finished)
        self._worker = worker  # type: ignore[assignment]
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
        kind, value = self._terminal or ("failed", "导入线程结束但没有终态")
        if kind == "completed" and isinstance(value, ImportRunResult) and value.action == "import" and self._repository_factory is not None:
            try:
                with self._repository_factory() as repository:
                    history = _load_history(repository)
                    history.append(_result_to_history(value))
                    repository.set_setting(IMPORT_HISTORY_KEY, history)
            except Exception as error:
                self.warning.emit(f"文件已导入，但历史保存失败：{error}")
        self._worker = None
        self._terminal = None
        worker.deleteLater()
        self.running_changed.emit(False)
        if kind == "completed":
            self.completed.emit(value)
        elif kind == "cancelled":
            self.cancelled.emit(value)
        else:
            self.failed.emit(str(value))
