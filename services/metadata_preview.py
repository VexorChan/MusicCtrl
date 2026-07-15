"""Read-only Title/Artist analysis and editable rename preview support."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
import os
from pathlib import Path
import stat
import threading
from typing import BinaryIO

from mutagen import File as MutagenFile
from PySide6.QtCore import QObject, QThread, Signal, Slot


SUPPORTED_EXTENSIONS = frozenset({".mp3", ".flac", ".wav", ".m4a", ".ogg", ".aac"})
FILE_STATES = frozenset({"active", "missing", "external_changed"})
_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x0400)
_INVALID_TRANSLATION = str.maketrans(
    {"<": "＜", ">": "＞", ":": "：", '"': "＂", "/": "／", "\\": "＼", "|": "｜", "?": "？", "*": "＊"}
)
_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{index}" for index in range(1, 10)}
    | {f"LPT{index}" for index in range(1, 10)}
)


class MetadataPreviewError(RuntimeError):
    """Base error for invalid or unsafe preview requests."""


class MetadataPreviewCancelled(MetadataPreviewError):
    """Raised at a cooperative cancellation checkpoint."""


@dataclass(frozen=True, slots=True)
class MetadataPreviewInput:
    asset_id: str
    canonical_path: Path
    allowed_root: Path
    file_state: str
    size_bytes: int
    mtime_ns: int | None


@dataclass(frozen=True, slots=True)
class MetadataPreviewResult:
    asset_id: str
    canonical_path: Path
    original_name: str
    suggested_stem: str | None
    extension: str
    title: str | None
    artist: str | None
    source: str
    status: str
    message: str
    requires_confirmation: bool


CancelCallback = Callable[[], bool]


def _check_cancelled(cancel_requested: CancelCallback | None) -> None:
    if cancel_requested is not None and cancel_requested():
        raise MetadataPreviewCancelled("元数据分析已取消")


def _path_key(path: Path) -> str:
    return os.path.normcase(os.path.normpath(os.path.abspath(os.fspath(path))))


def _windows_name_key(name: str) -> str:
    """Approximate the Windows filename equivalence used for rename conflicts."""

    return name.rstrip(" .").casefold()


def _within_root(path: Path, root: Path) -> bool:
    try:
        return os.path.commonpath((_path_key(path), _path_key(root))) == _path_key(root)
    except ValueError:
        return False


def _is_reparse(metadata: os.stat_result) -> bool:
    return bool(int(getattr(metadata, "st_file_attributes", 0)) & _REPARSE_POINT)


def _validate_request(
    items: Sequence[MetadataPreviewInput],
) -> None:
    asset_ids: set[str] = set()
    path_keys: set[str] = set()
    validated_roots: set[str] = set()
    for item in items:
        if not isinstance(item, MetadataPreviewInput):
            raise MetadataPreviewError("分析输入必须使用 MetadataPreviewInput")
        if not isinstance(item.asset_id, str) or not item.asset_id.strip():
            raise MetadataPreviewError("asset_id 必须是非空字符串")
        if item.asset_id in asset_ids:
            raise MetadataPreviewError(f"分析批次包含重复 asset_id：{item.asset_id}")
        asset_ids.add(item.asset_id)
        if not isinstance(item.canonical_path, Path) or not item.canonical_path.is_absolute():
            raise MetadataPreviewError("canonical_path 必须是绝对 Path")
        if not isinstance(item.allowed_root, Path) or not item.allowed_root.is_absolute():
            raise MetadataPreviewError("allowed_root 必须是绝对 Path")
        root_key = _path_key(item.allowed_root)
        if root_key not in validated_roots:
            try:
                root_metadata = os.lstat(item.allowed_root)
            except OSError as error:
                raise MetadataPreviewError(
                    f"无法读取允许根目录：{item.allowed_root}"
                ) from error
            if (
                not stat.S_ISDIR(root_metadata.st_mode)
                or stat.S_ISLNK(root_metadata.st_mode)
                or _is_reparse(root_metadata)
            ):
                raise MetadataPreviewError("允许根目录必须是普通目录，不能是链接或重解析点")
            validated_roots.add(root_key)
        key = _path_key(item.canonical_path)
        if key in path_keys:
            raise MetadataPreviewError(f"分析批次包含重复路径：{item.canonical_path}")
        path_keys.add(key)
        if not _within_root(item.canonical_path, item.allowed_root):
            raise MetadataPreviewError(f"分析路径超出允许根目录：{item.canonical_path}")
        if item.canonical_path.suffix.casefold() not in SUPPORTED_EXTENSIONS:
            raise MetadataPreviewError(f"不支持的音频格式：{item.canonical_path.suffix}")
        if item.file_state not in FILE_STATES:
            raise MetadataPreviewError(f"未知文件状态：{item.file_state}")
        if isinstance(item.size_bytes, bool) or not isinstance(item.size_bytes, int) or item.size_bytes < 0:
            raise MetadataPreviewError("size_bytes 必须是非负整数")
        if item.mtime_ns is not None and (
            isinstance(item.mtime_ns, bool) or not isinstance(item.mtime_ns, int) or item.mtime_ns < 0
        ):
            raise MetadataPreviewError("mtime_ns 必须是非负整数或 None")


def _text_values(value: object) -> list[str]:
    frame_text = getattr(value, "text", None)
    if frame_text is not None:
        value = frame_text
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, Iterable):
        values = [item for item in value if isinstance(item, str)]
    else:
        values = []
    return [item.strip() for item in values if item.strip()]


def _tag_values(tags: object, keys: tuple[str, ...]) -> list[str]:
    getter = getattr(tags, "get", None)
    if not callable(getter):
        return []
    for key in keys:
        values = _text_values(getter(key))
        if values:
            return values
    return []


def _read_tags(stream: BinaryIO) -> tuple[str | None, str | None, str | None, bool]:
    try:
        media = MutagenFile(stream, easy=True)
        if media is None:
            return None, None, "无法识别音频容器，已整体回退到文件名", True
        tags = getattr(media, "tags", None)
        if tags is None:
            return None, None, "未找到 Title 与 Artist，已整体回退到文件名", False
        titles = _tag_values(tags, ("title", "TIT2", "\xa9nam"))
        artists = _tag_values(tags, ("artist", "TPE1", "\xa9ART", "aART"))
        if titles and artists:
            return titles[0], "、".join(artists), None, False
        return None, None, "Title 与 Artist 未同时有效，已整体回退到文件名", False
    except Exception as error:
        return None, None, f"标签不可读，已整体回退到文件名：{error}", True


def _is_reserved_component(value: str) -> bool:
    return value.rstrip(" .").casefold().upper() in _RESERVED_NAMES


def _sanitize_component(value: str) -> tuple[str, bool, str | None]:
    stripped = value.strip()
    unsafe_tail = value.endswith((" ", "."))
    translated = "".join(" " if ord(char) < 32 else char for char in stripped).translate(_INVALID_TRANSLATION)
    translated = translated.strip()
    if not translated:
        return translated, True, "名称为空"
    if unsafe_tail:
        return translated.rstrip(" ."), True, "名称包含尾点或尾空格"
    if _is_reserved_component(translated):
        return translated, True, "名称是 Windows 保留设备名"
    return translated, False, None


def _identity_from_file_name(path: Path) -> tuple[str | None, str | None, str | None]:
    stem = path.stem
    if "-" not in stem:
        return None, None, "文件名没有可唯一识别的最后一个半角 '-'"
    title, artist = stem.rsplit("-", 1)
    if not title.strip() or not artist.strip():
        return None, None, "文件名分隔符两侧必须都有内容"
    return title, artist, None


def _file_metadata(path: Path, allowed_root: Path) -> os.stat_result:
    relative = Path(os.path.relpath(path, allowed_root))
    cursor = allowed_root
    metadata = os.lstat(cursor)
    if stat.S_ISLNK(metadata.st_mode) or _is_reparse(metadata):
        raise MetadataPreviewError("允许根目录不能是符号链接或重解析点")
    for part in relative.parts:
        cursor /= part
        metadata = os.lstat(cursor)
        if stat.S_ISLNK(metadata.st_mode) or _is_reparse(metadata):
            raise MetadataPreviewError("路径链不能包含符号链接或重解析点")
    if not stat.S_ISREG(metadata.st_mode):
        raise MetadataPreviewError("索引路径不是普通文件")
    return metadata


def _file_identity(metadata: os.stat_result) -> tuple[int, int, int, int] | None:
    """Return a no-content identity signature, or None when identity is unreliable."""

    device = getattr(metadata, "st_dev", None)
    inode = getattr(metadata, "st_ino", None)
    if not all(isinstance(value, int) and not isinstance(value, bool) for value in (device, inode)):
        return None
    if inode == 0:
        return None
    return (
        int(device),
        int(inode),
        int(metadata.st_size),
        int(metadata.st_mtime_ns),
    )


def _failed_result(item: MetadataPreviewInput, message: str, *, status: str = "分析失败") -> MetadataPreviewResult:
    return MetadataPreviewResult(
        asset_id=item.asset_id,
        canonical_path=item.canonical_path,
        original_name=item.canonical_path.name,
        suggested_stem=None,
        extension=item.canonical_path.suffix,
        title=None,
        artist=None,
        source="未分析",
        status=status,
        message=message,
        requires_confirmation=True,
    )


def _analyze_one(
    item: MetadataPreviewInput,
    *,
    cancel_requested: CancelCallback | None,
) -> MetadataPreviewResult:
    _check_cancelled(cancel_requested)
    if item.file_state == "missing":
        return _failed_result(item, "文件已缺失，禁止读取标签", status="文件缺失")
    try:
        before = _file_metadata(item.canonical_path, item.allowed_root)
    except OSError as error:
        return _failed_result(item, f"无法读取文件信息：{error}")
    except MetadataPreviewError as error:
        return _failed_result(item, str(error))
    before_identity = _file_identity(before)
    if before_identity is None:
        return _failed_result(item, "当前文件系统无法提供可靠文件身份，必须人工确认")

    fingerprint_changed = before.st_size != item.size_bytes or (
        item.mtime_ns is not None and before.st_mtime_ns != item.mtime_ns
    )
    _check_cancelled(cancel_requested)
    try:
        with item.canonical_path.open("rb") as stream:
            opened_identity = _file_identity(os.fstat(stream.fileno()))
            if opened_identity is None or opened_identity != before_identity:
                return _failed_result(
                    item,
                    "文件在安全检查与只读打开之间发生变化，已拒绝读取",
                )
            tag_result = _read_tags(stream)
            _check_cancelled(cancel_requested)
            handle_after_identity = _file_identity(os.fstat(stream.fileno()))
            if handle_after_identity is None or handle_after_identity != opened_identity:
                return _failed_result(
                    item,
                    "已打开文件在标签分析期间发生变化，已拒绝使用本次结果",
                )
            try:
                after = _file_metadata(item.canonical_path, item.allowed_root)
            except (OSError, MetadataPreviewError) as error:
                return _failed_result(item, f"分析后无法复核文件：{error}")
            after_identity = _file_identity(after)
            if after_identity is None or after_identity != handle_after_identity:
                return _failed_result(
                    item,
                    "文件路径与已验证只读句柄不再指向同一对象，已拒绝使用本次结果",
                )
    except MetadataPreviewCancelled:
        raise
    except OSError as error:
        return _failed_result(item, f"无法安全打开音频文件：{error}")
    if len(tag_result) == 3:
        # Test adapters written against the original internal helper contract remain valid.
        title, artist, tag_message = tag_result
        container_unreadable = False
    else:
        title, artist, tag_message, container_unreadable = tag_result
    source = "标签"
    parse_message = tag_message
    if title is None or artist is None:
        title, artist, file_message = _identity_from_file_name(item.canonical_path)
        source = "文件名" if title is not None and artist is not None else "无法识别"
        parse_message = file_message or tag_message
    if title is None or artist is None:
        return MetadataPreviewResult(
            asset_id=item.asset_id,
            canonical_path=item.canonical_path,
            original_name=item.canonical_path.name,
            suggested_stem=None,
            extension=item.canonical_path.suffix,
            title=None,
            artist=None,
            source=source,
            status="待手动确认",
            message=parse_message or "无法唯一识别歌名和歌手",
            requires_confirmation=True,
        )

    safe_title, title_manual, title_message = _sanitize_component(title)
    safe_artist, artist_manual, artist_message = _sanitize_component(artist)
    suggested = f"{safe_title}-{safe_artist}" if safe_title and safe_artist else None
    manual = title_manual or artist_manual or suggested is None
    needs_confirmation = (
        manual
        or container_unreadable
        or fingerprint_changed
        or item.file_state == "external_changed"
    )
    if manual:
        status = "待手动确认"
        message = title_message or artist_message or "名称需要人工确认"
    elif item.file_state == "external_changed" or fingerprint_changed:
        status = "外部变化"
        message = "文件已重新只读分析，必须由用户再次确认"
    elif container_unreadable:
        status = "待手动确认"
        message = parse_message or "音频容器不可读，必须人工确认"
    else:
        status = "可预览"
        message = parse_message or "只读分析完成"
    return MetadataPreviewResult(
        asset_id=item.asset_id,
        canonical_path=item.canonical_path,
        original_name=item.canonical_path.name,
        suggested_stem=suggested,
        extension=item.canonical_path.suffix,
        title=title,
        artist=artist,
        source=source,
        status=status,
        message=message,
        requires_confirmation=needs_confirmation,
    )


def _target_key(result: MetadataPreviewResult) -> tuple[str, str] | None:
    if result.suggested_stem is None:
        return None
    return (
        _path_key(result.canonical_path.parent),
        _windows_name_key(result.suggested_stem + result.extension),
    )


def _conflict_result(result: MetadataPreviewResult, message: str) -> MetadataPreviewResult:
    return MetadataPreviewResult(
        asset_id=result.asset_id,
        canonical_path=result.canonical_path,
        original_name=result.original_name,
        suggested_stem=result.suggested_stem,
        extension=result.extension,
        title=result.title,
        artist=result.artist,
        source=result.source,
        status="冲突",
        message=message,
        requires_confirmation=True,
    )


def _apply_conflicts(results: list[MetadataPreviewResult]) -> tuple[MetadataPreviewResult, ...]:
    targets: dict[tuple[str, str], list[int]] = {}
    existing_by_parent: dict[str, set[str]] = {}
    parent_errors: set[str] = set()
    for index, result in enumerate(results):
        target = _target_key(result)
        if target is None:
            continue
        targets.setdefault(target, []).append(index)
        parent_key = _path_key(result.canonical_path.parent)
        if parent_key not in existing_by_parent:
            try:
                existing_by_parent[parent_key] = {
                    _windows_name_key(entry.name)
                    for entry in os.scandir(result.canonical_path.parent)
                }
            except OSError:
                existing_by_parent[parent_key] = set()
                parent_errors.add(parent_key)

    conflicted: set[int] = set()
    for target, indices in targets.items():
        if len(indices) > 1:
            conflicted.update(indices)
        for index in indices:
            result = results[index]
            parent_key, target_name_key = target
            if parent_key in parent_errors:
                conflicted.add(index)
                continue
            parent_existing = existing_by_parent.get(parent_key, set())
            source_name_key = _windows_name_key(result.canonical_path.name)
            if target_name_key in parent_existing and target_name_key != source_name_key:
                conflicted.add(index)
    for index in conflicted:
        result = results[index]
        parent_key = _path_key(result.canonical_path.parent)
        message = (
            "无法读取同目录文件，不能安全判断名称冲突"
            if parent_key in parent_errors
            else "建议目标与现有文件或本批其他建议发生 Windows 等价名称冲突"
        )
        results[index] = _conflict_result(result, message)
    return tuple(results)


def analyze_metadata(
    item: MetadataPreviewInput,
    *,
    cancel_requested: CancelCallback | None = None,
) -> MetadataPreviewResult:
    """Analyze one indexed asset without modifying it."""

    _validate_request((item,))
    return _analyze_one(
        item,
        cancel_requested=cancel_requested,
    )


def build_metadata_previews(
    items: Sequence[MetadataPreviewInput],
    *,
    cancel_requested: CancelCallback | None = None,
) -> tuple[MetadataPreviewResult, ...]:
    """Analyze a frozen input batch without changing files or persistent state."""

    frozen_items = tuple(items)
    _validate_request(frozen_items)
    results: list[MetadataPreviewResult] = []
    for item in frozen_items:
        _check_cancelled(cancel_requested)
        results.append(
            _analyze_one(
                item,
                cancel_requested=cancel_requested,
            )
        )
    _check_cancelled(cancel_requested)
    completed = _apply_conflicts(results)
    _check_cancelled(cancel_requested)
    return completed


class MetadataPreviewWorker(QThread):
    completed = Signal(object)
    cancelled = Signal(int)
    failed = Signal(str)

    def __init__(
        self,
        items: Sequence[MetadataPreviewInput],
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._items = tuple(items)
        self._cancel_event = threading.Event()
        self._lifecycle_lock = threading.Lock()
        self._started_once = False

    def start(self, priority=QThread.Priority.InheritPriority) -> None:
        with self._lifecycle_lock:
            if self._started_once:
                raise RuntimeError("MetadataPreviewWorker 是 one-shot，不能重复启动")
            self._started_once = True
        super().start(priority)

    def request_cancel(self) -> None:
        self._cancel_event.set()
        self.requestInterruption()

    def run(self) -> None:
        terminal_emitted = False
        try:
            results = build_metadata_previews(
                self._items,
                cancel_requested=self._cancel_event.is_set,
            )
            if self._cancel_event.is_set():
                raise MetadataPreviewCancelled("元数据分析已取消")
            self.completed.emit(results)
            terminal_emitted = True
        except MetadataPreviewCancelled:
            self.cancelled.emit(0)
            terminal_emitted = True
        except Exception as error:
            self.failed.emit(str(error).strip() or error.__class__.__name__)
            terminal_emitted = True
        finally:
            if not terminal_emitted:
                self.failed.emit("元数据分析线程未产生终态")


class MetadataPreviewController(QObject):
    results_ready = Signal(object)
    cancelled = Signal(int)
    failed = Signal(str)
    running_changed = Signal(bool)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._worker: MetadataPreviewWorker | None = None
        self._terminal: tuple[str, object] | None = None

    @property
    def running(self) -> bool:
        return self._worker is not None

    def start(self, items: Sequence[MetadataPreviewInput]) -> None:
        if self.running:
            raise RuntimeError("已有歌曲信息分析正在运行")
        frozen_items = tuple(items)
        if not frozen_items:
            raise MetadataPreviewError("没有可分析的音乐")
        _validate_request(frozen_items)
        worker = MetadataPreviewWorker(frozen_items)
        worker.completed.connect(self._cache_completed)
        worker.cancelled.connect(self._cache_cancelled)
        worker.failed.connect(self._cache_failed)
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

    @Slot(object)
    def _cache_completed(self, results: object) -> None:
        self._cache("completed", results)

    @Slot(int)
    def _cache_cancelled(self, count: int) -> None:
        self._cache("cancelled", count)

    @Slot(str)
    def _cache_failed(self, message: str) -> None:
        self._cache("failed", message)

    @Slot()
    def _worker_finished(self) -> None:
        worker = self._worker
        if worker is None:
            return
        terminal = self._terminal or ("failed", "元数据分析线程结束但没有终态")
        self._worker = None
        self._terminal = None
        worker.deleteLater()
        self.running_changed.emit(False)
        kind, payload = terminal
        if kind == "completed":
            self.results_ready.emit(payload)
        elif kind == "cancelled":
            self.cancelled.emit(int(payload))
        else:
            self.failed.emit(str(payload))
