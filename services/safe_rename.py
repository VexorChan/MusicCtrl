"""P2-B safe same-directory rename execution and compensation."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
import os
from pathlib import Path
import stat
import threading

from PySide6.QtCore import QObject, QThread, Signal, Slot

from repositories import (
    LibraryRepository,
    RenameOperationRecord,
    RenamePlanItem,
    RepositoryCommitOutcomeUnknown,
)


_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x0400)


class SafeRenameError(RuntimeError):
    """Raised when a rename cannot be proven safe."""


class SafeRenameCancelled(SafeRenameError):
    """Raised at an item boundary after cancellation was requested."""


class _PostRenameValidationError(SafeRenameError):
    """Carries the pre-rename identity after os.rename already succeeded."""

    def __init__(self, message: str, identity: tuple[int, int, int, int]) -> None:
        super().__init__(message)
        self.identity = identity


@dataclass(frozen=True, slots=True)
class SafeRenameInput:
    asset_id: str
    source_path: Path
    target_path: Path
    allowed_root: Path
    expected_size_bytes: int
    expected_mtime_ns: int | None


@dataclass(frozen=True, slots=True)
class SafeRenameItemResult:
    asset_id: str
    operation_id: str
    item_id: str
    source_path: Path
    target_path: Path
    result: str
    message: str


@dataclass(frozen=True, slots=True)
class SafeRenameRunResult:
    operations: tuple[RenameOperationRecord, ...]
    success_count: int
    failure_count: int
    cancelled_count: int
    items: tuple[SafeRenameItemResult, ...]


RepositoryFactory = Callable[[], LibraryRepository]


def _path_key(path: Path) -> str:
    return os.path.normcase(os.path.normpath(os.path.abspath(os.fspath(path))))


def _windows_name_key(name: str) -> str:
    return name.rstrip(" .").casefold()


def _is_reparse(metadata: os.stat_result) -> bool:
    return bool(int(getattr(metadata, "st_file_attributes", 0)) & _REPARSE_POINT)


def _identity(metadata: os.stat_result) -> tuple[int, int, int, int]:
    return (
        int(metadata.st_dev),
        int(metadata.st_ino),
        int(metadata.st_size),
        int(metadata.st_mtime_ns),
    )


def _within_root(path: Path, root: Path) -> bool:
    try:
        return os.path.commonpath((_path_key(path), _path_key(root))) == _path_key(root)
    except ValueError:
        return False


def _validate_directory_chain(root: Path, parent: Path) -> None:
    if not _within_root(parent, root):
        raise SafeRenameError(f"路径超出已授权扫描根：{parent}")
    root_meta = os.lstat(root)
    if not stat.S_ISDIR(root_meta.st_mode) or stat.S_ISLNK(root_meta.st_mode) or _is_reparse(root_meta):
        raise SafeRenameError(f"授权根不是普通目录：{root}")
    relative = Path(os.path.relpath(parent, root))
    current = root
    if relative == Path("."):
        return
    for part in relative.parts:
        if part in {"", ".", ".."}:
            raise SafeRenameError(f"目录链无效：{parent}")
        current = current / part
        metadata = os.lstat(current)
        if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode) or _is_reparse(metadata):
            raise SafeRenameError(f"目录链包含链接或重解析点：{current}")


@contextmanager
def _locked_directory_chain(root: Path, parent: Path):
    """Keep every Windows directory component open without delete sharing.

    This prevents a checked component from being renamed or replaced by a
    junction in the final path-based ``os.rename`` window.
    """

    _validate_directory_chain(root, parent)
    if os.name != "nt":
        yield
        return

    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL

    file_read_attributes = 0x0080
    delete_access = 0x00010000
    share_read_write = 0x00000001 | 0x00000002
    open_existing = 3
    backup_semantics = 0x02000000
    open_reparse_point = 0x00200000
    invalid_handle = ctypes.c_void_p(-1).value

    relative = Path(os.path.relpath(parent, root))
    paths = [root]
    current = root
    if relative != Path("."):
        for part in relative.parts:
            current = current / part
            paths.append(current)

    handles: list[int] = []
    try:
        for path in paths:
            handle = create_file(
                os.fspath(path),
                file_read_attributes | delete_access,
                share_read_write,
                None,
                open_existing,
                backup_semantics | open_reparse_point,
                None,
            )
            if handle == invalid_handle:
                error = ctypes.get_last_error()
                raise SafeRenameError(
                    f"无法锁定重命名目录链：{path}（Windows 错误 {error}）"
                )
            handles.append(handle)
            metadata = os.lstat(path)
            if (
                not stat.S_ISDIR(metadata.st_mode)
                or stat.S_ISLNK(metadata.st_mode)
                or _is_reparse(metadata)
            ):
                raise SafeRenameError(f"目录链包含链接或重解析点：{path}")
        yield
    finally:
        for handle in reversed(handles):
            close_handle(handle)


def _validate_input(item: SafeRenameInput) -> None:
    if not isinstance(item, SafeRenameInput):
        raise SafeRenameError("重命名输入必须使用 SafeRenameInput")
    if not item.asset_id.strip():
        raise SafeRenameError("asset_id 不能为空")
    for name, value in (("source_path", item.source_path), ("target_path", item.target_path), ("allowed_root", item.allowed_root)):
        if not isinstance(value, Path) or not value.is_absolute():
            raise SafeRenameError(f"{name} 必须是绝对 Path")
    if item.expected_size_bytes < 0 or (
        item.expected_mtime_ns is not None and item.expected_mtime_ns < 0
    ):
        raise SafeRenameError("文件指纹不能为负数")
    if _path_key(item.source_path.parent) != _path_key(item.target_path.parent):
        raise SafeRenameError("重命名目标必须与源文件位于同一目录")
    if _path_key(item.source_path) == _path_key(item.target_path):
        raise SafeRenameError("重命名目标不能与源文件相同")
    if item.source_path.suffix.casefold() != item.target_path.suffix.casefold():
        raise SafeRenameError("重命名不能改变文件扩展名")
    if not _within_root(item.source_path, item.allowed_root) or not _within_root(item.target_path, item.allowed_root):
        raise SafeRenameError("重命名路径超出已授权扫描根")


def _target_is_free(source: Path, target: Path) -> bool:
    target_key = _windows_name_key(target.name)
    source_key = _windows_name_key(source.name)
    with os.scandir(source.parent) as entries:
        for entry in entries:
            key = _windows_name_key(entry.name)
            if key == target_key and key != source_key:
                return False
    return True


def _validated_source_identity(item: SafeRenameInput) -> tuple[int, int, int, int]:
    _validate_directory_chain(item.allowed_root, item.source_path.parent)
    metadata = os.lstat(item.source_path)
    if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode) or _is_reparse(metadata):
        raise SafeRenameError(f"源路径不是普通文件：{item.source_path}")
    if metadata.st_size != item.expected_size_bytes or (
        item.expected_mtime_ns is not None and metadata.st_mtime_ns != item.expected_mtime_ns
    ):
        raise SafeRenameError(f"源文件已变化，请重新分析：{item.source_path}")
    with item.source_path.open("rb") as handle:
        handle_identity = _identity(os.fstat(handle.fileno()))
        if handle_identity != _identity(metadata):
            raise SafeRenameError(f"安全检查与只读打开之间发生变化：{item.source_path}")
    return _identity(metadata)


def _rename_one_file(item: SafeRenameInput) -> tuple[int, int, int, int]:
    expected_identity = _validated_source_identity(item)
    if not _target_is_free(item.source_path, item.target_path):
        raise SafeRenameError(f"目标文件已存在，禁止覆盖：{item.target_path}")
    # Lock every checked component across the final validation, mutation and
    # post-check so a transient junction swap cannot redirect the operation.
    with _locked_directory_chain(item.allowed_root, item.source_path.parent):
        before = os.lstat(item.source_path)
        if _identity(before) != expected_identity or not stat.S_ISREG(before.st_mode) or _is_reparse(before):
            raise SafeRenameError(f"重命名前源文件发生变化：{item.source_path}")
        if not _target_is_free(item.source_path, item.target_path):
            raise SafeRenameError(f"目标文件已存在，禁止覆盖：{item.target_path}")
        os.rename(item.source_path, item.target_path)
        try:
            if os.path.lexists(item.source_path):
                raise SafeRenameError(f"重命名后源路径仍然存在：{item.source_path}")
            _validate_directory_chain(item.allowed_root, item.target_path.parent)
            target_meta = os.lstat(item.target_path)
            if not stat.S_ISREG(target_meta.st_mode) or stat.S_ISLNK(target_meta.st_mode) or _is_reparse(target_meta):
                raise SafeRenameError(f"重命名后目标不是普通文件：{item.target_path}")
            if _identity(target_meta) != expected_identity:
                raise SafeRenameError(f"重命名后文件身份或指纹不一致：{item.target_path}")
        except Exception as error:
            raise _PostRenameValidationError(str(error), expected_identity) from error
    return expected_identity


def _filesystem_matches(path: Path, identity: tuple[int, int, int, int]) -> bool:
    try:
        metadata = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISREG(metadata.st_mode) and not stat.S_ISLNK(metadata.st_mode) and not _is_reparse(metadata) and _identity(metadata) == identity


class SafeRenameWorker(QThread):
    completed = Signal(object)
    cancelled = Signal(object)
    failed = Signal(str)

    def __init__(
        self,
        *,
        items: Sequence[SafeRenameInput],
        repository_factory: RepositoryFactory,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._items = tuple(items)
        self._repository_factory = repository_factory
        self._cancel_event = threading.Event()
        self._lifecycle_lock = threading.Lock()
        self._started_once = False

    def start(self, priority=QThread.Priority.InheritPriority) -> None:
        with self._lifecycle_lock:
            if self._started_once:
                raise RuntimeError("SafeRenameWorker 是 one-shot，不能重复启动")
            self._started_once = True
        super().start(priority)

    def request_cancel(self) -> None:
        self._cancel_event.set()
        self.requestInterruption()

    def _validate_batch(self) -> None:
        if not self._items:
            raise SafeRenameError("没有可执行的重命名项")
        asset_ids: set[str] = set()
        sources: set[str] = set()
        targets: set[str] = set()
        for item in self._items:
            _validate_input(item)
            source_key = _path_key(item.source_path)
            target_key = _path_key(item.target_path)
            if item.asset_id in asset_ids or source_key in sources or target_key in targets:
                raise SafeRenameError("重命名批次包含重复资产、源路径或目标路径")
            asset_ids.add(item.asset_id)
            sources.add(source_key)
            targets.add(target_key)

    def _cancel_planned_operations(self, repository: LibraryRepository, planned: list[tuple[str, tuple[object, ...]]]) -> None:
        for operation_id, records in planned:
            repository.start_rename_operation(operation_id)
            for record in records:
                repository.record_rename_item_outcome(
                    operation_id,
                    record.id,
                    result="cancelled",
                    actual_path=record.source_path,
                    error_code="plan_cancelled",
                    error_message="其他根目录的计划创建失败，未触碰文件",
                )
            repository.finish_rename_operation(operation_id)

    def _process_item(
        self,
        repository: LibraryRepository,
        operation_id: str,
        record: object,
        item: SafeRenameInput,
    ) -> tuple[SafeRenameItemResult, LibraryRepository]:
        repository.start_rename_item(operation_id, record.id)
        identity: tuple[int, int, int, int] | None = None
        try:
            # Keep an executor-owned pre-move identity as well as the helper's
            # checks.  This lets us compensate even when a lower-level post-
            # rename verifier raises without its richer internal exception.
            identity = _validated_source_identity(item)
            identity = _rename_one_file(item)
        except _PostRenameValidationError as error:
            return (
                self._rollback_after_database_failure(
                    repository,
                    operation_id,
                    record.id,
                    item,
                    error.identity,
                    str(error),
                    error_code="post_rename_validation_failed",
                ),
                repository,
            )
        except Exception as error:
            if identity is not None and not os.path.lexists(item.source_path) and os.path.lexists(item.target_path):
                return (
                    self._rollback_after_database_failure(
                        repository,
                        operation_id,
                        record.id,
                        item,
                        identity,
                        str(error),
                        error_code="post_rename_validation_failed",
                    ),
                    repository,
                )
            repository.record_rename_item_outcome(
                operation_id,
                record.id,
                result="failed",
                actual_path=item.source_path if os.path.lexists(item.source_path) else None,
                error_code="filesystem_rename_failed",
                error_message=str(error),
            )
            return (
                SafeRenameItemResult(item.asset_id, operation_id, record.id, item.source_path, item.target_path, "failed", str(error)),
                repository,
            )

        try:
            repository.commit_rename_item(operation_id, record.id)
            return (
                SafeRenameItemResult(item.asset_id, operation_id, record.id, item.source_path, item.target_path, "success", "重命名成功"),
                repository,
            )
        except RepositoryCommitOutcomeUnknown:
            repository.close()
            try:
                readback_repository = self._repository_factory()
            except Exception as readback_error:
                raise SafeRenameError(
                    "数据库提交结果未知且无法重新打开数据库；文件保留在目标路径，"
                    f"需要人工恢复（recovery_required）：{readback_error}"
                ) from readback_error
            try:
                asset = readback_repository.get_asset_by_id(item.asset_id)
                db_item = readback_repository.get_rename_operation_item(record.id)
                if asset is not None and db_item is not None and _path_key(asset.canonical_path) == _path_key(item.target_path) and db_item.result == "success":
                    return (
                        SafeRenameItemResult(item.asset_id, operation_id, record.id, item.source_path, item.target_path, "success", "数据库提交已由回读确认"),
                        readback_repository,
                    )
                if asset is not None and db_item is not None and _path_key(asset.canonical_path) == _path_key(item.source_path) and db_item.result == "running":
                    return (
                        self._rollback_after_database_failure(readback_repository, operation_id, record.id, item, identity, "数据库提交结果不明确，回读确认未提交"),
                        readback_repository,
                    )
                if db_item is not None and db_item.result == "running":
                    readback_repository.record_rename_item_outcome(
                        operation_id, record.id, result="rollback_failed", actual_path=item.target_path if os.path.lexists(item.target_path) else None,
                        error_code="recovery_required", error_message="数据库提交结果与资产状态不一致，需要人工恢复",
                    )
                    return (
                        SafeRenameItemResult(item.asset_id, operation_id, record.id, item.source_path, item.target_path, "rollback_failed", "数据库提交结果不一致，需要人工恢复"),
                        readback_repository,
                    )
                raise SafeRenameError(
                    "数据库提交回读出现混合或不可识别状态；文件保持现场，"
                    "操作不终结为成功，需要人工恢复（recovery_required）"
                )
            except Exception:
                try:
                    readback_repository.close()
                except Exception:
                    pass
                raise
        except Exception as error:
            return (
                self._rollback_after_database_failure(repository, operation_id, record.id, item, identity, str(error)),
                repository,
            )

    def _rollback_after_database_failure(
        self,
        repository: LibraryRepository,
        operation_id: str,
        item_id: str,
        item: SafeRenameInput,
        identity: tuple[int, int, int, int],
        reason: str,
        *,
        error_code: str = "database_commit_failed",
    ) -> SafeRenameItemResult:
        try:
            if os.path.lexists(item.source_path) or not _filesystem_matches(item.target_path, identity):
                raise SafeRenameError("补偿前文件位置或身份不符合预期")
            with _locked_directory_chain(item.allowed_root, item.target_path.parent):
                if os.path.lexists(item.source_path) or not _filesystem_matches(item.target_path, identity):
                    raise SafeRenameError("补偿前文件位置或身份发生变化")
                os.rename(item.target_path, item.source_path)
                if not _filesystem_matches(item.source_path, identity) or os.path.lexists(item.target_path):
                    raise SafeRenameError("恢复原文件名后的校验失败")
            repository.record_rename_item_outcome(
                operation_id, item_id, result="rolled_back", actual_path=item.source_path,
                error_code=error_code, error_message=reason,
            )
            return SafeRenameItemResult(item.asset_id, operation_id, item_id, item.source_path, item.target_path, "rolled_back", reason)
        except Exception as rollback_error:
            actual = item.source_path if os.path.lexists(item.source_path) else item.target_path if os.path.lexists(item.target_path) else None
            repository.record_rename_item_outcome(
                operation_id, item_id, result="rollback_failed", actual_path=actual,
                error_code="recovery_required", error_message=f"{reason}；恢复失败：{rollback_error}",
            )
            return SafeRenameItemResult(item.asset_id, operation_id, item_id, item.source_path, item.target_path, "rollback_failed", f"{reason}；恢复失败：{rollback_error}")

    def _execute(self) -> SafeRenameRunResult:
        self._validate_batch()
        repository = self._repository_factory()
        final_operations: list[RenameOperationRecord] = []
        results: list[SafeRenameItemResult] = []
        planned: list[tuple[str, tuple[object, ...]]] = []
        try:
            grouped: dict[str, list[SafeRenameInput]] = defaultdict(list)
            roots: dict[str, Path] = {}
            for item in self._items:
                key = _path_key(item.allowed_root)
                roots[key] = item.allowed_root
                grouped[key].append(item)
            try:
                for key, group in grouped.items():
                    operation, records = repository.create_rename_operation(
                        allowed_root=roots[key],
                        items=tuple(RenamePlanItem(i.asset_id, i.source_path, i.target_path, i.expected_size_bytes, i.expected_mtime_ns) for i in group),
                    )
                    planned.append((operation.id, records))
            except Exception:
                self._cancel_planned_operations(repository, planned)
                raise

            input_by_asset = {item.asset_id: item for item in self._items}
            for operation_id, records in planned:
                repository.start_rename_operation(operation_id)
                for record in records:
                    item = input_by_asset[record.asset_id]
                    if self._cancel_event.is_set():
                        repository.record_rename_item_outcome(
                            operation_id, record.id, result="cancelled", actual_path=item.source_path,
                            error_code="cancelled", error_message="用户在文件项开始前取消",
                        )
                        results.append(SafeRenameItemResult(item.asset_id, operation_id, record.id, item.source_path, item.target_path, "cancelled", "已取消"))
                        continue
                    result, repository = self._process_item(repository, operation_id, record, item)
                    results.append(result)
                final_operations.append(repository.finish_rename_operation(operation_id))
            success = sum(result.result == "success" for result in results)
            cancelled = sum(result.result == "cancelled" for result in results)
            failure = len(results) - success - cancelled
            return SafeRenameRunResult(tuple(final_operations), success, failure, cancelled, tuple(results))
        finally:
            try:
                repository.close()
            except Exception:
                # The unknown-COMMIT branch intentionally closes the old
                # repository before constructing a readback connection.
                pass

    def run(self) -> None:
        try:
            result = self._execute()
            if self._cancel_event.is_set():
                self.cancelled.emit(result)
            else:
                self.completed.emit(result)
        except Exception as error:
            self.failed.emit(str(error).strip() or error.__class__.__name__)


class SafeRenameController(QObject):
    completed = Signal(object)
    cancelled = Signal(object)
    failed = Signal(str)
    running_changed = Signal(bool)

    def __init__(self, repository_factory: RepositoryFactory, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._repository_factory = repository_factory
        self._worker: SafeRenameWorker | None = None
        self._terminal: tuple[str, object] | None = None

    @property
    def running(self) -> bool:
        return self._worker is not None

    def start(self, items: Sequence[SafeRenameInput]) -> None:
        if self.running:
            raise RuntimeError("已有重命名任务正在运行")
        worker = SafeRenameWorker(items=tuple(items), repository_factory=self._repository_factory)
        worker.completed.connect(lambda value: self._cache("completed", value))
        worker.cancelled.connect(lambda value: self._cache("cancelled", value))
        worker.failed.connect(lambda value: self._cache("failed", value))
        worker.finished.connect(self._worker_finished)
        self._worker = worker
        self._terminal = None
        self.running_changed.emit(True)
        try:
            worker.start()
        except Exception:
            self._worker = None
            self.running_changed.emit(False)
            raise

    def request_cancel(self) -> None:
        if self._worker is not None:
            self._worker.request_cancel()

    def _cache(self, kind: str, payload: object) -> None:
        if self._terminal is None:
            self._terminal = (kind, payload)

    @Slot()
    def _worker_finished(self) -> None:
        worker = self._worker
        if worker is None:
            return
        terminal = self._terminal or ("failed", "重命名线程结束但没有终态")
        self._worker = None
        self._terminal = None
        worker.deleteLater()
        self.running_changed.emit(False)
        kind, payload = terminal
        if kind == "completed":
            self.completed.emit(payload)
        elif kind == "cancelled":
            self.cancelled.emit(payload)
        else:
            self.failed.emit(str(payload))
