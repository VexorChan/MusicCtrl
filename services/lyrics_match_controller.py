"""Background P4 lyrics indexing, matching and UI-thread coordination."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, replace
from pathlib import Path
import os
import stat
import threading
from uuid import uuid4

from PySide6.QtCore import QObject, QThread, Signal, Slot

from database import DatabaseConfig
from repositories import IndexBatchItem, LibraryRepository, LyricsMatchRecord
from services.lyrics_scanner import (
    AudioLyricsInput,
    LyricsFileEntry,
    LyricsMatchCandidate,
    LyricsScanCancelled,
    build_lyrics_candidates,
    detect_embedded_lyrics,
    iter_lrc_files,
)
from services.library_scan_controller import (
    AudioAssetSnapshot,
    RevalidatedAudioRecord,
    revalidate_audio_snapshots,
)
from services.file_safety import _is_reparse, _locked_directory_chain, _within_root


LAST_SUCCESSFUL_LYRICS_ROOT_KEY = "p4.last_successful_lyrics_root"
IGNORED_AUDIO_ASSET_IDS_KEY = "p4.ignored_audio_asset_ids"


@dataclass(frozen=True, slots=True)
class LyricsReviewItem:
    token: str
    audio_asset_id: str
    audio_label: str
    lyric_asset_id: str | None
    lyric_path: Path | None
    source_kind: str
    confidence: int
    status: str
    requires_confirmation: bool
    message: str
    has_current_match: bool = False
    ignored: bool = False
    audio_path: Path | None = None
    audio_root: Path | None = None
    audio_size_bytes: int | None = None
    audio_mtime_ns: int | None = None
    lyric_root: Path | None = None
    lyric_size_bytes: int | None = None
    lyric_mtime_ns: int | None = None


@dataclass(frozen=True, slots=True)
class LyricsScanResult:
    root: Path
    indexed_count: int
    automatic_count: int
    items: tuple[LyricsReviewItem, ...]


@dataclass(frozen=True, slots=True)
class LyricsBatchInput:
    token: str
    audio_asset_id: str
    audio_path: Path
    audio_root: Path
    audio_size_bytes: int
    audio_mtime_ns: int | None
    lyric_asset_id: str
    lyric_path: Path
    lyric_root: Path
    lyric_size_bytes: int
    lyric_mtime_ns: int | None
    confidence: int


@dataclass(frozen=True, slots=True)
class LyricsBatchResult:
    requested_count: int
    status: str
    success_count: int
    failure_count: int
    cancelled_count: int
    not_run_count: int
    items: tuple["LyricsBatchItemResult", ...]

    @property
    def committed_audio_ids(self) -> tuple[str, ...]:
        return tuple(item.audio_asset_id for item in self.items if item.result == "success")


@dataclass(frozen=True, slots=True)
class LyricsBatchItemResult:
    token: str
    audio_asset_id: str
    lyric_asset_id: str
    result: str
    message: str


@dataclass(frozen=True, slots=True)
class LyricsIgnoreResult:
    audio_asset_ids: tuple[str, ...]
    ignored: bool


def _ignored_audio_ids(repository: LibraryRepository) -> frozenset[str]:
    setting = repository.get_setting(IGNORED_AUDIO_ASSET_IDS_KEY)
    if setting is None:
        return frozenset()
    value = setting.value
    if (
        not isinstance(value, list)
        or any(not isinstance(item, str) or not item.strip() for item in value)
        or len(value) != len(set(value))
    ):
        raise ValueError("持久忽略列表损坏，请先修复设置")
    for asset_id in value:
        asset = repository.get_asset_by_id(asset_id)
        if asset is None:
            raise ValueError(f"持久忽略列表引用了不存在的音频：{asset_id}")
        if asset.kind != "audio":
            raise ValueError(f"持久忽略列表引用了非音频资产：{asset_id}")
    return frozenset(value)


def _path_key(path: Path) -> str:
    return os.path.normcase(os.path.normpath(os.path.abspath(os.fspath(path))))


def _identity_from_file_name(file_name: str) -> tuple[str, str]:
    stem = Path(file_name).stem
    if "-" not in stem:
        return stem.strip(), ""
    title, artist = (part.strip() for part in stem.rsplit("-", 1))
    return title or stem.strip(), artist


def _human_size(size_bytes: int) -> str:
    value = float(size_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{int(value)} B" if unit == "B" else f"{value:.1f}".rstrip("0").rstrip(".") + f" {unit}"
        value /= 1024
    return f"{size_bytes} B"


class LyricsMatchWorker(QThread):
    completed = Signal(object)
    cancelled = Signal(int)
    failed = Signal(str)

    def __init__(
        self,
        *,
        root: Path,
        repository_factory,
        batch_size: int = 100,
        audio_scope: tuple[AudioAssetSnapshot, ...] | None = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        if not isinstance(root, Path) or not root.is_absolute():
            raise ValueError("歌词目录必须是绝对 Path")
        if not callable(repository_factory):
            raise TypeError("repository_factory 必须可调用")
        if isinstance(batch_size, bool) or not isinstance(batch_size, int) or batch_size <= 0:
            raise ValueError("batch_size 必须是正整数")
        self._root = root
        self._repository_factory = repository_factory
        self._batch_size = batch_size
        if audio_scope is not None and (
            not isinstance(audio_scope, tuple)
            or not audio_scope
            or not all(isinstance(item, AudioAssetSnapshot) for item in audio_scope)
        ):
            raise ValueError("歌词限定范围必须是非空 AudioAssetSnapshot 元组")
        self._audio_scope = audio_scope
        self._cancel_event = threading.Event()
        self._start_lock = threading.Lock()
        self._started_once = False

    def start(self, priority=QThread.InheritPriority) -> None:
        with self._start_lock:
            if self._started_once:
                raise RuntimeError("LyricsMatchWorker 是 one-shot")
            self._started_once = True
        super().start(priority)

    def request_cancel(self) -> None:
        self._cancel_event.set()
        self.requestInterruption()

    def _check_cancelled(self) -> None:
        if self._cancel_event.is_set():
            raise LyricsScanCancelled("歌词扫描已取消")

    @staticmethod
    def _error_message(error: BaseException) -> str:
        return str(error).strip() or error.__class__.__name__

    def run(self) -> None:
        repository: LibraryRepository | None = None
        session_id: str | None = None
        indexed_count = 0
        terminal: tuple[str, object] | None = None
        try:
            self._check_cancelled()
            repository = self._repository_factory()
            self._check_cancelled()
            ignored_audio_ids = _ignored_audio_ids(repository)
            scoped_records = (
                revalidate_audio_snapshots(repository, self._audio_scope)
                if self._audio_scope is not None
                else None
            )
            self._check_cancelled()
            session = repository.create_scan_session(mode="lyric", source_folder=self._root)
            session_id = session.id
            lyric_entries: list[LyricsFileEntry] = []
            lyric_assets: dict[str, str] = {}
            pending: list[LyricsFileEntry] = []

            def write_pending() -> None:
                nonlocal indexed_count
                if not pending:
                    return
                self._check_cancelled()
                batch = tuple(pending)
                pending.clear()
                records = repository.index_scan_batch(
                    session_id,
                    tuple(
                        IndexBatchItem(entry.path, entry.size_bytes, entry.mtime_ns, kind="lyric")
                        for entry in batch
                    ),
                )
                indexed_count += len(records)
                for record in records:
                    lyric_assets[_path_key(record.asset.canonical_path)] = record.asset.id

            for entry in iter_lrc_files(
                self._root,
                allowed_root=self._root,
                cancel_requested=self._cancel_event.is_set,
            ):
                self._check_cancelled()
                lyric_entries.append(entry)
                pending.append(entry)
                if len(pending) >= self._batch_size:
                    write_pending()
            write_pending()
            self._check_cancelled()
            repository.finish_scan_session(session_id, status="completed")

            if self._audio_scope is None:
                audio_assets = repository.list_assets(kind="audio", file_state="active")
                roots = repository.latest_completed_audio_roots(asset.id for asset in audio_assets)
            else:
                scoped_records = revalidate_audio_snapshots(repository, self._audio_scope)
                audio_assets = scoped_records
                roots = {record.asset_id: record.allowed_root for record in scoped_records}
            current = {
                item.audio_asset_id: item
                for item in repository.list_lyrics_matches(current_only=True)
                if self._audio_scope is None
                or any(scope.asset_id == item.audio_asset_id for scope in self._audio_scope)
            }
            audio_inputs: list[AudioLyricsInput] = []
            labels: dict[str, str] = {}
            audio_facts: dict[str, tuple[Path, Path, int, int | None]] = {}
            analysis_errors: list[LyricsReviewItem] = []
            for asset in audio_assets:
                self._check_cancelled()
                title, artist = _identity_from_file_name(asset.file_name)
                labels[asset.id] = asset.file_name
                current_match = current.get(asset.id)
                allowed_root = roots.get(asset.id)
                if allowed_root is None:
                    analysis_errors.append(
                        LyricsReviewItem(
                            str(uuid4()), asset.id, asset.file_name, None, None, "external", 0,
                            "无法分析", True, "缺少 P1 完成扫描来源，未读取音频",
                        )
                    )
                    continue
                audio_facts[asset.id] = (
                    asset.canonical_path,
                    allowed_root,
                    asset.size_bytes,
                    asset.mtime_ns,
                )
                if asset.id in ignored_audio_ids:
                    analysis_errors.append(
                        LyricsReviewItem(
                            str(uuid4()), asset.id, asset.file_name, None, None, "external", 0,
                            "已忽略", False, "已按你的设置跳过自动匹配；可取消忽略后重新扫描",
                            current_match is not None, True,
                            asset.canonical_path, allowed_root, asset.size_bytes, asset.mtime_ns,
                        )
                    )
                    continue
                try:
                    embedded = detect_embedded_lyrics(asset.canonical_path, allowed_root=allowed_root)
                except Exception as error:
                    analysis_errors.append(
                        LyricsReviewItem(
                            str(uuid4()), asset.id, asset.file_name, None, None, "external", 0,
                            "无法分析", True, self._error_message(error), current_match is not None,
                            False, asset.canonical_path, allowed_root,
                            asset.size_bytes, asset.mtime_ns,
                        )
                    )
                    continue
                audio_inputs.append(AudioLyricsInput(asset.id, title, artist, embedded))

            raw_candidates = build_lyrics_candidates(audio_inputs, lyric_entries)
            grouped: dict[str, list[LyricsMatchCandidate]] = defaultdict(list)
            for candidate in raw_candidates:
                grouped[candidate.audio_asset_id].append(candidate)
            review_items: list[LyricsReviewItem] = list(analysis_errors)
            lyric_facts = {
                _path_key(entry.path): (entry.size_bytes, entry.mtime_ns)
                for entry in lyric_entries
            }
            automatic_count = 0
            for audio in audio_inputs:
                self._check_cancelled()
                candidates = grouped.get(audio.asset_id, [])
                existing = current.get(audio.asset_id)
                if not candidates:
                    review_items.append(
                        LyricsReviewItem(
                            str(uuid4()), audio.asset_id, labels[audio.asset_id], None, None,
                            "external", 0, "未匹配", True, "没有找到歌词候选",
                            existing is not None, False, *audio_facts[audio.asset_id],
                        )
                    )
                    continue
                top_confidence = max(item.confidence for item in candidates)
                top_ready = [
                    item for item in candidates
                    if item.confidence == top_confidence and item.confidence >= 95 and item.status != "冲突"
                ]
                auto_candidate = top_ready[0] if len(top_ready) == 1 else None
                ambiguous_top = len(top_ready) > 1
                for candidate in candidates:
                    self._check_cancelled()
                    lyric_id = None if candidate.lyric_path is None else lyric_assets.get(_path_key(candidate.lyric_path))
                    status = candidate.status
                    requires_confirmation = candidate.requires_confirmation
                    message = candidate.message
                    should_commit = candidate is auto_candidate
                    if ambiguous_top and candidate in top_ready:
                        status = "冲突"
                        requires_confirmation = True
                        message = "同一音频存在多个同分高置信度 LRC，必须人工选择"
                    if candidate.source_kind == "embedded":
                        should_commit = True
                    if existing is not None and not (
                        candidate.source_kind == "embedded" and existing.source_kind != "embedded"
                    ):
                        should_commit = False
                        status = "已有匹配"
                        requires_confirmation = True
                        message = "已有当前匹配，自动扫描不会覆盖用户选择"
                    if candidate.source_kind == "external" and lyric_id is None:
                        should_commit = False
                        status = "无法提交"
                        requires_confirmation = True
                        message = "候选歌词没有可信索引资产"
                    if should_commit:
                        repository.commit_lyrics_match(
                            audio_asset_id=audio.asset_id,
                            lyric_asset_id=lyric_id,
                            source_kind=candidate.source_kind,
                            confidence=candidate.confidence,
                            method="automatic",
                        )
                        automatic_count += 1
                        status = "已自动匹配" if candidate.source_kind == "external" else "已有内嵌歌词"
                        requires_confirmation = False
                    review_items.append(
                        LyricsReviewItem(
                            str(uuid4()), audio.asset_id, labels[audio.asset_id], lyric_id,
                            candidate.lyric_path, candidate.source_kind, candidate.confidence,
                            status, requires_confirmation, message,
                            existing is not None or should_commit,
                            False,
                            *audio_facts[audio.asset_id],
                            self._root if candidate.lyric_path is not None else None,
                            *(
                                lyric_facts.get(_path_key(candidate.lyric_path), (None, None))
                                if candidate.lyric_path is not None
                                else (None, None)
                            ),
                        )
                    )
            self._check_cancelled()
            terminal = (
                "completed",
                LyricsScanResult(
                    self._root,
                    indexed_count,
                    automatic_count,
                    tuple(review_items),
                ),
            )
        except LyricsScanCancelled:
            if repository is not None and session_id is not None:
                session = repository.get_scan_session(session_id)
                if session is not None and session.status == "running":
                    repository.finish_scan_session(session_id, status="cancelled")
            terminal = ("cancelled", indexed_count)
        except Exception as error:
            message = self._error_message(error)
            if repository is not None and session_id is not None:
                try:
                    session = repository.get_scan_session(session_id)
                    if session is not None and session.status == "running":
                        repository.finish_scan_session(session_id, status="failed")
                except Exception as finish_error:
                    message += f"；终结扫描会话失败：{self._error_message(finish_error)}"
            terminal = ("failed", message)
        finally:
            if repository is not None:
                try:
                    repository.close()
                except Exception as error:
                    terminal = ("failed", f"关闭歌词索引失败：{self._error_message(error)}")
            if terminal is None:
                terminal = ("failed", "歌词匹配线程未产生终态")
            kind, payload = terminal
            if kind == "completed":
                self.completed.emit(payload)
            elif kind == "cancelled":
                self.cancelled.emit(int(payload))
            else:
                self.failed.emit(str(payload))


class LyricsBatchCommitWorker(QThread):
    completed = Signal(object)
    partial = Signal(object)
    cancelled = Signal(object)
    failed = Signal(object)

    def __init__(self, *, inputs: tuple[LyricsBatchInput, ...], repository_factory, parent=None) -> None:
        super().__init__(parent)
        if not inputs or not all(isinstance(item, LyricsBatchInput) for item in inputs):
            raise ValueError("批量歌词提交必须包含有效冻结项")
        self._inputs = inputs
        self._repository_factory = repository_factory
        self._cancel_event = threading.Event()
        self._started_once = False
        self._start_lock = threading.Lock()

    def start(self, priority=QThread.InheritPriority) -> None:
        with self._start_lock:
            if self._started_once:
                raise RuntimeError("LyricsBatchCommitWorker 是 one-shot")
            self._started_once = True
        super().start(priority)

    def request_cancel(self) -> None:
        self._cancel_event.set()
        self.requestInterruption()

    @staticmethod
    def _validate_lyric(item: LyricsBatchInput, repository: LibraryRepository) -> None:
        lyric = repository.get_asset_by_id(item.lyric_asset_id)
        if (
            lyric is None
            or lyric.kind != "lyric"
            or lyric.file_state != "active"
            or _path_key(lyric.canonical_path) != _path_key(item.lyric_path)
            or lyric.size_bytes != item.lyric_size_bytes
            or lyric.mtime_ns != item.lyric_mtime_ns
        ):
            raise ValueError("歌词索引或文件事实已变化，请重新扫描")
        if not _within_root(item.lyric_path, item.lyric_root):
            raise ValueError("歌词路径超出本次可信扫描根")
        with _locked_directory_chain(item.lyric_root, item.lyric_path.parent):
            metadata = os.lstat(item.lyric_path)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or stat.S_ISLNK(metadata.st_mode)
                or _is_reparse(metadata)
                or metadata.st_size != item.lyric_size_bytes
                or metadata.st_mtime_ns != item.lyric_mtime_ns
            ):
                raise ValueError("歌词文件已变化，请重新扫描")

    @staticmethod
    def _make_result(
        inputs: tuple[LyricsBatchInput, ...],
        item_results: list[LyricsBatchItemResult],
    ) -> LyricsBatchResult:
        by_token = {item.token: item for item in item_results}
        ordered = tuple(
            by_token.get(
                item.token,
                LyricsBatchItemResult(
                    item.token, item.audio_asset_id, item.lyric_asset_id,
                    "not_run", "未开始处理",
                ),
            )
            for item in inputs
        )
        success = sum(item.result == "success" for item in ordered)
        failed = sum(item.result == "failed" for item in ordered)
        cancelled = sum(item.result == "cancelled" for item in ordered)
        not_run = sum(item.result == "not_run" for item in ordered)
        if cancelled:
            status = "cancelled"
        elif success == len(ordered):
            status = "completed"
        elif success:
            status = "partial"
        else:
            status = "failed"
        return LyricsBatchResult(
            len(ordered), status, success, failed, cancelled, not_run, ordered,
        )

    def run(self) -> None:
        repository: LibraryRepository | None = None
        item_results: list[LyricsBatchItemResult] = []
        terminal: LyricsBatchResult | None = None
        try:
            if self._cancel_event.is_set():
                item_results.extend(
                    LyricsBatchItemResult(
                        item.token, item.audio_asset_id, item.lyric_asset_id,
                        "cancelled", "开始前已取消",
                    )
                    for item in self._inputs
                )
                terminal = self._make_result(self._inputs, item_results)
                return
            repository = self._repository_factory()
            audio_ids = [item.audio_asset_id for item in self._inputs]
            lyric_ids = [item.lyric_asset_id for item in self._inputs]
            if len(audio_ids) != len(set(audio_ids)):
                raise ValueError("同一批次不能为一首音乐提交多个歌词")
            if len(lyric_ids) != len(set(lyric_ids)):
                raise ValueError("同一批次不能重复使用同一外部歌词")
            snapshots = tuple(
                AudioAssetSnapshot(
                    item.audio_asset_id,
                    item.audio_path,
                    item.audio_size_bytes,
                    item.audio_mtime_ns,
                    item.audio_root,
                )
                for item in self._inputs
            )
            revalidate_audio_snapshots(repository, snapshots)
            for item in self._inputs:
                self._validate_lyric(item, repository)
            current_matches = repository.list_lyrics_matches(current_only=True)
            current_by_audio = {item.audio_asset_id: item for item in current_matches}
            claimed_lyrics = {
                item.lyric_asset_id: item.audio_asset_id
                for item in current_matches
                if item.lyric_asset_id is not None
            }
            for item in self._inputs:
                current = current_by_audio.get(item.audio_asset_id)
                if current is not None and current.source_kind == "embedded":
                    raise ValueError("已有内嵌歌词的音乐不能改用外部歌词")
                claimed_by = claimed_lyrics.get(item.lyric_asset_id)
                if claimed_by is not None and claimed_by != item.audio_asset_id:
                    raise ValueError("所选歌词已被另一首音乐占用")
            if self._cancel_event.is_set():
                item_results.extend(
                    LyricsBatchItemResult(
                        item.token, item.audio_asset_id, item.lyric_asset_id,
                        "cancelled", "预验后已取消",
                    )
                    for item in self._inputs
                )
                terminal = self._make_result(self._inputs, item_results)
                return
            for index, item in enumerate(self._inputs):
                # 已开始的单项 repository 事务允许完成；随后观察取消并停止下一项。
                if self._cancel_event.is_set():
                    item_results.extend(
                        LyricsBatchItemResult(
                            pending.token, pending.audio_asset_id, pending.lyric_asset_id,
                            "cancelled", "批量任务已取消，未开始该项",
                        )
                        for pending in self._inputs[index:]
                    )
                    break
                try:
                    repository.commit_lyrics_match(
                        audio_asset_id=item.audio_asset_id,
                        lyric_asset_id=item.lyric_asset_id,
                        source_kind="external",
                        confidence=item.confidence,
                        method="manual",
                    )
                except Exception as error:
                    item_results.append(
                        LyricsBatchItemResult(
                            item.token, item.audio_asset_id, item.lyric_asset_id,
                            "failed", str(error).strip() or error.__class__.__name__,
                        )
                    )
                    continue
                item_results.append(
                    LyricsBatchItemResult(
                        item.token, item.audio_asset_id, item.lyric_asset_id,
                        "success", "匹配关系已保存",
                    )
                )
            terminal = self._make_result(self._inputs, item_results)
        except Exception as error:
            message = str(error).strip() or error.__class__.__name__
            if self._inputs:
                first = self._inputs[0]
                item_results.append(
                    LyricsBatchItemResult(
                        first.token, first.audio_asset_id, first.lyric_asset_id,
                        "failed", message,
                    )
                )
            terminal = self._make_result(self._inputs, item_results)
        finally:
            if repository is not None:
                try:
                    repository.close()
                except Exception as error:
                    inputs = self._inputs
                    terminal = LyricsBatchResult(
                        len(inputs), "failed", 0, 1, 0, max(0, len(inputs) - 1),
                        tuple(
                            LyricsBatchItemResult(
                                item.token, item.audio_asset_id, item.lyric_asset_id,
                                "failed" if index == 0 else "not_run",
                                f"关闭歌词批量提交失败：{error}" if index == 0 else "结果不可信，未报告成功",
                            )
                            for index, item in enumerate(inputs)
                        ),
                    )
            result = terminal or self._make_result(self._inputs, item_results)
            if result.status == "completed":
                self.completed.emit(result)
            elif result.status == "partial":
                self.partial.emit(result)
            elif result.status == "cancelled":
                self.cancelled.emit(result)
            else:
                self.failed.emit(result)


class LyricsIgnoreWorker(QThread):
    completed = Signal(object)
    cancelled = Signal(int)
    failed = Signal(str)

    def __init__(
        self,
        *,
        audio_asset_ids: tuple[str, ...],
        ignored: bool,
        repository_factory,
        parent=None,
    ) -> None:
        super().__init__(parent)
        if (
            not audio_asset_ids
            or len(audio_asset_ids) != len(set(audio_asset_ids))
            or any(not isinstance(item, str) or not item.strip() for item in audio_asset_ids)
        ):
            raise ValueError("请选择有效且不重复的音乐")
        self._audio_asset_ids = audio_asset_ids
        self._ignored = bool(ignored)
        self._repository_factory = repository_factory
        self._cancel_event = threading.Event()
        self._started_once = False
        self._start_lock = threading.Lock()

    def start(self, priority=QThread.InheritPriority) -> None:
        with self._start_lock:
            if self._started_once:
                raise RuntimeError("LyricsIgnoreWorker 是 one-shot")
            self._started_once = True
        super().start(priority)

    def request_cancel(self) -> None:
        self._cancel_event.set()
        self.requestInterruption()

    def run(self) -> None:
        repository: LibraryRepository | None = None
        terminal: tuple[str, object] | None = None
        try:
            if self._cancel_event.is_set():
                raise LyricsScanCancelled("忽略设置已取消")
            repository = self._repository_factory()
            current = set(_ignored_audio_ids(repository))
            for asset_id in self._audio_asset_ids:
                asset = repository.get_asset_by_id(asset_id)
                if asset is None or asset.kind != "audio":
                    raise ValueError(f"忽略设置只接受已索引音频：{asset_id}")
            if self._cancel_event.is_set():
                raise LyricsScanCancelled("忽略设置已取消")
            if self._ignored:
                current.update(self._audio_asset_ids)
            else:
                current.difference_update(self._audio_asset_ids)
            repository.set_setting(IGNORED_AUDIO_ASSET_IDS_KEY, sorted(current))
            terminal = (
                "completed",
                LyricsIgnoreResult(self._audio_asset_ids, self._ignored),
            )
        except LyricsScanCancelled:
            terminal = ("cancelled", 0)
        except Exception as error:
            terminal = ("failed", str(error).strip() or error.__class__.__name__)
        finally:
            if repository is not None:
                try:
                    repository.close()
                except Exception as error:
                    terminal = ("failed", f"关闭忽略设置失败：{error}")
            kind, payload = terminal or ("failed", "忽略设置没有终态")
            if kind == "completed":
                self.completed.emit(payload)
            elif kind == "cancelled":
                self.cancelled.emit(int(payload))
            else:
                self.failed.emit(str(payload))


class LyricsMatchController(QObject):
    results_ready = Signal(object)
    lyrics_changed = Signal(object)
    completed = Signal(object)
    cancelled = Signal(int)
    failed = Signal(str)
    warning = Signal(str)
    running_changed = Signal(bool)
    match_changed = Signal(str)
    batch_finished = Signal(object)

    def __init__(self, database_config: DatabaseConfig, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._database_config = database_config
        self._worker: LyricsMatchWorker | LyricsBatchCommitWorker | LyricsIgnoreWorker | None = None
        self._operation_kind: str | None = None
        self._terminal: tuple[str, object] | None = None
        self._active_root: Path | None = None
        self._review_by_token: dict[str, LyricsReviewItem] = {}
        self._original_review_by_token: dict[str, LyricsReviewItem] = {}
        self._last_result: LyricsScanResult | None = None
        self._pending_scope: tuple[AudioAssetSnapshot, ...] | None = None

    @property
    def running(self) -> bool:
        return self._worker is not None

    def _open_repository(self) -> LibraryRepository:
        return LibraryRepository(self._database_config)

    @property
    def pending_scope(self) -> tuple[AudioAssetSnapshot, ...] | None:
        return self._pending_scope

    def set_scope(self, records: tuple[RevalidatedAudioRecord, ...]) -> None:
        if self.running:
            raise RuntimeError("歌词扫描运行期间不能修改限定范围")
        if not isinstance(records, tuple) or not records:
            raise ValueError("请至少选择一首音乐重新匹配歌词")
        snapshots: list[AudioAssetSnapshot] = []
        for record in records:
            if not isinstance(record, RevalidatedAudioRecord):
                raise TypeError("歌词限定范围必须来自音乐索引重验")
            snapshots.append(
                AudioAssetSnapshot(
                    record.asset_id,
                    record.canonical_path,
                    record.size_bytes,
                    record.mtime_ns,
                    record.allowed_root,
                )
            )
        self._pending_scope = tuple(snapshots)

    def clear_scope(self) -> None:
        if self.running:
            return
        self._pending_scope = None

    def review_snapshot(self) -> LyricsScanResult | None:
        result = self._last_result
        if result is None:
            repository = self._open_repository()
            try:
                if self._pending_scope is None:
                    records = repository.list_assets(kind="audio", file_state="active")
                    roots = repository.latest_completed_audio_roots(item.id for item in records)
                else:
                    records = revalidate_audio_snapshots(repository, self._pending_scope)
                    roots = {item.asset_id: item.allowed_root for item in records}
                current_ids = {
                    item.audio_asset_id
                    for item in repository.list_lyrics_matches(current_only=True)
                }
                ignored_ids = _ignored_audio_ids(repository)
                setting = repository.get_setting(LAST_SUCCESSFUL_LYRICS_ROOT_KEY)
                root = (
                    Path(setting.value)
                    if setting is not None
                    and isinstance(setting.value, str)
                    and Path(setting.value).is_absolute()
                    else self._database_config.path.parent
                )
                items: list[LyricsReviewItem] = []
                originals: dict[str, LyricsReviewItem] = {}
                for record in records:
                    asset_id = record.id
                    allowed_root = roots.get(asset_id)
                    token = str(uuid4())
                    has_current = asset_id in current_ids
                    base = LyricsReviewItem(
                        token,
                        asset_id,
                        record.file_name,
                        None,
                        None,
                        "external",
                        0,
                        "已有匹配" if has_current else "未匹配",
                        False,
                        "请选择 LRC 目录并开始扫描以生成真实候选",
                        has_current,
                        False,
                        record.canonical_path,
                        allowed_root,
                        record.size_bytes,
                        record.mtime_ns,
                    )
                    originals[token] = base
                    items.append(
                        replace(
                            base,
                            status="已忽略",
                            message="已按你的设置跳过自动匹配；可取消忽略后重新扫描",
                            ignored=True,
                        )
                        if asset_id in ignored_ids
                        else base
                    )
                result = LyricsScanResult(root, 0, 0, tuple(items))
                self._last_result = result
                self._review_by_token = {item.token: item for item in items}
                self._original_review_by_token = originals
            finally:
                repository.close()
        if self._pending_scope is None:
            return replace(result, items=tuple(self._review_by_token.values()))
        allowed = {item.asset_id for item in self._pending_scope}
        return replace(
            result,
            items=tuple(
                item for item in self._review_by_token.values()
                if item.audio_asset_id in allowed
            ),
        )

    def load_lyrics_library(self) -> tuple[dict[str, object], ...]:
        repository = self._open_repository()
        try:
            assets = repository.list_assets(kind="lyric")
            setting = repository.get_setting(LAST_SUCCESSFUL_LYRICS_ROOT_KEY)
            allowed_root = (
                Path(setting.value)
                if setting is not None and isinstance(setting.value, str) and Path(setting.value).is_absolute()
                else None
            )
            matched_ids = {
                item.lyric_asset_id
                for item in repository.list_lyrics_matches(current_only=True)
                if item.lyric_asset_id is not None
            }
            records = []
            for asset in assets:
                title, artist = _identity_from_file_name(asset.file_name)
                records.append(
                    {
                        "_asset_id": asset.id,
                        "_canonical_path": asset.canonical_path,
                        "_allowed_root": allowed_root,
                        "_file_state": asset.file_state,
                        "title": title,
                        "artist": artist or "待识别",
                        "format": asset.extension.lstrip(".").upper(),
                        "size": _human_size(asset.size_bytes),
                        "status": "已匹配" if asset.id in matched_ids else "未匹配",
                    }
                )
            return tuple(records)
        finally:
            repository.close()

    def remembered_root(self) -> Path | None:
        repository: LibraryRepository | None = None
        try:
            repository = self._open_repository()
            setting = repository.get_setting(LAST_SUCCESSFUL_LYRICS_ROOT_KEY)
        except Exception as error:
            self.warning.emit(f"无法读取上次歌词目录：{error}")
            return None
        finally:
            if repository is not None:
                repository.close()
        if setting is None or not isinstance(setting.value, str):
            return None
        path = Path(setting.value)
        return path if path.is_absolute() else None

    def list_history(self) -> tuple[dict[str, object], ...]:
        """Return newest-first lyrics relation history with real indexed paths."""

        repository = self._open_repository()
        try:
            records = repository.list_lyrics_matches()
            history: list[dict[str, object]] = []
            for record in reversed(records):
                audio = repository.get_asset_by_id(record.audio_asset_id)
                if audio is None:
                    raise ValueError("歌词历史引用的音频索引不存在")
                lyric_path: Path | None = None
                if record.lyric_asset_id is not None:
                    lyric = repository.get_asset_by_id(record.lyric_asset_id)
                    if lyric is None:
                        raise ValueError("歌词历史引用的歌词索引不存在")
                    lyric_path = lyric.canonical_path
                history.append(
                    {
                        "id": record.id,
                        "created_at": record.created_at,
                        "updated_at": record.updated_at,
                        "audio_asset_id": record.audio_asset_id,
                        "audio_path": audio.canonical_path,
                        "lyric_asset_id": record.lyric_asset_id,
                        "lyric_path": lyric_path,
                        "source_kind": record.source_kind,
                        "confidence": record.confidence,
                        "method": record.method,
                        "state": record.state,
                        "is_current": record.is_current,
                    }
                )
            return tuple(history)
        finally:
            repository.close()

    def start_scan(self, root: Path) -> None:
        if not isinstance(root, Path) or not root.is_absolute():
            raise ValueError("歌词目录必须是绝对 Path")
        if self.running:
            raise RuntimeError("歌词扫描已经在运行")
        config = self._database_config
        scope = self._pending_scope
        self._pending_scope = None
        worker = LyricsMatchWorker(
            root=root,
            repository_factory=lambda: LibraryRepository(config),
            audio_scope=scope,
        )
        worker.completed.connect(lambda result: self._cache_terminal("completed", result))
        worker.cancelled.connect(lambda count: self._cache_terminal("cancelled", count))
        worker.failed.connect(lambda message: self._cache_terminal("failed", message))
        worker.finished.connect(self._worker_finished)
        self._worker = worker
        self._operation_kind = "scan"
        self._terminal = None
        self._active_root = root
        self._review_by_token.clear()
        self._original_review_by_token.clear()
        self._last_result = None
        self.running_changed.emit(True)
        try:
            worker.start()
        except Exception:
            self._worker = None
            self._operation_kind = None
            self._active_root = None
            self.running_changed.emit(False)
            raise

    def request_cancel(self) -> None:
        if self._worker is not None:
            self._worker.request_cancel()

    def _freeze_batch(self, tokens: tuple[str, ...]) -> tuple[LyricsBatchInput, ...]:
        if self.running:
            raise RuntimeError("歌词后台任务正在运行")
        if not isinstance(tokens, tuple) or not tokens:
            raise ValueError("请至少选择一个歌词候选")
        if len(tokens) != len(set(tokens)):
            raise ValueError("所选歌词候选包含重复项")
        inputs: list[LyricsBatchInput] = []
        for token in tokens:
            item = self._review_by_token.get(token)
            if (
                item is None
                or item.ignored
                or not item.requires_confirmation
                or item.source_kind != "external"
                or item.lyric_asset_id is None
                or item.audio_path is None
                or item.audio_root is None
                or item.audio_size_bytes is None
                or item.lyric_path is None
                or item.lyric_root is None
                or item.lyric_size_bytes is None
            ):
                raise ValueError("所选歌词候选已失效，请重新扫描")
            inputs.append(
                LyricsBatchInput(
                    item.token,
                    item.audio_asset_id,
                    item.audio_path,
                    item.audio_root,
                    item.audio_size_bytes,
                    item.audio_mtime_ns,
                    item.lyric_asset_id,
                    item.lyric_path,
                    item.lyric_root,
                    item.lyric_size_bytes,
                    item.lyric_mtime_ns,
                    item.confidence,
                )
            )
        audio_ids = [item.audio_asset_id for item in inputs]
        lyric_ids = [item.lyric_asset_id for item in inputs]
        if len(audio_ids) != len(set(audio_ids)):
            raise ValueError("同一批次不能为一首音乐选择多个歌词")
        if len(lyric_ids) != len(set(lyric_ids)):
            raise ValueError("同一批次不能重复使用同一歌词")
        return tuple(inputs)

    def commit_candidates(self, tokens: tuple[str, ...]) -> None:
        inputs = self._freeze_batch(tokens)
        config = self._database_config
        worker = LyricsBatchCommitWorker(
            inputs=inputs,
            repository_factory=lambda: LibraryRepository(config),
        )
        worker.completed.connect(lambda result: self._cache_terminal("completed", result))
        worker.partial.connect(lambda result: self._cache_terminal("partial", result))
        worker.cancelled.connect(lambda result: self._cache_terminal("cancelled", result))
        worker.failed.connect(lambda result: self._cache_terminal("failed", result))
        worker.finished.connect(self._worker_finished)
        self._worker = worker
        self._operation_kind = "batch"
        self._terminal = None
        self.running_changed.emit(True)
        try:
            worker.start()
        except Exception:
            self._worker = None
            self._operation_kind = None
            self.running_changed.emit(False)
            raise

    def _set_ignored(self, audio_asset_ids: tuple[str, ...], *, ignored: bool) -> None:
        if self.running:
            raise RuntimeError("歌词后台任务正在运行")
        config = self._database_config
        worker = LyricsIgnoreWorker(
            audio_asset_ids=audio_asset_ids,
            ignored=ignored,
            repository_factory=lambda: LibraryRepository(config),
        )
        worker.completed.connect(lambda result: self._cache_terminal("completed", result))
        worker.cancelled.connect(lambda count: self._cache_terminal("cancelled", count))
        worker.failed.connect(lambda message: self._cache_terminal("failed", message))
        worker.finished.connect(self._worker_finished)
        self._worker = worker
        self._operation_kind = "ignore"
        self._terminal = None
        self.running_changed.emit(True)
        try:
            worker.start()
        except Exception:
            self._worker = None
            self._operation_kind = None
            self.running_changed.emit(False)
            raise

    def ignore_audio_assets(self, audio_asset_ids: tuple[str, ...]) -> None:
        self._set_ignored(audio_asset_ids, ignored=True)

    def unignore_audio_assets(self, audio_asset_ids: tuple[str, ...]) -> None:
        self._set_ignored(audio_asset_ids, ignored=False)

    def _cache_terminal(self, kind: str, payload: object) -> None:
        if self._terminal is None:
            self._terminal = (kind, payload)

    @Slot()
    def _worker_finished(self) -> None:
        worker = self._worker
        if worker is None:
            return
        operation_kind = self._operation_kind
        kind, payload = self._terminal or ("failed", "歌词线程结束但没有终态")
        if operation_kind == "scan" and kind == "completed" and isinstance(payload, LyricsScanResult):
            self._review_by_token = {item.token: item for item in payload.items}
            self._original_review_by_token = {
                item.token: (
                    replace(
                        item,
                        status="未匹配",
                        requires_confirmation=True,
                        message="已取消忽略，请重新扫描以生成歌词候选",
                        ignored=False,
                    )
                    if item.ignored
                    else item
                )
                for item in payload.items
            }
            self._last_result = payload
            repository = self._open_repository()
            try:
                repository.set_setting(LAST_SUCCESSFUL_LYRICS_ROOT_KEY, str(payload.root))
            except Exception as error:
                self.warning.emit(f"歌词扫描成功，但无法记住目录：{error}")
            finally:
                repository.close()
        if operation_kind == "batch" and isinstance(payload, LyricsBatchResult):
            committed_audio_ids = payload.committed_audio_ids
            if committed_audio_ids:
                consumed = set(committed_audio_ids)
                self._review_by_token = {
                    token: item for token, item in self._review_by_token.items()
                    if item.audio_asset_id not in consumed
                }
                self._original_review_by_token = {
                    token: item for token, item in self._original_review_by_token.items()
                    if item.audio_asset_id not in consumed
                }
                if self._last_result is not None:
                    self._last_result = replace(
                        self._last_result,
                        items=tuple(self._review_by_token.values()),
                    )
                    self.results_ready.emit(self._last_result)
            self.match_changed.emit(
                "匹配关系处理完成："
                f"成功 {payload.success_count} 项，失败 {payload.failure_count} 项，"
                f"取消 {payload.cancelled_count} 项，未执行 {payload.not_run_count} 项。"
            )
        if operation_kind == "ignore" and kind == "completed" and isinstance(payload, LyricsIgnoreResult):
            audio_asset_ids = payload.audio_asset_ids
            for token, original in tuple(self._original_review_by_token.items()):
                if original.audio_asset_id not in audio_asset_ids:
                    continue
                self._review_by_token[token] = (
                    replace(
                        original,
                        status="已忽略",
                        requires_confirmation=False,
                        message="已按你的设置跳过自动匹配；可取消忽略后重新扫描",
                        ignored=True,
                    )
                    if payload.ignored
                    else original
                )
            if self._last_result is not None:
                self._last_result = replace(
                    self._last_result,
                    items=tuple(self._review_by_token.values()),
                )
                self.results_ready.emit(self._last_result)
            self.match_changed.emit(
                "已持久忽略所选音乐" if payload.ignored else "已取消忽略所选音乐"
            )
        try:
            self.lyrics_changed.emit(self.load_lyrics_library())
        except Exception as error:
            self.warning.emit(f"无法刷新歌词列表：{error}")
        if operation_kind == "batch" and isinstance(payload, LyricsBatchResult):
            self.batch_finished.emit(payload)
        elif kind == "completed":
            if operation_kind == "scan":
                self.results_ready.emit(payload)
            self.completed.emit(payload)
        elif kind == "cancelled":
            self.cancelled.emit(int(payload))
        else:
            self.failed.emit(str(payload))
        self._worker = None
        self._operation_kind = None
        self._terminal = None
        self._active_root = None
        worker.deleteLater()
        self.running_changed.emit(False)

    def commit_candidate(self, token: str) -> None:
        self.commit_candidates((token,))

    def cancel_current_match(self, audio_asset_id: str) -> LyricsMatchRecord:
        repository = self._open_repository()
        try:
            record = repository.cancel_current_lyrics_match(audio_asset_id)
        finally:
            repository.close()
        self.match_changed.emit("已取消当前歌词匹配，历史记录仍保留")
        self.lyrics_changed.emit(self.load_lyrics_library())
        return record
