"""P6 verified move-import for explicitly selected roots."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import os
from pathlib import Path
import stat
import tempfile
import threading
import time
from typing import Iterator
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
class ImportPreviewItem:
    source_path: Path
    target_path: Path
    status: str
    message: str
    expected_device: int
    expected_inode: int
    expected_size_bytes: int
    expected_mtime_ns: int
    expected_sha256: str


@dataclass(frozen=True, slots=True)
class ImportPreviewPlan:
    id: str
    source_root: Path
    target_root: Path
    mode: str
    created_at: str
    items: tuple[ImportPreviewItem, ...]
    ready_count: int
    duplicate_count: int
    conflict_count: int
    failure_count: int


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
    mode: str = "audio"
    terminal_status: str = "completed"
    terminal_message: str = ""
    plan_id: str | None = None


def _validate_mode(mode: object) -> str:
    if mode not in {"audio", "lyrics"}:
        raise SafeImportError("导入模式必须是 audio 或 lyrics")
    return str(mode)


def _result_to_history(result: ImportRunResult) -> dict[str, object]:
    return {
        "id": str(uuid4()),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "mode": _validate_mode(result.mode),
        "source_root": str(result.source_root),
        "target_root": str(result.target_root),
        "undone_at": None,
        "terminal_status": result.terminal_status,
        "terminal_message": result.terminal_message,
        "plan_id": result.plan_id,
        "complete": (
            result.terminal_status == "completed"
            and result.success_count > 0
            and result.failure_count == 0
            and not any(item.status == "cancelled" for item in result.items)
        ),
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
    overflow = len(setting.value) > 200
    legacy_overflow = overflow and all(
        all(key not in item for key in ("terminal_status", "terminal_message", "plan_id"))
        for item in setting.value
    )
    if overflow and not legacy_overflow:
        raise SafeImportError("新版导入历史超过安全上限")
    history: list[dict[str, object]] = []
    identifiers: set[str] = set()
    for raw_entry in setting.value:
        entry = dict(raw_entry)
        _validate_mode(entry.get("mode"))
        if not isinstance(entry.get("id"), str) or not entry["id"]:
            raise SafeImportError("导入历史批次编号损坏")
        if entry["id"] in identifiers:
            raise SafeImportError("导入历史包含重复批次编号")
        identifiers.add(entry["id"])
        if not isinstance(entry.get("complete"), bool) or not isinstance(entry.get("items"), list):
            raise SafeImportError("导入历史批次状态损坏")
        terminal_status = entry.get("terminal_status", "completed")
        if terminal_status not in {"completed", "cancelled", "failed"}:
            raise SafeImportError("导入历史终态损坏")
        entry["terminal_status"] = terminal_status
        if not isinstance(entry.get("terminal_message", ""), str):
            raise SafeImportError("导入历史终态说明损坏")
        if entry.get("plan_id") is not None and not isinstance(entry.get("plan_id"), str):
            raise SafeImportError("导入历史预览编号损坏")
        entry.setdefault("terminal_message", "")
        entry.setdefault("plan_id", None)
        for key in ("created_at", "undone_at"):
            value = entry.get(key)
            if key == "undone_at" and value is None:
                continue
            if not isinstance(value, str):
                raise SafeImportError("导入历史时间损坏")
            try:
                parsed = datetime.fromisoformat(value)
            except ValueError as error:
                raise SafeImportError("导入历史时间损坏") from error
            if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
                raise SafeImportError("导入历史时间必须使用 UTC 时区")
        for key in ("source_root", "target_root"):
            value = entry.get(key)
            if not isinstance(value, str) or not Path(value).is_absolute():
                raise SafeImportError("导入历史路径损坏")
        success_count = 0
        has_failure = False
        for item in entry["items"]:
            if not isinstance(item, dict) or item.get("status") not in {
                "success", "duplicate", "conflict", "failed", "cancelled"
            }:
                raise SafeImportError("导入历史项目损坏")
            for key in ("source_path", "target_path"):
                value = item.get(key)
                if not isinstance(value, str) or not Path(value).is_absolute():
                    raise SafeImportError("导入历史项目路径损坏")
            digest = item.get("sha256")
            if digest is not None and (
                not isinstance(digest, str)
                or len(digest) != 64
                or any(character not in "0123456789abcdefABCDEF" for character in digest)
            ):
                raise SafeImportError("导入历史 SHA-256 损坏")
            if not isinstance(item.get("message", ""), str):
                raise SafeImportError("导入历史项目说明损坏")
            success_count += item.get("status") == "success"
            has_failure = has_failure or item.get("status") in {"failed", "cancelled"}
        if entry["complete"] and (
            terminal_status != "completed" or success_count == 0 or has_failure
        ):
            raise SafeImportError("导入历史完整状态损坏")
        history.append(entry)
    return history[-200:] if legacy_overflow else history


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


def _sha256(path: Path, *, cancel_event: threading.Event | None = None) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            if cancel_event is not None and cancel_event.is_set():
                raise InterruptedError("用户取消导入")
            chunk = handle.read(1024 * 1024)
            if cancel_event is not None and cancel_event.is_set():
                raise InterruptedError("用户取消导入")
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _path_key(path: Path) -> str:
    return os.path.normcase(os.path.normpath(os.path.abspath(os.fspath(path)))).replace("\\", "/").casefold()


def _validate_roots(source_root: Path, target_root: Path) -> None:
    _validate_root(source_root, label="源目录")
    _validate_root(target_root, label="目标目录")
    if _within_root(target_root, source_root) or _within_root(source_root, target_root):
        raise SafeImportError("源目录和目标目录不能相同或互相包含")


def _stable_identity_and_hash(
    path: Path, *, cancel_event: threading.Event | None = None
) -> tuple[tuple[int, int, int, int], str]:
    before = _file_identity(path)
    digest = _sha256(path, cancel_event=cancel_event)
    after = _file_identity(path)
    if before != after:
        raise SafeImportError(f"文件在只读分析期间发生变化：{path.name}")
    return before, digest


def _preview_item(
    source: Path,
    *,
    source_root: Path,
    target_root: Path,
    cancel_event: threading.Event | None = None,
) -> ImportPreviewItem:
    target = target_root / source.name
    try:
        with _locked_directory_chain(source_root, source.parent), _locked_directory_chain(
            target_root, target_root
        ):
            identity, source_hash = _stable_identity_and_hash(source, cancel_event=cancel_event)
            status = "ready"
            message = "等待确认后安全移动"
            if os.path.lexists(target):
                target_identity, target_hash = _stable_identity_and_hash(
                    target, cancel_event=cancel_event
                )
                if target_identity[2] == identity[2] and target_hash == source_hash:
                    status, message = "duplicate", "目标已有相同内容，将保留源文件"
                else:
                    status, message = "conflict", "同名目标内容不同，禁止覆盖"
            return ImportPreviewItem(
                source, target, status, message,
                identity[0], identity[1], identity[2], identity[3], source_hash,
            )
    except Exception as error:
        return ImportPreviewItem(
            source, target, "failed", str(error).strip() or error.__class__.__name__,
            -1, -1, -1, -1, "",
        )


def iter_import_files(
    root: Path,
    *,
    mode: str,
    cancel_event: threading.Event | None = None,
) -> Iterator[Path]:
    _validate_root(root, label="源目录")
    mode = _validate_mode(mode)
    extensions = SUPPORTED_AUDIO if mode == "audio" else {".lrc"}

    def visit(folder: Path) -> Iterator[Path]:
        ordered: list[os.DirEntry[str]] = []
        with os.scandir(folder) as entries:
            iterator = iter(entries)
            while True:
                if cancel_event is not None and cancel_event.is_set():
                    return
                try:
                    entry = next(iterator)
                except StopIteration:
                    break
                if cancel_event is not None and cancel_event.is_set():
                    return
                ordered.append(entry)
        ordered.sort(key=lambda entry: (entry.name.casefold(), entry.name))
        for entry in ordered:
            if cancel_event is not None and cancel_event.is_set():
                return
            metadata = entry.stat(follow_symlinks=False)
            if cancel_event is not None and cancel_event.is_set():
                return
            if entry.is_symlink() or _is_reparse(metadata):
                continue
            path = folder / entry.name
            if entry.is_dir(follow_symlinks=False):
                yield from visit(path)
            elif entry.is_file(follow_symlinks=False) and path.suffix.casefold() in extensions:
                if cancel_event is not None and cancel_event.is_set():
                    return
                yield path

    yield from visit(root)


def enumerate_import_files(root: Path, *, mode: str) -> tuple[Path, ...]:
    return tuple(iter_import_files(root, mode=mode))


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
    expected_sha256: str | None = None,
    expected_identity: tuple[int, int, int, int] | None = None,
    expected_target_absent: bool = False,
) -> ImportItemResult:
    _validate_roots(source_root, target_root)
    if not isinstance(source, Path) or not source.is_absolute() or not _within_root(source, source_root):
        raise SafeImportError("源文件超出已选择源目录")
    candidate: Path | None = None
    try:
        with _locked_directory_chain(source_root, source.parent), _locked_directory_chain(
            target_root, target_root
        ):
            identity = _file_identity(source)
            if expected_identity is not None and identity != expected_identity:
                raise SafeImportError("源文件身份与预览记录不一致")
            target = target_root / source.name
            source_hash = _sha256(source, cancel_event=cancel_event)
            if expected_sha256 is not None:
                if (
                    len(expected_sha256) != 64
                    or any(character not in "0123456789abcdefABCDEF" for character in expected_sha256)
                    or source_hash != expected_sha256.casefold()
                ):
                    raise SafeImportError("源文件 SHA-256 与预期记录不一致")
            if target.exists():
                if expected_target_absent:
                    raise SafeImportError("目标在预览后出现，禁止覆盖")
                if _file_identity(source) != identity:
                    raise SafeImportError("源文件在冲突检查期间发生变化")
                target_identity = _file_identity(target)
                target_hash = _sha256(target, cancel_event=cancel_event)
                if _file_identity(target) != target_identity:
                    raise SafeImportError("目标文件在冲突检查期间发生变化")
                if target_hash == source_hash and target_identity[2] == identity[2]:
                    return ImportItemResult(source, target, "duplicate", "目标已有相同内容，已保留源文件", source_hash)
                return ImportItemResult(source, target, "conflict", "同名目标内容不同，禁止覆盖", source_hash)
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
            if candidate.stat().st_size != identity[2] or _sha256(
                candidate, cancel_event=cancel_event
            ) != source_hash:
                raise SafeImportError("目标临时文件大小或 SHA-256 校验失败")
            if target.exists():
                raise SafeImportError("目标在导入期间出现，禁止覆盖")
            os.rename(candidate, target)
            candidate = None
            try:
                target_hash = _sha256(target, cancel_event=cancel_event)
            except InterruptedError:
                try:
                    os.unlink(target)
                except OSError as rollback_error:
                    raise SafeImportError(
                        f"取消时目标副本回滚失败，需要人工处理：{rollback_error}"
                    ) from rollback_error
                raise
            if target.stat().st_size != identity[2] or target_hash != source_hash:
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


class SafeImportPreviewWorker(QThread):
    completed = Signal(object)
    cancelled = Signal()
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
        try:
            _validate_roots(self._source_root, self._target_root)
            mode = _validate_mode(self._mode)
            items: list[ImportPreviewItem] = []
            for source in iter_import_files(self._source_root, mode=mode, cancel_event=self._cancel):
                if self._cancel.is_set():
                    self.cancelled.emit()
                    return
                items.append(_preview_item(
                    source,
                    source_root=self._source_root,
                    target_root=self._target_root,
                    cancel_event=self._cancel,
                ))
            if self._cancel.is_set():
                self.cancelled.emit()
                return
            grouped: dict[str, list[int]] = {}
            for index, item in enumerate(items):
                grouped.setdefault(_path_key(item.target_path), []).append(index)
            for indexes in grouped.values():
                if len(indexes) < 2:
                    continue
                for index in indexes:
                    item = items[index]
                    if item.status != "failed":
                        items[index] = ImportPreviewItem(
                            item.source_path, item.target_path, "conflict",
                            "同一批次存在 Windows 等价目标，禁止执行",
                            item.expected_device, item.expected_inode,
                            item.expected_size_bytes, item.expected_mtime_ns,
                            item.expected_sha256,
                        )
            plan = ImportPreviewPlan(
                str(uuid4()), self._source_root, self._target_root, mode,
                datetime.now(timezone.utc).isoformat(), tuple(items),
                sum(item.status == "ready" for item in items),
                sum(item.status == "duplicate" for item in items),
                sum(item.status == "conflict" for item in items),
                sum(item.status == "failed" for item in items),
            )
            self.completed.emit(plan)
        except Exception as error:
            self.failed.emit(str(error).strip() or error.__class__.__name__)


class SafeImportWorker(QThread):
    completed = Signal(object)
    cancelled = Signal(object)
    failed = Signal(object)

    def __init__(self, *, plan: ImportPreviewPlan, parent=None) -> None:
        super().__init__(parent)
        self._plan = plan
        self._cancel = threading.Event()

    def request_cancel(self) -> None:
        self._cancel.set()

    def run(self) -> None:
        results: list[ImportItemResult] = []
        try:
            _validate_roots(self._plan.source_root, self._plan.target_root)
            for index, item in enumerate(self._plan.items):
                if item.status != "ready":
                    results.append(ImportItemResult(
                        item.source_path, item.target_path, item.status, item.message,
                        item.expected_sha256 or None,
                    ))
                    continue
                if self._cancel.is_set():
                    self._append_unstarted(results, index, "cancelled", "用户取消，未执行")
                    self.cancelled.emit(self._result(results, "cancelled", "用户取消导入"))
                    return
                try:
                    result = import_one(
                        item.source_path,
                        source_root=self._plan.source_root,
                        target_root=self._plan.target_root,
                        cancel_event=self._cancel,
                        expected_sha256=item.expected_sha256,
                        expected_identity=(
                            item.expected_device, item.expected_inode,
                            item.expected_size_bytes, item.expected_mtime_ns,
                        ),
                        expected_target_absent=True,
                    )
                except InterruptedError:
                    self._append_unstarted(results, index, "cancelled", "用户取消，未执行")
                    self.cancelled.emit(self._result(results, "cancelled", "用户取消导入"))
                    return
                except Exception as error:
                    result = ImportItemResult(
                        item.source_path, item.target_path, "failed",
                        str(error).strip() or error.__class__.__name__, item.expected_sha256,
                    )
                results.append(result)
            self.completed.emit(self._result(results, "completed", ""))
        except Exception as error:
            self._append_unstarted(results, len(results), "failed", "执行未完成")
            self.failed.emit(self._result(
                results, "failed", str(error).strip() or error.__class__.__name__
            ))

    def _append_unstarted(
        self, results: list[ImportItemResult], start_index: int, status: str, message: str
    ) -> None:
        for item in self._plan.items[start_index:]:
            if item.status == "ready":
                results.append(ImportItemResult(
                    item.source_path, item.target_path, status, message, item.expected_sha256
                ))
            elif not any(existing.source_path == item.source_path for existing in results):
                results.append(ImportItemResult(
                    item.source_path, item.target_path, item.status, item.message,
                    item.expected_sha256 or None,
                ))

    def _result(self, items: list[ImportItemResult], terminal_status: str, message: str) -> ImportRunResult:
        return ImportRunResult(
            self._plan.source_root,
            self._plan.target_root,
            tuple(items),
            sum(item.status == "success" for item in items),
            sum(item.status == "duplicate" for item in items),
            sum(item.status == "conflict" for item in items),
            sum(item.status == "failed" for item in items),
            mode=self._plan.mode,
            terminal_status=terminal_status,
            terminal_message=message,
            plan_id=self._plan.id,
        )


class SafeImportUndoWorker(QThread):
    completed = Signal(object)
    cancelled = Signal(object)
    failed = Signal(str)

    def __init__(self, *, batch: dict[str, object], repository_factory, parent=None) -> None:
        super().__init__(parent)
        self._batch = batch
        self._repository_factory = repository_factory
        self._cancel = threading.Event()

    def request_cancel(self) -> None:
        self._cancel.set()

    def _result(self, source_root: Path, target_root: Path, items: tuple[ImportItemResult, ...] = ()) -> ImportRunResult:
        return ImportRunResult(
            source_root,
            target_root,
            items,
            sum(item.status == "success" for item in items),
            0,
            0,
            sum(item.status == "failed" for item in items),
            action="undo",
            mode=_validate_mode(self._batch.get("mode")),
        )

    @staticmethod
    def _path_key(path: Path) -> str:
        return os.path.normcase(os.path.normpath(os.path.abspath(os.fspath(path))))

    def _validated_batch(self) -> tuple[Path, Path, list[dict[str, object]]]:
        _validate_mode(self._batch.get("mode"))
        if self._batch.get("complete") is not True or self._batch.get("undone_at") is not None:
            raise SafeImportError("只有尚未撤销的完整成功批次可以撤销")
        source_root = Path(str(self._batch["source_root"]))
        target_root = Path(str(self._batch["target_root"]))
        raw_items = self._batch.get("items")
        if not source_root.is_absolute() or not target_root.is_absolute() or not isinstance(raw_items, list):
            raise SafeImportError("导入历史路径损坏")
        _validate_root(source_root, label="历史源目录")
        _validate_root(target_root, label="历史目标目录")
        if _within_root(target_root, source_root) or _within_root(source_root, target_root):
            raise SafeImportError("导入历史根目录边界损坏")
        if self._cancel.is_set():
            raise InterruptedError("用户取消撤销")
        if self._batch.get("terminal_status", "completed") != "completed":
            raise SafeImportError("失败或取消的导入批次不能撤销")
        if any(
            not isinstance(item, dict)
            or item.get("status") not in {"success", "duplicate", "conflict"}
            for item in raw_items
        ):
            raise SafeImportError("只有无失败项目的完成批次可以撤销")
        items = [item for item in raw_items if item.get("status") == "success"]
        if not items:
            raise SafeImportError("导入批次没有可撤销的成功项目")
        seen_sources: set[str] = set()
        seen_targets: set[str] = set()
        for item in items:
            if self._cancel.is_set():
                raise InterruptedError("用户取消撤销")
            source = Path(str(item.get("source_path", "")))
            target = Path(str(item.get("target_path", "")))
            expected_hash = item.get("sha256")
            if (
                not source.is_absolute()
                or not target.is_absolute()
                or not _within_root(source, source_root)
                or not _within_root(target, target_root)
                or self._path_key(target.parent) != self._path_key(target_root)
                or source.name != target.name
            ):
                raise SafeImportError("导入历史项目路径超出记录根目录或映射损坏")
            if not isinstance(expected_hash, str) or len(expected_hash) != 64:
                raise SafeImportError("导入历史 SHA-256 损坏")
            try:
                bytes.fromhex(expected_hash)
            except ValueError as error:
                raise SafeImportError("导入历史 SHA-256 损坏") from error
            source_key = self._path_key(source)
            target_key = self._path_key(target)
            if source_key in seen_sources or target_key in seen_targets:
                raise SafeImportError("导入历史包含重复路径")
            seen_sources.add(source_key)
            seen_targets.add(target_key)
            with _locked_directory_chain(source_root, source.parent), _locked_directory_chain(
                target_root, target.parent
            ):
                target_identity = _file_identity(target)
                target_hash = _sha256(target)
                if (
                    os.path.lexists(source)
                    or _file_identity(target) != target_identity
                    or target_hash != expected_hash
                ):
                    raise SafeImportError(f"文件已变化，不能撤销：{target.name}")
            if self._cancel.is_set():
                raise InterruptedError("用户取消撤销")
        return source_root, target_root, items

    def _compensate(
        self,
        restored: list[tuple[Path, Path]],
        *,
        source_root: Path,
        target_root: Path,
    ) -> None:
        for source, target in reversed(restored):
            if source.exists() and not target.exists():
                moved = import_one(source, source_root=source_root, target_root=target_root)
                if moved.status != "success":
                    raise SafeImportError(f"撤销补偿失败：{source.name}")
        restored.clear()

    def run(self) -> None:
        restored: list[tuple[Path, Path]] = []
        source_root = Path(str(self._batch.get("source_root", "")))
        target_root = Path(str(self._batch.get("target_root", "")))
        try:
            source_root, target_root, items = self._validated_batch()
            if self._cancel.is_set():
                self.cancelled.emit(self._result(source_root, target_root))
                return
            for item in reversed(items):
                if self._cancel.is_set():
                    self._compensate(restored, source_root=source_root, target_root=target_root)
                    self.cancelled.emit(self._result(source_root, target_root))
                    return
                source = Path(str(item["source_path"]))
                target = Path(str(item["target_path"]))
                try:
                    moved = import_one(
                        target,
                        source_root=target_root,
                        target_root=source.parent,
                        cancel_event=self._cancel,
                        expected_sha256=str(item["sha256"]),
                    )
                except InterruptedError:
                    self._compensate(restored, source_root=source_root, target_root=target_root)
                    self.cancelled.emit(self._result(source_root, target_root))
                    return
                if moved.status != "success":
                    raise SafeImportError(f"撤销校验失败：{target.name}")
                restored.append((source, target))
                if moved.sha256 != item.get("sha256"):
                    self._compensate(restored, source_root=source_root, target_root=target_root)
                    raise SafeImportError(f"撤销校验失败：{target.name}")
                if self._cancel.is_set():
                    self._compensate(restored, source_root=source_root, target_root=target_root)
                    self.cancelled.emit(self._result(source_root, target_root))
                    return
            repository = self._repository_factory()
            try:
                history = _load_history(repository)
                matching = [entry for entry in history if entry.get("id") == self._batch.get("id")]
                if len(matching) != 1:
                    raise SafeImportError("导入历史已变化")
                matching[0]["undone_at"] = datetime.now(timezone.utc).isoformat()
                repository.set_setting(IMPORT_HISTORY_KEY, history)
            except Exception:
                self._compensate(restored, source_root=source_root, target_root=target_root)
                raise
            finally:
                repository.close()
            results = tuple(
                ImportItemResult(target, source, "success", "已撤销导入并恢复源路径", str(item["sha256"]))
                for item, (source, target) in zip(reversed(items), restored)
            )
            self.completed.emit(self._result(source_root, target_root, results))
        except InterruptedError:
            self.cancelled.emit(self._result(source_root, target_root))
        except Exception as error:
            if restored:
                try:
                    source_root = Path(str(self._batch["source_root"]))
                    target_root = Path(str(self._batch["target_root"]))
                    self._compensate(restored, source_root=source_root, target_root=target_root)
                except Exception as rollback_error:
                    self.failed.emit(f"撤销失败且补偿失败，需要人工处理：{rollback_error}")
                    return
            self.failed.emit(str(error).strip() or error.__class__.__name__)


class SafeImportController(QObject):
    completed = Signal(object)
    cancelled = Signal(object)
    failed = Signal(str)
    preview_ready = Signal(object)
    preview_cancelled = Signal()
    preview_failed = Signal(str)
    running_changed = Signal(bool)

    warning = Signal(str)

    def __init__(self, repository_factory=None, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._repository_factory = repository_factory
        self._worker: SafeImportPreviewWorker | SafeImportWorker | SafeImportUndoWorker | None = None
        self._terminal: tuple[str, object] | None = None
        self._current_plan: ImportPreviewPlan | None = None
        self._phase = "idle"

    @property
    def running(self) -> bool:
        return self._worker is not None

    @property
    def phase(self) -> str:
        return self._phase

    @property
    def current_plan(self) -> ImportPreviewPlan | None:
        return self._current_plan

    def start(self, source_root: Path, target_root: Path, mode: str) -> None:
        self.start_preview(source_root, target_root, mode)

    def start_preview(self, source_root: Path, target_root: Path, mode: str) -> None:
        if self.running:
            raise RuntimeError("已有安全导入任务正在运行")
        self._current_plan = None
        worker = SafeImportPreviewWorker(source_root=source_root, target_root=target_root, mode=mode)
        worker.completed.connect(lambda value: self._cache("completed", value))
        worker.cancelled.connect(lambda: self._cache("cancelled", None))
        worker.failed.connect(lambda value: self._cache("failed", value))
        worker.finished.connect(self._finished)
        self._worker = worker
        self._phase = "preview"
        self._terminal = None
        self.running_changed.emit(True)
        worker.start()

    def discard_preview(self) -> None:
        if self.running:
            raise RuntimeError("任务运行期间不能丢弃预览")
        self._current_plan = None

    def start_execute(self, plan_id: str) -> None:
        if self.running:
            raise RuntimeError("已有安全导入任务正在运行")
        plan = self._current_plan
        if plan is None or plan.id != plan_id:
            raise SafeImportError("预览已失效，请重新生成")
        if plan.ready_count <= 0:
            raise SafeImportError("预览中没有可执行项目")
        self._current_plan = None
        worker = SafeImportWorker(plan=plan)
        worker.completed.connect(lambda value: self._cache("completed", value))
        worker.cancelled.connect(lambda value: self._cache("cancelled", value))
        worker.failed.connect(lambda value: self._cache("failed", value))
        worker.finished.connect(self._finished)
        self._worker = worker
        self._phase = "execute"
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
        worker.cancelled.connect(lambda value: self._cache("cancelled", value))
        worker.failed.connect(lambda value: self._cache("failed", value))
        worker.finished.connect(self._finished)
        self._worker = worker
        self._phase = "undo"
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
        phase = self._phase
        audit_warning: str | None = None
        if phase == "preview" and kind == "completed" and isinstance(value, ImportPreviewPlan):
            self._current_plan = value
        if (
            phase == "execute"
            and isinstance(value, ImportRunResult)
            and value.action == "import"
            and self._repository_factory is not None
        ):
            try:
                with self._repository_factory() as repository:
                    history = _load_history(repository)
                    history = (history + [_result_to_history(value)])[-200:]
                    repository.set_setting(IMPORT_HISTORY_KEY, history)
            except Exception as error:
                audit_warning = f"文件操作已结束，但历史保存失败：{error}"
        self._worker = None
        self._terminal = None
        self._phase = "idle"
        worker.deleteLater()
        self.running_changed.emit(False)
        if phase == "preview":
            if kind == "completed":
                self.preview_ready.emit(value)
            elif kind == "cancelled":
                self.preview_cancelled.emit()
            else:
                self.preview_failed.emit(str(value))
        elif kind == "completed":
            self.completed.emit(value)
        elif kind == "cancelled":
            self.cancelled.emit(value)
        else:
            if isinstance(value, ImportRunResult):
                self.failed.emit(value.terminal_message or "安全导入失败")
            else:
                self.failed.emit(str(value))
        if audit_warning:
            self.warning.emit(audit_warning)
