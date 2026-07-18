"""Background P4 lyrics indexing, matching and UI-thread coordination."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, replace
from pathlib import Path
import os
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


LAST_SUCCESSFUL_LYRICS_ROOT_KEY = "p4.last_successful_lyrics_root"


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


@dataclass(frozen=True, slots=True)
class LyricsScanResult:
    root: Path
    indexed_count: int
    automatic_count: int
    items: tuple[LyricsReviewItem, ...]


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
            analysis_errors: list[LyricsReviewItem] = []
            for asset in audio_assets:
                self._check_cancelled()
                title, artist = _identity_from_file_name(asset.file_name)
                labels[asset.id] = asset.file_name
                allowed_root = roots.get(asset.id)
                if allowed_root is None:
                    analysis_errors.append(
                        LyricsReviewItem(
                            str(uuid4()), asset.id, asset.file_name, None, None, "external", 0,
                            "无法分析", True, "缺少 P1 完成扫描来源，未读取音频",
                        )
                    )
                    continue
                try:
                    embedded = detect_embedded_lyrics(asset.canonical_path, allowed_root=allowed_root)
                except Exception as error:
                    analysis_errors.append(
                        LyricsReviewItem(
                            str(uuid4()), asset.id, asset.file_name, None, None, "external", 0,
                            "无法分析", True, self._error_message(error),
                        )
                    )
                    continue
                audio_inputs.append(AudioLyricsInput(asset.id, title, artist, embedded))

            raw_candidates = build_lyrics_candidates(audio_inputs, lyric_entries)
            grouped: dict[str, list[LyricsMatchCandidate]] = defaultdict(list)
            for candidate in raw_candidates:
                grouped[candidate.audio_asset_id].append(candidate)
            review_items: list[LyricsReviewItem] = list(analysis_errors)
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


class LyricsMatchController(QObject):
    results_ready = Signal(object)
    lyrics_changed = Signal(object)
    completed = Signal(object)
    cancelled = Signal(int)
    failed = Signal(str)
    warning = Signal(str)
    running_changed = Signal(bool)
    match_changed = Signal(str)

    def __init__(self, database_config: DatabaseConfig, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._database_config = database_config
        self._worker: LyricsMatchWorker | None = None
        self._terminal: tuple[str, object] | None = None
        self._active_root: Path | None = None
        self._review_by_token: dict[str, LyricsReviewItem] = {}
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
        self._terminal = None
        self._active_root = root
        self._review_by_token.clear()
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

    def _cache_terminal(self, kind: str, payload: object) -> None:
        if self._terminal is None:
            self._terminal = (kind, payload)

    @Slot()
    def _worker_finished(self) -> None:
        worker = self._worker
        if worker is None:
            return
        kind, payload = self._terminal or ("failed", "歌词线程结束但没有终态")
        if kind == "completed" and isinstance(payload, LyricsScanResult):
            self._review_by_token = {item.token: item for item in payload.items}
            repository = self._open_repository()
            try:
                repository.set_setting(LAST_SUCCESSFUL_LYRICS_ROOT_KEY, str(payload.root))
            except Exception as error:
                self.warning.emit(f"歌词扫描成功，但无法记住目录：{error}")
            finally:
                repository.close()
        try:
            self.lyrics_changed.emit(self.load_lyrics_library())
        except Exception as error:
            self.warning.emit(f"无法刷新歌词列表：{error}")
        if kind == "completed":
            self.results_ready.emit(payload)
            self.completed.emit(payload)
        elif kind == "cancelled":
            self.cancelled.emit(int(payload))
        else:
            self.failed.emit(str(payload))
        self._worker = None
        self._terminal = None
        self._active_root = None
        worker.deleteLater()
        self.running_changed.emit(False)

    def commit_candidate(self, token: str) -> LyricsMatchRecord:
        item = self._review_by_token.get(token)
        if (
            item is None
            or not item.requires_confirmation
            or item.source_kind != "external"
            or item.lyric_asset_id is None
        ):
            raise ValueError("人工歌词候选 token 无效")
        repository = self._open_repository()
        try:
            record = repository.commit_lyrics_match(
                audio_asset_id=item.audio_asset_id,
                lyric_asset_id=item.lyric_asset_id,
                source_kind="external",
                confidence=item.confidence,
                method="manual",
            )
        finally:
            repository.close()
        self.match_changed.emit("已保存人工歌词匹配")
        self.lyrics_changed.emit(self.load_lyrics_library())
        return record

    def cancel_current_match(self, audio_asset_id: str) -> LyricsMatchRecord:
        repository = self._open_repository()
        try:
            record = repository.cancel_current_lyrics_match(audio_asset_id)
        finally:
            repository.close()
        self.match_changed.emit("已取消当前歌词匹配，历史记录仍保留")
        self.lyrics_changed.emit(self.load_lyrics_library())
        return record
