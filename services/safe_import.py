"""P6 verified move-import for explicitly selected roots."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
import threading
import time
from typing import Iterator
from uuid import uuid4

from PySide6.QtCore import QObject, QThread, Signal

from database import DatabaseConfig
from services.file_safety import _is_reparse, _locked_directory_chain, _within_root
from repositories import LibraryRepository, RepositoryCommitOutcomeUnknown


SUPPORTED_AUDIO = {".mp3", ".flac", ".wav", ".m4a", ".ogg", ".aac"}
_CANDIDATE_PREFIX = ".musicctrl-import-"
IMPORT_HISTORY_KEY = "p6.import_history"
PENDING_IMPORT_KEY = "p6.pending_import"
_JOURNAL_VERSION = 1
_JOURNAL_STATES = {
    "planned", "candidate_ready", "target_placed", "source_deleted", "done"
}


class SafeImportError(RuntimeError):
    pass


class _JournalPersistenceError(SafeImportError):
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
    batch_id = result.plan_id or str(uuid4())
    return {
        "id": batch_id,
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


def _candidate_path(target_root: Path, batch_id: str, index: int) -> Path:
    return target_root / f"{_CANDIDATE_PREFIX}{batch_id}-{index}.tmp"


def _journal_for_plan(plan: ImportPreviewPlan) -> dict[str, object]:
    if not isinstance(plan.id, str) or not plan.id:
        raise SafeImportError("导入批次编号无效")
    _validate_roots(plan.source_root, plan.target_root)
    seen_source: set[str] = set()
    seen_target: set[str] = set()
    items: list[dict[str, object]] = []
    for index, item in enumerate(plan.items):
        source_key = _path_key(item.source_path)
        target_key = _path_key(item.target_path)
        if (
            not item.source_path.is_absolute()
            or not item.target_path.is_absolute()
            or not _within_root(item.source_path, plan.source_root)
            or not _within_root(item.target_path, plan.target_root)
            or source_key in seen_source
            or target_key in seen_target
            or (
                item.status != "failed"
                and len(item.expected_sha256) != 64
            )
        ):
            raise SafeImportError("导入计划包含越界、重复或损坏项目")
        seen_source.add(source_key)
        seen_target.add(target_key)
        state = "planned" if item.status == "ready" else "done"
        items.append({
            "source_path": str(item.source_path),
            "target_path": str(item.target_path),
            "candidate_path": str(_candidate_path(plan.target_root, plan.id, index)),
            "preview_status": item.status,
            "state": state,
            "sha256": item.expected_sha256 or None,
            "size_bytes": item.expected_size_bytes if item.expected_size_bytes >= 0 else None,
            "mtime_ns": item.expected_mtime_ns if item.expected_mtime_ns >= 0 else None,
            "message": item.message,
        })
    return {
        "version": _JOURNAL_VERSION,
        "batch_id": plan.id,
        "mode": _validate_mode(plan.mode),
        "source_root": str(plan.source_root),
        "target_root": str(plan.target_root),
        "created_at": plan.created_at,
        "items": items,
    }


def _validate_journal(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or value.get("version") != _JOURNAL_VERSION:
        raise SafeImportError("待恢复导入日志版本或格式损坏")
    batch_id = value.get("batch_id")
    raw_items = value.get("items")
    source_root = Path(str(value.get("source_root", "")))
    target_root = Path(str(value.get("target_root", "")))
    _validate_mode(value.get("mode"))
    if (
        not isinstance(batch_id, str)
        or not batch_id
        or not isinstance(raw_items, list)
        or not source_root.is_absolute()
        or not target_root.is_absolute()
    ):
        raise SafeImportError("待恢复导入日志字段损坏")
    _validate_roots(source_root, target_root)
    seen_source: set[str] = set()
    seen_target: set[str] = set()
    seen_candidate: set[str] = set()
    for index, item in enumerate(raw_items):
        if not isinstance(item, dict) or item.get("state") not in _JOURNAL_STATES:
            raise SafeImportError("待恢复导入项目状态损坏")
        source = Path(str(item.get("source_path", "")))
        target = Path(str(item.get("target_path", "")))
        candidate = Path(str(item.get("candidate_path", "")))
        digest = item.get("sha256")
        size = item.get("size_bytes")
        mtime_ns = item.get("mtime_ns")
        preview_status = item.get("preview_status")
        if (
            not source.is_absolute()
            or not target.is_absolute()
            or not candidate.is_absolute()
            or not _within_root(source, source_root)
            or not _within_root(target, target_root)
            or not _within_root(candidate, target_root)
            or _path_key(target) != _path_key(target_root / source.name)
            or _path_key(target.parent) != _path_key(target_root)
            or _path_key(candidate.parent) != _path_key(target_root)
            or _path_key(candidate) != _path_key(_candidate_path(target_root, batch_id, index))
            or preview_status not in {"ready", "duplicate", "conflict", "failed"}
            or (preview_status != "ready" and item.get("state") != "done")
            or (
                preview_status != "failed"
                and (
                    not isinstance(digest, str)
                    or len(digest) != 64
                    or any(character not in "0123456789abcdefABCDEF" for character in digest)
                    or not isinstance(size, int)
                    or isinstance(size, bool)
                    or size < 0
                    or not isinstance(mtime_ns, int)
                    or isinstance(mtime_ns, bool)
                    or mtime_ns < 0
                )
            )
            or (
                preview_status == "failed"
                and (digest is not None or size is not None or mtime_ns is not None)
            )
        ):
            raise SafeImportError("待恢复导入项目路径或校验值损坏")
        keys = (_path_key(source), _path_key(target), _path_key(candidate))
        if keys[0] in seen_source or keys[1] in seen_target or keys[2] in seen_candidate:
            raise SafeImportError("待恢复导入日志包含重复路径")
        seen_source.add(keys[0])
        seen_target.add(keys[1])
        seen_candidate.add(keys[2])
    return dict(value)


def _finalize_with_readback(
    repository: LibraryRepository,
    repository_factory,
    *,
    batch_id: str,
    entry: dict[str, object],
) -> None:
    try:
        repository.finalize_import_journal(
            pending_key=PENDING_IMPORT_KEY,
            history_key=IMPORT_HISTORY_KEY,
            batch_id=batch_id,
            history_entry=entry,
        )
    except RepositoryCommitOutcomeUnknown:
        with repository_factory() as readback:
            pending, matching = readback.read_import_finalize_state(
                pending_key=PENDING_IMPORT_KEY,
                history_key=IMPORT_HISTORY_KEY,
                batch_id=batch_id,
            )
        if pending is None and _canonical_json(matching) == _canonical_json(entry):
            return
        raise


def _canonical_json(value: object) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


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
    candidate_path: Path | None = None,
    stage_callback=None,
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
            if candidate_path is None:
                handle = tempfile.NamedTemporaryFile(
                    mode="wb",
                    prefix=_CANDIDATE_PREFIX,
                    suffix=".tmp",
                    dir=target_root,
                    delete=False,
                )
                candidate = Path(handle.name)
            else:
                if (
                    not candidate_path.is_absolute()
                    or not _within_root(candidate_path, target_root)
                    or candidate_path.parent != target_root
                    or not candidate_path.name.startswith(_CANDIDATE_PREFIX)
                    or os.path.lexists(candidate_path)
                ):
                    raise SafeImportError("导入候选路径无效或已存在")
                candidate = candidate_path
                handle = candidate.open("xb")
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
            if stage_callback is not None:
                stage_callback("candidate_ready")
            if target.exists():
                raise SafeImportError("目标在导入期间出现，禁止覆盖")
            os.rename(candidate, target)
            candidate = None
            if stage_callback is not None:
                try:
                    stage_callback("target_placed")
                except Exception:
                    os.unlink(target)
                    raise
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
            if stage_callback is not None:
                stage_callback("source_deleted")
                stage_callback("done")
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

    def __init__(self, *, plan: ImportPreviewPlan, repository_factory, parent=None) -> None:
        super().__init__(parent)
        self._plan = plan
        self._repository_factory = repository_factory
        self._cancel = threading.Event()

    def request_cancel(self) -> None:
        self._cancel.set()

    def run(self) -> None:
        results: list[ImportItemResult] = []
        repository: LibraryRepository | None = None
        try:
            _validate_roots(self._plan.source_root, self._plan.target_root)
            journal = _journal_for_plan(self._plan)
            repository = self._repository_factory()
            # The complete plan is durable before the first candidate, rename, or unlink.
            repository.create_import_journal(
                pending_key=PENDING_IMPORT_KEY,
                batch_id=self._plan.id,
                journal=journal,
            )
            for index, item in enumerate(self._plan.items):
                if item.status != "ready":
                    results.append(ImportItemResult(
                        item.source_path, item.target_path, item.status, item.message,
                        item.expected_sha256 or None,
                    ))
                    continue
                if self._cancel.is_set():
                    self._append_unstarted(results, index, "cancelled", "用户取消，未执行")
                    cancelled = self._result(results, "cancelled", "用户取消导入")
                    self._finalize(repository, cancelled)
                    self.cancelled.emit(cancelled)
                    return
                try:
                    def advance(state: str, *, item_index: int = index) -> None:
                        raw_items = journal["items"]
                        assert isinstance(raw_items, list)
                        current = raw_items[item_index]
                        assert isinstance(current, dict)
                        current["state"] = state
                        try:
                            repository.set_setting(PENDING_IMPORT_KEY, journal)
                        except Exception as error:
                            raise _JournalPersistenceError(
                                f"导入恢复日志推进失败：{error}"
                            ) from error

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
                        candidate_path=_candidate_path(
                            self._plan.target_root, self._plan.id, index
                        ),
                        stage_callback=advance,
                    )
                except InterruptedError:
                    self._append_unstarted(results, index, "cancelled", "用户取消，未执行")
                    cancelled = self._result(results, "cancelled", "用户取消导入")
                    self._finalize(repository, cancelled)
                    self.cancelled.emit(cancelled)
                    return
                except _JournalPersistenceError:
                    raise
                except Exception as error:
                    raw_items = journal["items"]
                    assert isinstance(raw_items, list)
                    current = raw_items[index]
                    assert isinstance(current, dict)
                    candidate = _candidate_path(self._plan.target_root, self._plan.id, index)
                    target = self._plan.target_root / item.source_path.name
                    if (
                        current.get("state") != "planned"
                        or os.path.lexists(candidate)
                    ):
                        raise _JournalPersistenceError(
                            f"导入中断且磁盘可能存在待恢复状态：{error}"
                        ) from error
                    result = ImportItemResult(
                        item.source_path, item.target_path, "failed",
                        str(error).strip() or error.__class__.__name__, item.expected_sha256,
                    )
                results.append(result)
            final = self._result(results, "completed", "")
            self._finalize(repository, final)
            self.completed.emit(final)
        except Exception as error:
            self._append_unstarted(results, len(results), "failed", "执行未完成")
            failed = self._result(
                results, "failed", str(error).strip() or error.__class__.__name__
            )
            self.failed.emit(failed)
        finally:
            if repository is not None:
                try:
                    repository.close()
                except Exception:
                    pass

    def _finalize(self, repository: LibraryRepository, result: ImportRunResult) -> None:
        entry = _result_to_history(result)
        _finalize_with_readback(
            repository,
            self._repository_factory,
            batch_id=self._plan.id,
            entry=entry,
        )

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


class SafeImportRecoveryWorker(QThread):
    """Reconcile one durable import journal without ever deleting a source file."""

    completed = Signal(object)
    failed = Signal(str)

    def __init__(self, *, repository_factory, parent=None) -> None:
        super().__init__(parent)
        self._repository_factory = repository_factory
        self._cancel = threading.Event()

    def request_cancel(self) -> None:
        self._cancel.set()

    @staticmethod
    def _inspect(
        path: Path,
        root: Path,
        digest: str,
        size: int,
        cancel_event: threading.Event,
    ) -> tuple[bool, tuple[int, int, int, int] | None]:
        if not os.path.lexists(path):
            return False, None
        if os.name != "nt":
            return False, None
        try:
            import ctypes
            from ctypes import wintypes
            import msvcrt

            with _locked_directory_chain(root, path.parent):
                metadata = os.lstat(path)
                if stat.S_ISLNK(metadata.st_mode) or _is_reparse(metadata):
                    return False, None
                identity = (
                    int(metadata.st_dev), int(metadata.st_ino),
                    int(metadata.st_size), int(metadata.st_mtime_ns),
                )
                if not stat.S_ISREG(metadata.st_mode):
                    return False, None
                kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
                create_file = kernel32.CreateFileW
                create_file.argtypes = (
                    wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                    wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD,
                    wintypes.HANDLE,
                )
                create_file.restype = wintypes.HANDLE
                close_handle = kernel32.CloseHandle
                close_handle.argtypes = (wintypes.HANDLE,)
                raw_handle = create_file(
                    os.fspath(path),
                    0x80000000,  # GENERIC_READ
                    0x00000001,  # share read only: block writes/deletes/renames
                    None,
                    3,
                    0x00200000 | 0x08000000,
                    None,
                )
                invalid_handle = ctypes.c_void_p(-1).value
                if raw_handle == invalid_handle:
                    return False, None
                fd = -1
                try:
                    fd = msvcrt.open_osfhandle(int(raw_handle), os.O_RDONLY | os.O_BINARY)
                    raw_handle = invalid_handle
                    with os.fdopen(fd, "rb", closefd=True) as handle:
                        fd = -1
                        opened = os.fstat(handle.fileno())
                        opened_identity = (
                            int(opened.st_dev), int(opened.st_ino),
                            int(opened.st_size), int(opened.st_mtime_ns),
                        )
                        if opened_identity != identity:
                            return False, None
                        hasher = hashlib.sha256()
                        while chunk := handle.read(1024 * 1024):
                            if cancel_event.is_set():
                                raise InterruptedError("用户取消导入恢复")
                            hasher.update(chunk)
                        after = os.fstat(handle.fileno())
                        after_identity = (
                            int(after.st_dev), int(after.st_ino),
                            int(after.st_size), int(after.st_mtime_ns),
                        )
                        latest = os.lstat(path)
                        latest_identity = (
                            int(latest.st_dev), int(latest.st_ino),
                            int(latest.st_size), int(latest.st_mtime_ns),
                        )
                        good = (
                            after_identity == identity
                            and latest_identity == identity
                            and opened.st_size == size
                            and hasher.hexdigest() == digest
                        )
                        return good, identity
                finally:
                    if fd >= 0:
                        os.close(fd)
                    if raw_handle != invalid_handle:
                        close_handle(raw_handle)
        except (OSError, SafeImportError):
            return False, None

    @staticmethod
    def _remove_owned(
        path: Path,
        root: Path,
        *,
        expected_identity: tuple[int, int, int, int] | None,
        expected_digest: str,
        cancel_event: threading.Event,
        allow_any_regular_content: bool = False,
    ) -> None:
        """Delete only the exact regular file inspected for this journal item."""

        if os.name != "nt":
            raise SafeImportError("当前平台不能证明候选文件删除身份，已停止自动恢复")
        import ctypes
        from ctypes import wintypes
        import msvcrt

        with _locked_directory_chain(root, path.parent):
            if cancel_event.is_set():
                raise InterruptedError("用户取消导入恢复")
            metadata = os.lstat(path)
            identity = (
                int(metadata.st_dev), int(metadata.st_ino),
                int(metadata.st_size), int(metadata.st_mtime_ns),
            )
            if (
                not stat.S_ISREG(metadata.st_mode)
                or stat.S_ISLNK(metadata.st_mode)
                or _is_reparse(metadata)
                or identity != expected_identity
            ):
                raise SafeImportError("待清理文件身份已变化，已停止自动恢复")

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            create_file = kernel32.CreateFileW
            create_file.argtypes = (
                wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
            )
            create_file.restype = wintypes.HANDLE
            close_handle = kernel32.CloseHandle
            close_handle.argtypes = (wintypes.HANDLE,)
            set_info = kernel32.SetFileInformationByHandle
            set_info.argtypes = (
                wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD,
            )
            set_info.restype = wintypes.BOOL

            class FileDispositionInfo(ctypes.Structure):
                _fields_ = [("DeleteFile", wintypes.BOOL)]

            raw_handle = create_file(
                os.fspath(path),
                0x80000000 | 0x00010000,  # GENERIC_READ | DELETE
                0x00000001,  # share read only: block write/delete/rename swaps
                None,
                3,  # OPEN_EXISTING
                0x00200000 | 0x08000000,  # OPEN_REPARSE_POINT | SEQUENTIAL_SCAN
                None,
            )
            invalid_handle = ctypes.c_void_p(-1).value
            if raw_handle == invalid_handle:
                raise SafeImportError(
                    f"无法锁定待清理候选文件（Windows 错误 {ctypes.get_last_error()}）"
                )
            fd = -1
            try:
                fd = msvcrt.open_osfhandle(int(raw_handle), os.O_RDONLY | os.O_BINARY)
                raw_handle = invalid_handle  # descriptor now owns the native handle
                with os.fdopen(fd, "rb", closefd=True) as handle:
                    fd = -1
                    handle_meta = os.fstat(handle.fileno())
                    handle_identity = (
                        int(handle_meta.st_dev), int(handle_meta.st_ino),
                        int(handle_meta.st_size), int(handle_meta.st_mtime_ns),
                    )
                    if handle_identity != identity:
                        raise SafeImportError("待清理文件在打开时发生变化，已停止自动恢复")
                    if not allow_any_regular_content:
                        digest = hashlib.sha256()
                        while chunk := handle.read(1024 * 1024):
                            if cancel_event.is_set():
                                raise InterruptedError("用户取消导入恢复")
                            digest.update(chunk)
                        if digest.hexdigest() != expected_digest:
                            raise SafeImportError("候选文件内容已变化，已停止自动恢复")
                    latest = os.lstat(path)
                    latest_identity = (
                        int(latest.st_dev), int(latest.st_ino),
                        int(latest.st_size), int(latest.st_mtime_ns),
                    )
                    if latest_identity != identity or cancel_event.is_set():
                        raise SafeImportError("待清理路径已变化或恢复已取消")
                    disposition = FileDispositionInfo(True)
                    os_handle = wintypes.HANDLE(msvcrt.get_osfhandle(handle.fileno()))
                    if not set_info(
                        os_handle, 4, ctypes.byref(disposition), ctypes.sizeof(disposition)
                    ):
                        raise SafeImportError(
                            f"候选文件安全删除失败（Windows 错误 {ctypes.get_last_error()}）"
                        )
            finally:
                if fd >= 0:
                    os.close(fd)
                if raw_handle != invalid_handle:
                    close_handle(raw_handle)

    def run(self) -> None:
        repository: LibraryRepository | None = None
        try:
            repository = self._repository_factory()
            setting = repository.get_setting(PENDING_IMPORT_KEY)
            if setting is None:
                self.completed.emit(None)
                return
            journal = _validate_journal(setting.value)
            batch_id = str(journal["batch_id"])
            _, existing = repository.read_import_finalize_state(
                pending_key=PENDING_IMPORT_KEY,
                history_key=IMPORT_HISTORY_KEY,
                batch_id=batch_id,
            )
            if existing is not None:
                _finalize_with_readback(
                    repository,
                    self._repository_factory,
                    batch_id=batch_id,
                    entry=existing,
                )
                self.completed.emit(existing)
                return
            source_root = Path(str(journal["source_root"]))
            target_root = Path(str(journal["target_root"]))
            raw_items = journal["items"]
            assert isinstance(raw_items, list)
            results: list[ImportItemResult] = []
            manual: list[str] = []
            ready_success = 0
            ready_total = 0
            for raw in raw_items:
                if self._cancel.is_set():
                    raise InterruptedError("用户取消导入恢复，日志已保留")
                assert isinstance(raw, dict)
                source = Path(str(raw["source_path"]))
                target = Path(str(raw["target_path"]))
                candidate = Path(str(raw["candidate_path"]))
                preview_status = str(raw["preview_status"])
                if preview_status != "ready":
                    results.append(ImportItemResult(
                        source, target, preview_status, str(raw.get("message", "")),
                        None if raw.get("sha256") is None else str(raw["sha256"]),
                    ))
                    continue
                digest = str(raw["sha256"])
                size = int(raw["size_bytes"])
                ready_total += 1
                state = str(raw["state"])
                source_good, _ = self._inspect(
                    source, source_root, digest, size, self._cancel
                )
                target_good, target_identity = self._inspect(
                    target, target_root, digest, size, self._cancel
                )
                candidate_good, candidate_identity = self._inspect(
                    candidate, target_root, digest, size, self._cancel
                )
                candidate_exists = os.path.lexists(candidate)
                target_exists = os.path.lexists(target)
                source_exists = os.path.lexists(source)
                if (
                    state in {"planned", "candidate_ready"}
                    and source_good
                    and candidate_exists
                    and (state == "planned" or candidate_good)
                    and not target_exists
                ):
                    self._remove_owned(
                        candidate,
                        target_root,
                        expected_identity=candidate_identity,
                        expected_digest=digest,
                        cancel_event=self._cancel,
                        allow_any_regular_content=(state == "planned" and not candidate_good),
                    )
                    results.append(ImportItemResult(
                        source, target, "failed", "已删除未落位候选并保留源文件", digest
                    ))
                elif (
                    state in {"candidate_ready", "target_placed"}
                    and source_good
                    and target_good
                    and not candidate_exists
                ):
                    results.append(ImportItemResult(
                        source, target, "failed",
                        "源文件与目标副本均存在；为避免误删已全部保留", digest
                    ))
                elif (
                    state in {"target_placed", "source_deleted", "done"}
                    and (not source_exists)
                    and target_good
                    and not candidate_exists
                ):
                    ready_success += 1
                    results.append(ImportItemResult(
                        source, target, "success", "已确认源删除且目标校验通过", digest
                    ))
                elif (
                    state == "planned"
                    and source_good
                    and not target_exists
                    and not candidate_exists
                ):
                    results.append(ImportItemResult(
                        source, target, "failed", "文件操作尚未开始，源文件保持不变", digest
                    ))
                elif (
                    state in {"candidate_ready", "target_placed"}
                    and source_good
                    and not target_exists
                    and not candidate_exists
                ):
                    results.append(ImportItemResult(
                        source, target, "failed", "目标副本已回滚，源文件保持不变", digest
                    ))
                else:
                    manual.append(source.name)
                    results.append(ImportItemResult(
                        source, target, "failed", "磁盘状态不明确，需要人工处理", digest
                    ))
            if manual:
                self.failed.emit("导入恢复需要人工处理，日志已保留：" + "、".join(manual))
                return
            terminal = (
                "completed"
                if ready_total > 0
                and ready_success == ready_total
                and all(item.status in {"success", "duplicate", "conflict"} for item in results)
                else "failed"
            )
            result = ImportRunResult(
                source_root,
                target_root,
                tuple(results),
                ready_success,
                sum(item.status == "duplicate" for item in results),
                sum(item.status == "conflict" for item in results),
                sum(item.status == "failed" for item in results),
                mode=str(journal["mode"]),
                terminal_status=terminal,
                terminal_message="" if terminal == "completed" else "导入已安全回滚或需要重新执行",
                plan_id=batch_id,
            )
            entry = _result_to_history(result)
            _finalize_with_readback(
                repository,
                self._repository_factory,
                batch_id=batch_id,
                entry=entry,
            )
            self.completed.emit(result)
        except Exception as error:
            self.failed.emit(str(error).strip() or error.__class__.__name__)
        finally:
            if repository is not None:
                try:
                    repository.close()
                except Exception:
                    pass


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
        self._ephemeral_repository_dir = None
        if repository_factory is None:
            self._ephemeral_repository_dir = tempfile.TemporaryDirectory(
                prefix="musicctrl-import-controller-"
            )
            config = DatabaseConfig(
                Path(self._ephemeral_repository_dir.name) / "library.sqlite3"
            )
            repository_factory = lambda: LibraryRepository(config)
        self._repository_factory = repository_factory
        self._worker: (
            SafeImportPreviewWorker
            | SafeImportWorker
            | SafeImportRecoveryWorker
            | SafeImportUndoWorker
            | None
        ) = None
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
        worker = SafeImportWorker(plan=plan, repository_factory=self._repository_factory)
        worker.completed.connect(lambda value: self._cache("completed", value))
        worker.cancelled.connect(lambda value: self._cache("cancelled", value))
        worker.failed.connect(lambda value: self._cache("failed", value))
        worker.finished.connect(self._finished)
        self._worker = worker
        self._phase = "execute"
        self._terminal = None
        self.running_changed.emit(True)
        worker.start()

    def start_recovery(self) -> None:
        if self.running:
            raise RuntimeError("已有安全导入任务正在运行")
        worker = SafeImportRecoveryWorker(repository_factory=self._repository_factory)
        worker.completed.connect(lambda value: self._cache("completed", value))
        worker.failed.connect(lambda value: self._cache("failed", value))
        worker.finished.connect(self._finished)
        self._worker = worker
        self._phase = "recovery"
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
        if phase == "preview" and kind == "completed" and isinstance(value, ImportPreviewPlan):
            self._current_plan = value
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
