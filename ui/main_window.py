from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import QDir, QProcess, QTimer, QUrl, Qt
from PySide6.QtGui import QCloseEvent, QDesktopServices
from PySide6.QtWidgets import QApplication, QDialog, QFileDialog, QHBoxLayout, QMainWindow, QMessageBox, QStackedWidget, QVBoxLayout, QWidget

from dialogs.delete_confirm_dialog import DeleteConfirmDialog, DeleteLyricsConfirmDialog, RemovePlaylistItemsDialog
from dialogs.history_dialog import HistoryDialog
from dialogs.import_dialog import ImportDialog
from dialogs.lyrics_match_dialog import LyricsMatchDialog
from dialogs.playlist_dialog import CreatePlaylistDialog
from dialogs.rename_preview_dialog import RenamePreviewDialog
from dialogs.read_only_scan_dialog import ReadOnlyScanDialog
from dialogs.settings_dialog import SettingsDialog
from mock.data import LYRICS, PLAYLIST_MAP, SONGS
from ui.music_page import LibraryPage
from ui.sidebar import Sidebar
from ui.toolbars import GlobalToolbar

if TYPE_CHECKING:
    from services.library_scan_controller import LibraryScanController
    from services.lyrics_match_controller import LyricsMatchController
    from services.metadata_preview import MetadataPreviewController
    from services.safe_rename import SafeRenameController
    from services.playlist_controller import PlaylistController
    from services.safe_import import SafeImportController
    from services.backup_manager import BackupController

from services.metadata_preview import MetadataPreviewInput
from services.safe_rename import SafeRenameInput
from services.playlist_controller import PlaylistAudioInput, PlaylistRemovalInput, PlaylistRetargetInput
from services.backup_manager import BackupInput
from services.history_service import HistoryService
from services.library_scan_controller import (
    AudioAssetSnapshot,
    RevalidatedAudioRecord,
)


class MainWindow(QMainWindow):
    def __init__(
        self,
        scan_controller: LibraryScanController | None = None,
        metadata_preview_controller: MetadataPreviewController | None = None,
        safe_rename_controller: SafeRenameController | None = None,
        lyrics_match_controller: LyricsMatchController | None = None,
        playlist_controller: PlaylistController | None = None,
        safe_import_controller: SafeImportController | None = None,
        backup_controller: BackupController | None = None,
        *,
        use_model_view: bool = False,
    ) -> None:
        super().__init__()
        self._scan_controller = scan_controller
        self._metadata_preview_controller = metadata_preview_controller
        self._safe_rename_controller = safe_rename_controller
        self._lyrics_match_controller = lyrics_match_controller
        self._playlist_controller = playlist_controller
        self._safe_import_controller = safe_import_controller
        self._backup_controller = backup_controller
        self._use_model_view = bool(use_model_view)
        self._scan_dialog: ReadOnlyScanDialog | None = None
        self._import_dialog: ImportDialog | None = None
        self._rename_dialog: RenamePreviewDialog | None = None
        self._lyrics_dialog: LyricsMatchDialog | None = None
        self._history_dialog: HistoryDialog | None = None
        self._settings_dialog: SettingsDialog | None = None
        self._close_pending = False
        self._lyrics_context_scope_active = False
        self._metadata_inputs_by_asset: dict[str, MetadataPreviewInput] = {}
        self._metadata_results_by_asset: dict[str, object] = {}
        self._playlist_add_queue: list[tuple[str, tuple[PlaylistAudioInput, ...]]] = []
        self._pending_refresh_roots: list[tuple[str, Path]] = []
        self.setWindowTitle("乐库整理助手")
        app = QApplication.instance()
        if app is not None and not app.windowIcon().isNull():
            self.setWindowIcon(app.windowIcon())
        self.resize(1200, 760)
        self.setMinimumSize(960, 600)
        self._open_windows: list[QWidget] = []
        self.pages: dict[str, LibraryPage] = {}

        root = QWidget()
        root.setObjectName("AppRoot")
        self.setCentralWidget(root)
        root_layout = QHBoxLayout(root)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        self.sidebar = Sidebar(live_mode=playlist_controller is not None)
        self.sidebar.navigation_requested.connect(self.navigate)
        self.sidebar.create_playlist_requested.connect(self.create_playlist)
        root_layout.addWidget(self.sidebar)

        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(0)
        self.toolbar = GlobalToolbar()
        self.toolbar.import_requested.connect(self.open_import)
        self.toolbar.rename_requested.connect(self.open_rename)
        self.toolbar.lyrics_requested.connect(self.open_lyrics_match)
        self.toolbar.history_requested.connect(self.open_history)
        self.toolbar.settings_requested.connect(self.open_settings)
        content_layout.addWidget(self.toolbar)

        self.stack = QStackedWidget()
        content_layout.addWidget(self.stack, 1)
        root_layout.addWidget(content, 1)

        music_data = SONGS if scan_controller is None else []
        music_count = 268 if scan_controller is None else 0
        self._add_page(
            "所有音乐",
            LibraryPage(
                "所有音乐",
                music_data,
                display_count=music_count,
                use_model_view=self._use_model_view,
                live_mode=scan_controller is not None,
            ),
        )
        lyrics_data = LYRICS if lyrics_match_controller is None else []
        lyrics_count = 214 if lyrics_match_controller is None else 0
        self._add_page(
            "所有歌词",
            LibraryPage("所有歌词", lyrics_data, kind="lyrics", display_count=lyrics_count),
        )
        display_counts = {"我喜欢的": 62, "粤语": 36, "通勤": 28, "怀旧": 41, "古巨基": 17}
        if playlist_controller is None:
            for name, indices in PLAYLIST_MAP.items():
                records = [SONGS[index] for index in indices]
                self._add_page(
                    f"playlist:{name}",
                    LibraryPage(name, records, display_count=display_counts.get(name, len(records)), playlist_name=name),
                )
        self.navigate("所有音乐")
        if scan_controller is not None:
            scan_controller.library_changed.connect(self._replace_music_library)
            scan_controller.running_changed.connect(self._background_running_changed)
            try:
                self._replace_music_library(scan_controller.load_library())
            except Exception as error:
                scan_controller.warning.emit(f"无法加载音乐索引：{error}")
        if metadata_preview_controller is not None:
            metadata_preview_controller.results_ready.connect(self._metadata_results_ready)
            metadata_preview_controller.cancelled.connect(self._metadata_cancelled)
            metadata_preview_controller.failed.connect(self._metadata_failed)
            metadata_preview_controller.running_changed.connect(self._metadata_running_changed)
        if safe_rename_controller is not None:
            safe_rename_controller.completed.connect(self._safe_rename_completed)
            safe_rename_controller.cancelled.connect(self._safe_rename_cancelled)
            safe_rename_controller.failed.connect(self._safe_rename_failed)
            safe_rename_controller.running_changed.connect(self._safe_rename_running_changed)
        if lyrics_match_controller is not None:
            lyrics_match_controller.lyrics_changed.connect(self._replace_lyrics_library)
            lyrics_match_controller.results_ready.connect(self._lyrics_results_ready)
            lyrics_match_controller.cancelled.connect(self._lyrics_cancelled)
            lyrics_match_controller.failed.connect(self._lyrics_failed)
            lyrics_match_controller.warning.connect(self._lyrics_warning)
            lyrics_match_controller.match_changed.connect(self._lyrics_warning)
            lyrics_match_controller.running_changed.connect(self._lyrics_running_changed)
            try:
                self._replace_lyrics_library(lyrics_match_controller.load_lyrics_library())
            except Exception as error:
                lyrics_match_controller.warning.emit(f"无法加载歌词索引：{error}")
        if playlist_controller is not None:
            playlist_controller.playlists_changed.connect(self._replace_playlists)
            playlist_controller.playlist_changed.connect(self._replace_playlist)
            playlist_controller.completed.connect(self._playlist_completed)
            playlist_controller.cancelled.connect(self._playlist_cancelled)
            playlist_controller.failed.connect(self._playlist_failed)
            playlist_controller.running_changed.connect(self._background_running_changed)
            try:
                self._replace_playlists(playlist_controller.list_playlists())
            except Exception as error:
                playlist_controller.failed.emit(f"无法加载歌单：{error}")
        if safe_import_controller is not None:
            if hasattr(safe_import_controller, "preview_ready"):
                safe_import_controller.preview_ready.connect(self._safe_import_preview_ready)
                safe_import_controller.preview_cancelled.connect(self._safe_import_preview_cancelled)
                safe_import_controller.preview_failed.connect(self._safe_import_failed)
            safe_import_controller.completed.connect(self._safe_import_completed)
            safe_import_controller.cancelled.connect(self._safe_import_cancelled)
            safe_import_controller.failed.connect(self._safe_import_failed)
            safe_import_controller.warning.connect(self._safe_import_warning)
            safe_import_controller.running_changed.connect(self._safe_import_running_changed)
        if backup_controller is not None:
            backup_controller.completed.connect(self._backup_completed)
            backup_controller.failed.connect(self._backup_failed)
            backup_controller.running_changed.connect(self._background_running_changed)

    def _add_page(self, key: str, page: LibraryPage) -> None:
        self.pages[key] = page
        self.stack.addWidget(page)
        page.delete_requested.connect(lambda records, p=page: self._confirm_delete(p, records))
        page.new_playlist_requested.connect(self.create_playlist)
        page.add_to_playlists_requested.connect(self._add_to_playlists)
        page.open_location_requested.connect(self._open_selected_location)
        page.rename_context_requested.connect(self._rename_selected_context)
        page.rematch_lyrics_requested.connect(self._rematch_selected_lyrics)

    @staticmethod
    def _audio_snapshot_from_record(record: object) -> AudioAssetSnapshot:
        if not isinstance(record, dict):
            raise ValueError("右键选择快照格式无效")
        asset_id = record.get("_asset_id")
        path = record.get("_canonical_path")
        size_bytes = record.get("_size_bytes")
        mtime_ns = record.get("_mtime_ns")
        allowed_root = record.get("_allowed_root")
        file_state = record.get("_file_state")
        if (
            not isinstance(asset_id, str)
            or not asset_id.strip()
            or not isinstance(path, Path)
            or not path.is_absolute()
            or isinstance(size_bytes, bool)
            or not isinstance(size_bytes, int)
            or size_bytes < 0
            or (
                mtime_ns is not None
                and (isinstance(mtime_ns, bool) or not isinstance(mtime_ns, int) or mtime_ns < 0)
            )
            or not isinstance(allowed_root, Path)
            or not allowed_root.is_absolute()
        ):
            raise ValueError("所选音乐缺少有效的 P1 索引快照，请刷新列表后重试")
        if file_state != "active":
            raise ValueError("所选音乐不是可操作状态，请重新扫描并刷新列表后重试")
        return AudioAssetSnapshot(asset_id, path, size_bytes, mtime_ns, allowed_root)

    def _revalidate_context_records(
        self,
        records: object,
    ) -> tuple[RevalidatedAudioRecord, ...]:
        if self._scan_controller is None:
            raise ValueError("当前没有可用的音乐索引重验服务")
        if not isinstance(records, tuple) or not records:
            raise ValueError("请先选中或勾选音乐")
        snapshots = tuple(self._audio_snapshot_from_record(record) for record in records)
        return self._scan_controller.revalidate_audio_records(snapshots)

    def _context_status(self, message: str) -> None:
        page = self.pages.get("所有音乐")
        if page is not None:
            page.status.setText(message)

    def _open_selected_location(self, records: object) -> None:
        if self._has_running_background_task():
            self._context_status("已有后台任务运行，请完成后再打开文件位置。")
            return
        try:
            if not isinstance(records, tuple) or len(records) != 1:
                raise ValueError("打开所在文件夹时必须且只能选择一首音乐")
            validated = self._revalidate_context_records(records)
            path = validated[0].canonical_path
            native_path = QDir.toNativeSeparators(str(path))
            launched = QProcess.startDetached("explorer.exe", ["/select,", native_path])
            if isinstance(launched, tuple):
                launched = launched[0]
            if not launched and not QDesktopServices.openUrl(QUrl.fromLocalFile(str(path.parent))):
                raise RuntimeError("系统无法打开资源管理器或文件所在目录")
        except Exception as error:
            self._context_status(f"无法打开所在文件夹：{error}")

    @staticmethod
    def _record_from_revalidated(record: RevalidatedAudioRecord) -> dict[str, object]:
        return {
            "_asset_id": record.asset_id,
            "_canonical_path": record.canonical_path,
            "_allowed_root": record.allowed_root,
            "_file_state": "active",
            "_size_bytes": record.size_bytes,
            "_mtime_ns": record.mtime_ns,
        }

    def _rename_selected_context(self, records: object) -> None:
        if self._has_running_background_task():
            self._context_status("已有后台任务运行，请完成后再重命名。")
            return
        if (
            self._scan_controller is None
            or self._metadata_preview_controller is None
            or self._safe_rename_controller is None
        ):
            self._context_status("当前未启用真实安全重命名，无法执行此操作。")
            return
        try:
            validated = self._revalidate_context_records(records)
        except Exception as error:
            self._context_status(f"无法开始重命名：{error}")
            return
        self.open_rename(tuple(self._record_from_revalidated(record) for record in validated))

    def _rematch_selected_lyrics(self, records: object) -> None:
        if self._lyrics_match_controller is None:
            self._context_status("当前没有可用的歌词匹配服务")
            return
        if self._has_running_background_task():
            self._context_status("已有后台任务运行，请完成后再重新匹配歌词。")
            return
        try:
            validated = self._revalidate_context_records(records)
            self._lyrics_match_controller.set_scope(validated)
        except Exception as error:
            self._context_status(f"无法限定歌词匹配范围：{error}")
            return
        self._lyrics_context_scope_active = True
        self.open_lyrics_match(validated)

    def navigate(self, key: str) -> None:
        page = self.pages.get(key)
        if page is None:
            return
        self.stack.setCurrentWidget(page)
        self.sidebar.select_key(key)

    def _show_window(self, window: QWidget) -> None:
        window.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self._open_windows.append(window)
        window_id = id(window)
        window.destroyed.connect(
            lambda _object=None, tracked_id=window_id: self._remove_open_window(tracked_id)
        )
        window.show()
        window.raise_()
        window.activateWindow()

    def _remove_open_window(self, tracked_id: int) -> None:
        self._open_windows = [window for window in self._open_windows if id(window) != tracked_id]

    def open_import(self) -> None:
        if self._safe_import_controller is not None:
            if self._import_dialog is not None:
                self._import_dialog.show()
                self._import_dialog.raise_()
                self._import_dialog.activateWindow()
                return
            dialog = ImportDialog(self, live_mode=True)
            self._import_dialog = dialog
            dialog.destroyed.connect(lambda: setattr(self, "_import_dialog", None))
            dialog.start_requested.connect(self._start_safe_import)
            dialog.preview_requested.connect(self._start_safe_import_preview)
            dialog.execute_requested.connect(self._execute_safe_import)
            dialog.discard_preview_requested.connect(self._discard_safe_import_preview)
            dialog.scan_existing_requested.connect(self.open_read_only_scan)
            dialog.cancel_requested.connect(self._safe_import_controller.request_cancel)
            dialog.set_running(
                self._safe_import_controller.running,
                getattr(self._safe_import_controller, "phase", "execute"),
            )
            self._show_window(dialog)
            return
        if self._scan_controller is None:
            self._show_window(ImportDialog(self))
            return
        self.open_read_only_scan()

    def open_read_only_scan(self) -> None:
        if self._scan_controller is None:
            if self._import_dialog is not None:
                self._import_dialog.show_failed("当前未启用只读音乐扫描")
            return
        if self._safe_import_controller is not None and self._safe_import_controller.running:
            if self._import_dialog is not None:
                self._import_dialog.show_failed("安全移动导入正在运行，请完成或取消后再扫描")
            return
        if self._safe_import_controller is not None and hasattr(
            self._safe_import_controller, "discard_preview"
        ):
            self._safe_import_controller.discard_preview()
        if self._import_dialog is not None:
            self._import_dialog.clear_preview()
            self._import_dialog.hide()
        if self._scan_dialog is not None:
            self._scan_dialog.show()
            self._scan_dialog.raise_()
            self._scan_dialog.activateWindow()
            return
        dialog = ReadOnlyScanDialog(None, self)
        self._scan_dialog = dialog
        dialog.destroyed.connect(lambda: setattr(self, "_scan_dialog", None))
        dialog.start_requested.connect(self._start_read_only_scan)
        dialog.cancel_requested.connect(self._scan_controller.request_cancel)
        self._scan_controller.batch_committed.connect(dialog.add_batch)
        self._scan_controller.completed.connect(dialog.show_completed)
        self._scan_controller.cancelled.connect(dialog.show_cancelled)
        self._scan_controller.failed.connect(dialog.show_failed)
        self._scan_controller.warning.connect(dialog.show_warning)
        self._scan_controller.running_changed.connect(dialog.set_running)
        if self._has_running_rename_task():
            dialog.show_warning("歌曲信息分析或重命名正在运行，请完成或取消后再开始扫描。")
            dialog.set_running(False)
            self._show_window(dialog)
            return
        remembered = self._scan_controller.remembered_root()
        if remembered is not None:
            dialog.path_input.setText(str(remembered))
        dialog.set_running(self._scan_controller.running)
        self._show_window(dialog)

    def _start_safe_import(self, source_root: Path, target_root: Path, mode: str) -> None:
        self._start_safe_import_preview(source_root, target_root, mode)

    def _start_safe_import_preview(self, source_root: Path, target_root: Path, mode: str) -> None:
        if self._safe_import_controller is None or self._import_dialog is None:
            return
        if self._has_running_background_task():
            self._import_dialog.show_failed("已有后台任务运行，请完成后再导入")
            return
        try:
            starter = getattr(
                self._safe_import_controller,
                "start_preview",
                self._safe_import_controller.start,
            )
            starter(source_root, target_root, mode)
        except Exception as error:
            self._import_dialog.show_failed(str(error))

    def _safe_import_preview_ready(self, plan: object) -> None:
        if self._import_dialog is not None:
            self._import_dialog.show_preview(plan)

    def _safe_import_preview_cancelled(self) -> None:
        if self._import_dialog is not None:
            self._import_dialog.show_failed("已取消生成预览")

    def _discard_safe_import_preview(self) -> None:
        if (
            self._safe_import_controller is None
            or self._safe_import_controller.running
            or not hasattr(self._safe_import_controller, "discard_preview")
        ):
            return
        self._safe_import_controller.discard_preview()

    def _execute_safe_import(self, plan_id: str) -> None:
        if self._safe_import_controller is None or self._import_dialog is None:
            return
        if self._has_running_background_task():
            self._import_dialog.show_failed("已有后台任务运行，请完成后再导入")
            return
        plan = self._safe_import_controller.current_plan
        if plan is None or plan.id != plan_id:
            self._import_dialog.show_failed("预览已失效，请重新生成")
            return
        answer = QMessageBox.question(
            self._import_dialog,
            "确认安全移动导入",
            f"模式：{'音频' if plan.mode == 'audio' else '歌词'}\n"
            f"可执行 {plan.ready_count} · 重复 {plan.duplicate_count} · "
            f"冲突 {plan.conflict_count} · 失败 {plan.failure_count}\n"
            f"源目录：{plan.source_root}\n目标目录：{plan.target_root}\n"
            "确认后才会移动文件，是否继续？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        if self._has_running_background_task():
            self._import_dialog.show_failed("确认期间已有后台任务启动，请完成后重新执行预览")
            return
        try:
            self._safe_import_controller.start_execute(plan_id)
        except Exception as error:
            self._import_dialog.show_failed(str(error))

    def _safe_import_completed(self, result: object) -> None:
        if self._import_dialog is not None:
            self._import_dialog.show_result(result)
        if getattr(result, "success_count", 0):
            action = getattr(result, "action", "import")
            mode = getattr(result, "mode", "audio")
            root = getattr(
                result,
                "source_root" if action == "undo" else "target_root",
                None,
            )
            if isinstance(root, Path) and mode in {"audio", "lyrics"}:
                self._queue_library_refresh(
                    (("audio" if mode == "audio" else "lyric", root),)
                )
            elif self._import_dialog is not None:
                self._import_dialog.show_failed("文件操作已完成，但刷新信息无效，请手动重新扫描。")

    def _safe_import_cancelled(self, result: object) -> None:
        if self._import_dialog is not None:
            self._import_dialog.show_result(result, cancelled=True)

    def _safe_import_failed(self, message: str) -> None:
        if self._import_dialog is not None:
            self._import_dialog.show_failed(message)

    def _safe_import_warning(self, message: str) -> None:
        if self._import_dialog is not None:
            self._import_dialog.summary.setText(f"警告：{message}")

    def _safe_import_running_changed(self, running: bool) -> None:
        if self._import_dialog is not None:
            phase = getattr(self._safe_import_controller, "phase", "execute")
            self._import_dialog.set_running(running, phase)
        self._background_running_changed(running)

    def _start_read_only_scan(self, root) -> None:
        if self._scan_controller is None:
            return
        if self._has_running_background_task():
            if self._scan_dialog is not None:
                self._scan_dialog.show_failed("已有后台任务运行，不能同时开始扫描")
            return
        try:
            self._scan_controller.start_scan(root)
        except Exception as error:
            if self._scan_dialog is not None:
                self._scan_dialog.show_failed(str(error))

    def _replace_music_library(self, records) -> None:
        self.pages["所有音乐"].replace_data(records)

    def _replace_lyrics_library(self, records) -> None:
        self.pages["所有歌词"].replace_data(records)

    def _background_running_changed(self, running: bool) -> None:
        if self._settings_dialog is not None:
            self._settings_dialog.set_maintenance_running(
                self._has_running_background_task()
            )
        if self._close_pending and not self._has_running_background_task():
            QTimer.singleShot(0, self.close)
            return
        if not running:
            QTimer.singleShot(0, self._drain_library_refresh)

    def _queue_library_refresh(self, roots: object) -> None:
        if self._close_pending:
            return
        if not isinstance(roots, (tuple, list)):
            return
        for value in roots:
            if (
                not isinstance(value, tuple)
                or len(value) != 2
                or value[0] not in {"audio", "lyric"}
                or not isinstance(value[1], Path)
                or not value[1].is_absolute()
            ):
                continue
            item = (value[0], value[1])
            if item not in self._pending_refresh_roots:
                self._pending_refresh_roots.append(item)
        self._drain_library_refresh()

    def _drain_library_refresh(self) -> None:
        if self._close_pending or self._has_running_background_task():
            return
        while self._pending_refresh_roots:
            kind, root = self._pending_refresh_roots.pop(0)
            controller = (
                self._scan_controller if kind == "audio" else self._lyrics_match_controller
            )
            if controller is None:
                current = self.stack.currentWidget()
                if isinstance(current, LibraryPage):
                    current.status.setText(
                        f"文件操作已完成，但缺少{'音乐' if kind == 'audio' else '歌词'}刷新服务，请手动扫描。"
                    )
                continue
            try:
                controller.start_scan(root)
                return
            except Exception as error:
                current = self.stack.currentWidget()
                if isinstance(current, LibraryPage):
                    current.status.setText(f"无法刷新 {root}：{error}")

    def open_rename(self, records: object | None = None) -> None:
        if self._metadata_preview_controller is None or self._scan_controller is None:
            self._show_window(RenamePreviewDialog(self))
            return
        if self._rename_dialog is not None and records is None:
            self._rename_dialog.show()
            self._rename_dialog.raise_()
            self._rename_dialog.activateWindow()
            return

        if self._rename_dialog is None:
            dialog = RenamePreviewDialog(
                self,
                live_mode=True,
                execution_enabled=self._safe_rename_controller is not None,
            )
            self._rename_dialog = dialog
            dialog.destroyed.connect(lambda: setattr(self, "_rename_dialog", None))
            dialog.cancel_requested.connect(self._cancel_active_rename_task)
            if self._safe_rename_controller is not None:
                dialog.execution_requested.connect(self._start_safe_rename)
            self._show_window(dialog)
        else:
            dialog = self._rename_dialog
            dialog.show()
            dialog.raise_()
            dialog.activateWindow()

        page = self.pages["所有音乐"]
        if self.stack.currentWidget() is not page:
            dialog.show_warning("请先切换到“所有音乐”并明确选择要分析的音乐。")
            return
        if self._scan_controller is not None and self._scan_controller.running:
            dialog.show_warning("扫描正在运行，请完成或取消后再分析歌曲信息。")
            return
        if self._has_running_background_task():
            dialog.show_warning("已有后台任务运行，请完成或取消后再分析歌曲信息。")
            return
        selected_records = page.selected_records() if records is None else records
        if not isinstance(selected_records, (tuple, list)) or not selected_records:
            dialog.show_warning("请先勾选或选中至少一首音乐，再开始只读分析。")
            return
        try:
            items = tuple(self._metadata_input_from_record(record) for record in selected_records)
            self._metadata_inputs_by_asset = {item.asset_id: item for item in items}
            self._metadata_results_by_asset.clear()
            self._metadata_preview_controller.start(items)
        except Exception as error:
            dialog.show_failed(str(error))

    @staticmethod
    def _metadata_input_from_record(record: dict[str, object]) -> MetadataPreviewInput:
        required = (
            "_asset_id",
            "_canonical_path",
            "_allowed_root",
            "_file_state",
            "_size_bytes",
            "_mtime_ns",
        )
        missing = [key for key in required if key not in record]
        if missing:
            raise ValueError(f"所选记录缺少 P1 索引信息：{', '.join(missing)}")
        if record["_allowed_root"] is None:
            raise ValueError("所选音乐缺少可验证的已完成扫描来源，请重新扫描其所在目录。")
        return MetadataPreviewInput(
            asset_id=record["_asset_id"],  # type: ignore[arg-type]
            canonical_path=record["_canonical_path"],  # type: ignore[arg-type]
            allowed_root=record["_allowed_root"],  # type: ignore[arg-type]
            file_state=record["_file_state"],  # type: ignore[arg-type]
            size_bytes=record["_size_bytes"],  # type: ignore[arg-type]
            mtime_ns=record["_mtime_ns"],  # type: ignore[arg-type]
        )

    def _metadata_results_ready(self, results: object) -> None:
        if isinstance(results, (tuple, list)):
            self._metadata_results_by_asset = {
                result.asset_id: result
                for result in results
                if hasattr(result, "asset_id")
            }
        if self._rename_dialog is not None:
            self._rename_dialog.show_results(results)

    def _metadata_cancelled(self, count: int) -> None:
        if self._rename_dialog is not None:
            self._rename_dialog.show_cancelled(count)

    def _metadata_failed(self, message: str) -> None:
        if self._rename_dialog is not None:
            self._rename_dialog.show_failed(message)

    def _metadata_running_changed(self, running: bool) -> None:
        if self._rename_dialog is not None:
            self._rename_dialog.set_running(running)
        self._background_running_changed(running)

    def _cancel_active_rename_task(self) -> None:
        if self._safe_rename_controller is not None and self._safe_rename_controller.running:
            self._safe_rename_controller.request_cancel()
        elif self._metadata_preview_controller is not None and self._metadata_preview_controller.running:
            self._metadata_preview_controller.request_cancel()

    def _has_running_rename_task(self) -> bool:
        return bool(
            (self._metadata_preview_controller is not None and self._metadata_preview_controller.running)
            or (self._safe_rename_controller is not None and self._safe_rename_controller.running)
            or (self._lyrics_match_controller is not None and self._lyrics_match_controller.running)
            or (self._playlist_controller is not None and self._playlist_controller.running)
        )

    def _start_safe_rename(self, requests: object) -> None:
        if self._safe_rename_controller is None or self._rename_dialog is None:
            return
        if self._has_running_background_task():
            self._rename_dialog.show_warning("已有后台任务运行，不能同时重命名。")
            return
        if not isinstance(requests, (tuple, list)):
            self._rename_dialog.show_warning("重命名选择格式无效。")
            return
        try:
            items: list[SafeRenameInput] = []
            sync_requested = self._rename_dialog.id3_checkbox.isChecked()
            for request in requests:
                if not isinstance(request, tuple) or len(request) != 2:
                    raise ValueError("重命名选择格式无效")
                asset_id, suggested_stem = request
                if not isinstance(asset_id, str) or not isinstance(suggested_stem, str):
                    raise ValueError("重命名选择字段无效")
                preview_input = self._metadata_inputs_by_asset.get(asset_id)
                preview_result = self._metadata_results_by_asset.get(asset_id)
                if preview_input is None or preview_result is None:
                    raise ValueError("预览快照已失效，请重新分析")
                if getattr(preview_result, "canonical_path", None) != preview_input.canonical_path:
                    raise ValueError("预览路径与索引快照不一致，请重新分析")
                extension = getattr(preview_result, "extension", None)
                if not isinstance(extension, str) or not extension:
                    raise ValueError("预览扩展名无效")
                sync_metadata = sync_requested and extension.casefold() in {".mp3", ".flac", ".m4a"}
                metadata_title: str | None = None
                metadata_artist: str | None = None
                if sync_metadata:
                    if "-" not in suggested_stem:
                        raise ValueError(
                            f"{suggested_stem}{extension}：同步标签时建议名称必须包含最后一个半角 '-'"
                        )
                    metadata_title, metadata_artist = (
                        value.strip() for value in suggested_stem.rsplit("-", 1)
                    )
                    if not metadata_title or not metadata_artist:
                        raise ValueError(
                            f"{suggested_stem}{extension}：同步标签时歌名和歌手都不能为空"
                        )
                items.append(
                    SafeRenameInput(
                        asset_id=asset_id,
                        source_path=preview_input.canonical_path,
                        target_path=preview_input.canonical_path.parent / f"{suggested_stem}{extension}",
                        allowed_root=preview_input.allowed_root,
                        expected_size_bytes=preview_input.size_bytes,
                        expected_mtime_ns=preview_input.mtime_ns,
                        sync_metadata=sync_metadata,
                        metadata_title=metadata_title,
                        metadata_artist=metadata_artist,
                    )
                )
            if not items:
                raise ValueError("请至少选择一个重命名项")
        except Exception as error:
            self._rename_dialog.show_warning(str(error))
            return

        confirmation_message = (
            f"将实际修改 {len(items)} 个文件名。\n\n"
            "所有文件只在原目录内重命名，目标存在时绝不覆盖。\n"
        )
        confirmation_message += (
            "MP3、FLAC、M4A 会先在候选副本同步 Title/Artist，回读验证后才替换；失败会恢复原文件。\n"
            if any(item.sync_metadata for item in items)
            else "本次不会写入音频标签。\n"
        )
        confirmation_message += "是否继续？"
        answer = QMessageBox.question(
            self._rename_dialog,
            "确认安全重命名",
            confirmation_message,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            self._rename_dialog.show_warning("已取消确认；没有创建操作，也没有修改文件。")
            return
        try:
            self._safe_rename_controller.start(tuple(items))
        except Exception as error:
            self._rename_dialog.show_failed(str(error))

    def _safe_rename_completed(self, result: object) -> None:
        if self._rename_dialog is not None:
            self._rename_dialog.show_rename_completed(result)
        self._reload_library_after_rename()
        if self._playlist_controller is not None and self._playlist_controller.remembered_root() is not None:
            try:
                retarget_items = tuple(
                    PlaylistRetargetInput(
                        source_path=item.source_path,
                        target_path=item.target_path,
                        audio_root=self._metadata_inputs_by_asset[item.asset_id].allowed_root,
                    )
                    for item in getattr(result, "items", ())
                    if getattr(item, "result", None) == "success"
                    and item.asset_id in self._metadata_inputs_by_asset
                )
                if retarget_items:
                    self._playlist_controller.start_retarget(retarget_items)
            except Exception as error:
                if self._rename_dialog is not None:
                    self._rename_dialog.show_warning(f"音乐已重命名，但歌单快捷方式联动失败：{error}")

    def _safe_rename_cancelled(self, result: object) -> None:
        if self._rename_dialog is not None:
            self._rename_dialog.show_rename_cancelled(result)
        self._reload_library_after_rename()

    def _safe_rename_failed(self, message: str) -> None:
        if self._rename_dialog is not None:
            self._rename_dialog.show_failed(f"重命名失败：{message}")
        self._reload_library_after_rename()

    def _safe_rename_running_changed(self, running: bool) -> None:
        if self._rename_dialog is not None:
            self._rename_dialog.set_running(running)
        self._background_running_changed(running)

    def _reload_library_after_rename(self) -> None:
        if self._scan_controller is None:
            return
        try:
            self._replace_music_library(self._scan_controller.load_library())
        except Exception as error:
            if self._rename_dialog is not None:
                self._rename_dialog.show_warning(f"重命名结果已记录，但列表刷新失败：{error}")

    def open_lyrics_match(self, scope: object | None = None) -> None:
        if self._lyrics_match_controller is None:
            self._show_window(LyricsMatchDialog(self))
            return
        if scope is None:
            self._clear_lyrics_context_scope()
        else:
            self._lyrics_context_scope_active = True
        if self._lyrics_dialog is not None:
            if scope is not None:
                self._lyrics_dialog.show_warning(
                    f"已限定 {len(scope) if isinstance(scope, tuple) else 0} 首音乐；请选择歌词目录并点击开始。"
                )
            else:
                self._lyrics_dialog.show_warning("已切换为全库歌词匹配；请选择歌词目录并点击开始。")
            self._lyrics_dialog.show()
            self._lyrics_dialog.raise_()
            self._lyrics_dialog.activateWindow()
            return
        dialog = LyricsMatchDialog(self, live_mode=True)
        self._lyrics_dialog = dialog
        dialog.destroyed.connect(lambda: setattr(self, "_lyrics_dialog", None))
        dialog.destroyed.connect(self._clear_lyrics_context_scope)
        dialog.scan_requested.connect(self._start_lyrics_scan)
        dialog.candidate_requested.connect(self._commit_lyrics_candidate)
        dialog.cancel_match_requested.connect(self._cancel_lyrics_match)
        dialog.cancel_scan_requested.connect(self._lyrics_match_controller.request_cancel)
        remembered = self._lyrics_match_controller.remembered_root()
        if remembered is not None:
            dialog.path_input.setText(str(remembered))
        dialog.set_running(self._lyrics_match_controller.running)
        if scope is not None:
            dialog.show_warning(
                f"已限定 {len(scope) if isinstance(scope, tuple) else 0} 首音乐；请选择歌词目录并点击开始。"
            )
        self._show_window(dialog)

    def _clear_lyrics_context_scope(self) -> None:
        self._lyrics_context_scope_active = False
        controller = self._lyrics_match_controller
        clear_scope = getattr(controller, "clear_scope", None)
        if controller is not None and not controller.running and callable(clear_scope):
            clear_scope()

    def _start_lyrics_scan(self, root: Path) -> None:
        controller = self._lyrics_match_controller
        if controller is None or self._lyrics_dialog is None:
            return
        if self._lyrics_context_scope_active and controller.pending_scope is None:
            self._lyrics_dialog.show_warning(
                "本次限定范围已使用；请关闭后重新右键选择，或从顶部工具栏打开全库匹配。"
            )
            return
        if self._has_running_background_task():
            self._lyrics_dialog.show_warning("已有后台任务运行，请完成后再匹配歌词。")
            return
        try:
            controller.start_scan(root)
        except Exception as error:
            self._lyrics_dialog.show_warning(f"无法开始歌词扫描：{error}")

    def _lyrics_results_ready(self, result: object) -> None:
        if self._lyrics_dialog is not None:
            self._lyrics_dialog.show_results(result)

    def _lyrics_cancelled(self, count: int) -> None:
        if self._lyrics_dialog is not None:
            self._lyrics_dialog.show_warning(f"歌词扫描已取消；已安全提交 {count} 个索引。")

    def _lyrics_failed(self, message: str) -> None:
        if self._lyrics_dialog is not None:
            self._lyrics_dialog.show_warning(f"歌词扫描失败：{message}")

    def _lyrics_warning(self, message: str) -> None:
        if self._lyrics_dialog is not None:
            self._lyrics_dialog.show_warning(message)

    def _lyrics_running_changed(self, running: bool) -> None:
        if self._lyrics_dialog is not None:
            self._lyrics_dialog.set_running(running)
        self._background_running_changed(running)

    def _commit_lyrics_candidate(self, token: str) -> None:
        if self._lyrics_match_controller is None or self._lyrics_dialog is None:
            return
        try:
            self._lyrics_match_controller.commit_candidate(token)
        except Exception as error:
            self._lyrics_dialog.show_warning(f"无法保存歌词匹配：{error}")

    def _cancel_lyrics_match(self, audio_asset_id: str) -> None:
        if self._lyrics_match_controller is None or self._lyrics_dialog is None:
            return
        try:
            self._lyrics_match_controller.cancel_current_match(audio_asset_id)
        except Exception as error:
            self._lyrics_dialog.show_warning(f"无法取消歌词匹配：{error}")

    def open_history(self) -> None:
        live_mode = any(
            controller is not None
            for controller in (
                self._safe_import_controller,
                self._safe_rename_controller,
                self._backup_controller,
                self._playlist_controller,
                self._lyrics_match_controller,
            )
        )
        if not live_mode:
            if self._history_dialog is not None:
                self._history_dialog.show()
                self._history_dialog.raise_()
                self._history_dialog.activateWindow()
                return
            self._show_window(HistoryDialog(self))
            return

        snapshot = HistoryService(
            import_controller=self._safe_import_controller,
            rename_controller=self._safe_rename_controller,
            backup_controller=self._backup_controller,
            playlist_controller=self._playlist_controller,
            lyrics_controller=self._lyrics_match_controller,
        ).load()
        if self._history_dialog is not None:
            self._history_dialog.set_snapshot(snapshot)
            self._history_dialog.show()
            self._history_dialog.raise_()
            self._history_dialog.activateWindow()
            return
        dialog = HistoryDialog(self, snapshot=snapshot)
        self._history_dialog = dialog
        dialog.destroyed.connect(lambda: setattr(self, "_history_dialog", None))
        dialog.restore_requested.connect(self._restore_backups)
        dialog.cleanup_requested.connect(self._cleanup_backups)
        dialog.undo_import_requested.connect(self._undo_last_import)
        self._show_window(dialog)

    def _restore_backups(self, entry_ids: object) -> None:
        if self._backup_controller is None or not isinstance(entry_ids, tuple):
            return
        if self._has_running_background_task():
            current = self.stack.currentWidget()
            if isinstance(current, LibraryPage):
                current.status.setText("已有后台任务运行，请完成后再恢复备份。")
            return
        try:
            self._backup_controller.start_restore(entry_ids)
        except Exception as error:
            current = self.stack.currentWidget()
            if isinstance(current, LibraryPage):
                current.status.setText(f"无法恢复备份：{error}")

    def _undo_last_import(self) -> None:
        if self._safe_import_controller is None:
            return
        if self._has_running_background_task():
            current = self.stack.currentWidget()
            if isinstance(current, LibraryPage):
                current.status.setText("已有后台任务运行，请完成后再撤销导入。")
            return
        answer = QMessageBox.question(
            self._history_dialog,
            "确认撤销导入",
            "只会撤销最近一次完整成功且文件未变化的导入；遇到冲突会停止并恢复现场。是否继续？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            self._safe_import_controller.undo_last_complete()
        except Exception as error:
            current = self.stack.currentWidget()
            if isinstance(current, LibraryPage):
                current.status.setText(f"无法撤销导入：{error}")

    def _settings_message(self, message: str, dialog: SettingsDialog | None = None) -> None:
        target = dialog or self._settings_dialog
        if target is not None:
            target.show_message(message)
            return
        current = self.stack.currentWidget()
        if isinstance(current, LibraryPage):
            current.status.setText(message)

    def _cleanup_backups(self, parent: QWidget | None = None) -> None:
        if self._backup_controller is None:
            return
        settings_dialog = parent if isinstance(parent, SettingsDialog) else None
        if self._has_running_background_task():
            self._settings_message("已有后台任务运行，请完成后再清理备份。", settings_dialog)
            return
        try:
            preview = self._backup_controller.cleanup_preview()
        except Exception as error:
            self._settings_message(f"无法预览过期备份：{error}", settings_dialog)
            return
        if preview.retention_days is None:
            self._settings_message("当前设置为永久保留，没有到期备份可清理。", settings_dialog)
            return
        if preview.eligible_count <= 0:
            self._settings_message("当前没有超过保留期限的备份。", settings_dialog)
            return
        answer = QMessageBox.warning(
            parent or self._history_dialog,
            "确认永久清理",
            f"将永久删除 {preview.eligible_count} 个超过 {preview.retention_days} 天且尚未恢复的备份。\n"
            f"范围：{preview.backup_root}\n"
            "此操作不可撤销，是否继续？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            self._backup_controller.start_cleanup()
            if settings_dialog is not None:
                settings_dialog.set_maintenance_running(True)
                settings_dialog.show_message("正在后台清理已确认的过期备份…")
        except Exception as error:
            self._settings_message(f"无法清理备份：{error}", settings_dialog)

    def open_settings(self) -> None:
        if self._backup_controller is None:
            self._show_window(SettingsDialog(self))
            return
        if self._settings_dialog is not None:
            self._settings_dialog.show()
            self._settings_dialog.raise_()
            self._settings_dialog.activateWindow()
            return
        try:
            dialog = SettingsDialog(
                self,
                live_mode=True,
                retention_days=self._backup_controller.retention_days(),
                remembered_paths=self._remembered_settings_paths(),
                backup_root=self._backup_controller.backup_root,
            )
            self._settings_dialog = dialog
            dialog.destroyed.connect(lambda: setattr(self, "_settings_dialog", None))
            dialog.save_requested.connect(
                lambda values, current=dialog: self._save_settings(values, current)
            )
            dialog.cleanup_requested.connect(
                lambda current=dialog: self._cleanup_backups(current)
            )
            dialog.open_backup_requested.connect(
                lambda current=dialog: self._open_backup_directory(current)
            )
            dialog.rescan_requested.connect(
                lambda current=dialog: self._rescan_remembered_libraries(current)
            )
            dialog.set_maintenance_running(self._has_running_background_task())
            self._show_window(dialog)
        except Exception as error:
            current = self.stack.currentWidget()
            if isinstance(current, LibraryPage):
                current.status.setText(f"无法打开设置：{error}")

    def _remembered_settings_paths(self) -> dict[str, Path | None]:
        paths: dict[str, Path | None] = {"audio": None, "lyrics": None, "playlist": None}
        for key, controller in (
            ("audio", self._scan_controller),
            ("lyrics", self._lyrics_match_controller),
            ("playlist", self._playlist_controller),
        ):
            if controller is None:
                continue
            try:
                value = controller.remembered_root()
            except Exception:
                continue
            if isinstance(value, Path) and value.is_absolute():
                paths[key] = value
        return paths

    def _save_settings(
        self,
        values: object,
        dialog: SettingsDialog | None = None,
    ) -> None:
        if self._backup_controller is None or not isinstance(values, dict):
            return
        try:
            self._backup_controller.set_retention_days(values.get("backup_retention_days"))
        except Exception as error:
            self._settings_message(f"设置保存失败：{error}", dialog)
            return
        target = dialog or self._settings_dialog
        if target is not None:
            target.complete_save()

    def _open_backup_directory(self, dialog: SettingsDialog) -> None:
        if self._backup_controller is None:
            return
        try:
            root = self._backup_controller.prepare_backup_root()
            opened = QDesktopServices.openUrl(QUrl.fromLocalFile(str(root)))
            if not opened:
                raise RuntimeError("系统未能打开该目录")
        except Exception as error:
            dialog.show_message(f"无法打开备份目录：{error}")
            return
        dialog.show_message(f"已打开备份目录：{root}")

    def _rescan_remembered_libraries(self, dialog: SettingsDialog) -> None:
        if self._has_running_background_task():
            dialog.show_message("已有后台任务运行，请完成后再重新检查。")
            return
        paths = self._remembered_settings_paths()
        roots = tuple(
            (kind, path)
            for kind, path in (
                ("audio", paths["audio"]),
                ("lyric", paths["lyrics"]),
            )
            if path is not None
        )
        if not roots:
            dialog.show_message("尚未记住音乐或歌词目录，请先在对应功能中选择并完成扫描。")
            return
        dialog.show_message(f"正在重新检查 {len(roots)} 个已记住目录…")
        self._queue_library_refresh(roots)

    def _replace_playlists(self, names: object) -> None:
        if self._playlist_controller is None or not isinstance(names, (tuple, list)):
            return
        clean_names = tuple(str(name) for name in names)
        wanted = {f"playlist:{name}" for name in clean_names}
        for key in tuple(self.pages):
            if not key.startswith("playlist:") or key in wanted:
                continue
            page = self.pages.pop(key)
            self.stack.removeWidget(page)
            page.deleteLater()
        self.sidebar.set_playlists(clean_names)
        for name in clean_names:
            key = f"playlist:{name}"
            if key not in self.pages:
                self._add_page(
                    key,
                    LibraryPage(
                        name,
                        (),
                        display_count=0,
                        playlist_name=name,
                        live_mode=True,
                        use_model_view=self._use_model_view,
                        playlist_names=clean_names,
                    ),
                )
            try:
                self._replace_playlist(name, self._playlist_controller.load_playlist(name))
            except Exception as error:
                self.pages[key].status.setText(f"无法读取歌单：{error}")
        for page in self.pages.values():
            page.set_playlist_names(clean_names)

    def _replace_playlist(self, name: str, records: object) -> None:
        page = self.pages.get(f"playlist:{name}")
        if page is not None and isinstance(records, (tuple, list)):
            page.replace_data(records)

    def _add_to_playlists(self, records: object, names: object) -> None:
        if self._playlist_controller is None:
            return
        if self._has_running_background_task():
            self.pages["所有音乐"].status.setText("已有后台任务运行，请完成后再添加歌单。")
            return
        if not isinstance(records, list) or not isinstance(names, list) or not records or not names:
            self.pages["所有音乐"].status.setText("请选择音乐和至少一个歌单。")
            return
        try:
            inputs = tuple(self._playlist_audio_input(record) for record in records)
        except Exception as error:
            self.pages["所有音乐"].status.setText(f"无法添加到歌单：{error}")
            return
        self._playlist_add_queue = [(str(name), inputs) for name in names]
        self._start_next_playlist_add()

    @staticmethod
    def _playlist_audio_input(record: dict[str, object]) -> PlaylistAudioInput:
        required = ("_asset_id", "_canonical_path", "_allowed_root", "_file_state")
        missing = [key for key in required if key not in record]
        if missing or record.get("_allowed_root") is None:
            raise ValueError("所选音乐缺少可验证的 P1 扫描来源，请重新扫描")
        return PlaylistAudioInput(
            asset_id=record["_asset_id"],  # type: ignore[arg-type]
            target_path=record["_canonical_path"],  # type: ignore[arg-type]
            audio_root=record["_allowed_root"],  # type: ignore[arg-type]
            file_state=record["_file_state"],  # type: ignore[arg-type]
        )

    def _start_next_playlist_add(self) -> None:
        if self._playlist_controller is None or not self._playlist_add_queue:
            return
        if self._has_running_background_task():
            self._playlist_add_queue.clear()
            self.pages["所有音乐"].status.setText("已有后台任务运行，已停止后续歌单操作。")
            return
        name, inputs = self._playlist_add_queue.pop(0)
        try:
            self._playlist_controller.start_add(name, inputs)
        except Exception as error:
            self._playlist_add_queue.clear()
            self.pages["所有音乐"].status.setText(f"无法添加到歌单：{error}")

    def _playlist_completed(self, result: object) -> None:
        page = self.pages.get("所有音乐")
        if page is not None:
            success = getattr(result, "success_count", 0)
            skipped = getattr(result, "skipped_count", 0)
            failed = getattr(result, "failure_count", 0)
            page.status.setText(f"歌单操作完成：成功 {success}，跳过 {skipped}，失败 {failed}")
        QTimer.singleShot(0, self._start_next_playlist_add)

    def _playlist_cancelled(self, result: object) -> None:
        self._playlist_add_queue.clear()
        page = self.pages.get("所有音乐")
        if page is not None:
            page.status.setText(f"歌单操作已取消；已完成 {getattr(result, 'success_count', 0)} 项")

    def _playlist_failed(self, message: str) -> None:
        self._playlist_add_queue.clear()
        page = self.pages.get("所有音乐")
        if page is not None:
            page.status.setText(f"歌单操作失败：{message}")

    def create_playlist(self) -> None:
        if self._has_running_background_task():
            self.pages["所有音乐"].status.setText("已有后台任务运行，请完成后再创建歌单。")
            return
        dialog = CreatePlaylistDialog(self)
        if dialog.exec() != dialog.DialogCode.Accepted:
            return
        name = dialog.name_input.text().strip()
        if not name:
            return
        if self._playlist_controller is None:
            self.sidebar.add_playlist(name)
            page = LibraryPage(name, [], display_count=0, playlist_name=name)
            self._add_page(f"playlist:{name}", page)
            self.navigate(f"playlist:{name}")
            return
        if self._playlist_controller.running:
            return
        try:
            if self._playlist_controller.remembered_root() is None:
                selected = QFileDialog.getExistingDirectory(self, "选择歌单根目录")
                if not selected:
                    return
                self._playlist_controller.set_root(Path(selected))
            created = self._playlist_controller.create_playlist(name)
            self.navigate(f"playlist:{created}")
        except Exception as error:
            self.pages["所有音乐"].status.setText(f"无法创建歌单：{error}")

    def _confirm_delete(self, page: LibraryPage, records: list[dict]) -> None:
        if not records:
            return
        if self._has_running_background_task():
            page.status.setText("已有后台任务运行，请完成后再执行删除或移除。")
            return
        dialog = self._create_delete_dialog(page, records)
        if dialog.exec() != dialog.DialogCode.Accepted:
            return
        if page.playlist_name and self._playlist_controller is not None:
            try:
                items = tuple(
                    PlaylistRemovalInput(
                        shortcut_path=record["_shortcut_path"],  # type: ignore[arg-type]
                        expected_target=record["_target_path"],  # type: ignore[arg-type]
                    )
                    for record in records
                    if record.get("_target_path") is not None
                )
                if len(items) != len(records):
                    raise ValueError("损坏或目标未索引的快捷方式需要先修复，不能盲目移除")
                self._playlist_controller.start_remove(page.playlist_name, items)
            except Exception as error:
                page.status.setText(f"无法从歌单移除：{error}")
        elif self._backup_controller is not None and page.kind in {"music", "lyrics"}:
            try:
                items = tuple(self._backup_input(record, page.kind) for record in records)
                self._backup_controller.start_backup(items)
            except Exception as error:
                page.status.setText(f"无法备份删除：{error}")

    @staticmethod
    def _backup_input(record: dict[str, object], kind: str) -> BackupInput:
        allowed_root = record.get("_allowed_root")
        path = record.get("_canonical_path")
        asset_id = record.get("_asset_id")
        if not isinstance(asset_id, str) or not isinstance(path, Path) or not isinstance(allowed_root, Path):
            raise ValueError("所选记录缺少可验证的扫描来源，请重新扫描")
        if record.get("_file_state", "active") != "active":
            raise ValueError("只允许备份删除 active 文件")
        return BackupInput(asset_id, path, allowed_root, "audio" if kind == "music" else "lyric")

    def _backup_completed(self, result: object) -> None:
        action = getattr(result, "action", "backup")
        action_label = {
            "backup": "已安全移入备份",
            "restore": "已恢复备份",
            "cleanup": "已永久清理到期备份",
        }.get(action, "备份操作已完成")
        message = f"{action_label} {getattr(result, 'success_count', 0)} 项，失败 {getattr(result, 'failure_count', 0)} 项"
        current = self.stack.currentWidget()
        if isinstance(current, LibraryPage):
            current.status.setText(message)
        if self._settings_dialog is not None:
            self._settings_dialog.set_maintenance_running(False)
            self._settings_dialog.show_message(message)
        if self._history_dialog is not None:
            self._history_dialog.close()
        if action in {"backup", "restore"}:
            self._queue_library_refresh(getattr(result, "affected_roots", ()))

    def _backup_failed(self, message: str) -> None:
        if self._settings_dialog is not None:
            self._settings_dialog.set_maintenance_running(False)
            self._settings_dialog.show_message(f"备份操作失败：{message}")
        current = self.stack.currentWidget()
        if isinstance(current, LibraryPage):
            current.status.setText(f"备份删除失败：{message}")

    def _create_delete_dialog(self, page: LibraryPage, records: list[dict]) -> QDialog:
        if page.playlist_name:
            return RemovePlaylistItemsDialog(len(records), self)
        if page.kind == "music":
            return DeleteConfirmDialog(
                records,
                self,
                live_mode=self._backup_controller is not None,
            )
        if page.kind == "lyrics":
            return DeleteLyricsConfirmDialog(
                records,
                self,
                live_mode=self._backup_controller is not None,
            )
        raise ValueError(f"不支持的页面类型：{page.kind}")

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._has_running_background_task():
            first_request = not self._close_pending
            self._close_pending = True
            self._pending_refresh_roots.clear()
            self._playlist_add_queue.clear()
            if first_request:
                for controller in (
                    self._scan_controller,
                    self._metadata_preview_controller,
                    self._safe_rename_controller,
                    self._lyrics_match_controller,
                    self._playlist_controller,
                    self._safe_import_controller,
                    self._backup_controller,
                ):
                    if controller is not None and controller.running:
                        try:
                            controller.request_cancel()
                        except Exception:
                            continue
            event.ignore()
            return
        self._close_pending = True
        self._pending_refresh_roots.clear()
        self._playlist_add_queue.clear()
        self._close_auxiliary_windows()
        super().closeEvent(event)

    def _close_auxiliary_windows(self) -> None:
        for window in tuple(self._open_windows):
            try:
                window.close()
            except RuntimeError:
                self._remove_open_window(id(window))

    def _has_running_background_task(self) -> bool:
        return bool(
            (self._scan_controller is not None and self._scan_controller.running)
            or (
                self._metadata_preview_controller is not None
                and self._metadata_preview_controller.running
            )
            or (
                self._safe_rename_controller is not None
                and self._safe_rename_controller.running
            )
            or (
                self._lyrics_match_controller is not None
                and self._lyrics_match_controller.running
            )
            or (
                self._playlist_controller is not None
                and self._playlist_controller.running
            )
            or (
                self._safe_import_controller is not None
                and self._safe_import_controller.running
            )
            or (
                self._backup_controller is not None
                and self._backup_controller.running
            )
        )
