from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import QTimer, Qt
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import QApplication, QDialog, QHBoxLayout, QMainWindow, QMessageBox, QStackedWidget, QVBoxLayout, QWidget

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
    from services.metadata_preview import MetadataPreviewController
    from services.safe_rename import SafeRenameController

from services.metadata_preview import MetadataPreviewInput
from services.safe_rename import SafeRenameInput


class MainWindow(QMainWindow):
    def __init__(
        self,
        scan_controller: LibraryScanController | None = None,
        metadata_preview_controller: MetadataPreviewController | None = None,
        safe_rename_controller: SafeRenameController | None = None,
        *,
        use_model_view: bool = False,
    ) -> None:
        super().__init__()
        self._scan_controller = scan_controller
        self._metadata_preview_controller = metadata_preview_controller
        self._safe_rename_controller = safe_rename_controller
        self._use_model_view = bool(use_model_view)
        self._scan_dialog: ReadOnlyScanDialog | None = None
        self._rename_dialog: RenamePreviewDialog | None = None
        self._close_pending = False
        self._metadata_inputs_by_asset: dict[str, MetadataPreviewInput] = {}
        self._metadata_results_by_asset: dict[str, object] = {}
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

        self.sidebar = Sidebar()
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
        self._add_page("所有歌词", LibraryPage("所有歌词", LYRICS, kind="lyrics", display_count=214))
        display_counts = {"我喜欢的": 62, "粤语": 36, "通勤": 28, "怀旧": 41, "古巨基": 17}
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

    def _add_page(self, key: str, page: LibraryPage) -> None:
        self.pages[key] = page
        self.stack.addWidget(page)
        page.delete_requested.connect(lambda records, p=page: self._confirm_delete(p, records))
        page.new_playlist_requested.connect(self.create_playlist)

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
        if self._scan_controller is None:
            self._show_window(ImportDialog(self))
            return
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

    def _start_read_only_scan(self, root) -> None:
        if self._scan_controller is None:
            return
        if self._has_running_rename_task():
            if self._scan_dialog is not None:
                self._scan_dialog.show_failed("歌曲信息分析或重命名正在运行，不能同时开始扫描")
            return
        try:
            self._scan_controller.start_scan(root)
        except Exception as error:
            if self._scan_dialog is not None:
                self._scan_dialog.show_failed(str(error))

    def _replace_music_library(self, records) -> None:
        self.pages["所有音乐"].replace_data(records)

    def _background_running_changed(self, _running: bool) -> None:
        if self._close_pending and not self._has_running_background_task():
            QTimer.singleShot(0, self.close)

    def open_rename(self) -> None:
        if self._metadata_preview_controller is None or self._scan_controller is None:
            self._show_window(RenamePreviewDialog(self))
            return
        if self._rename_dialog is not None:
            self._rename_dialog.show()
            self._rename_dialog.raise_()
            self._rename_dialog.activateWindow()
            return

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

        page = self.pages["所有音乐"]
        if self.stack.currentWidget() is not page:
            dialog.show_warning("请先切换到“所有音乐”并明确选择要分析的音乐。")
            return
        if self._scan_controller.running:
            dialog.show_warning("只读扫描正在运行，请完成或取消后再分析歌曲信息。")
            return
        if self._safe_rename_controller is not None and self._safe_rename_controller.running:
            dialog.show_warning("安全重命名正在运行，请等待当前任务结束。")
            return
        records = page.selected_records()
        if not records:
            dialog.show_warning("请先勾选或选中至少一首音乐，再开始只读分析。")
            return
        try:
            items = tuple(self._metadata_input_from_record(record) for record in records)
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
        )

    def _start_safe_rename(self, requests: object) -> None:
        if self._safe_rename_controller is None or self._rename_dialog is None:
            return
        if self._scan_controller is not None and self._scan_controller.running:
            self._rename_dialog.show_warning("只读扫描正在运行，不能同时重命名。")
            return
        if self._metadata_preview_controller is not None and self._metadata_preview_controller.running:
            self._rename_dialog.show_warning("歌曲信息分析尚未结束，不能开始重命名。")
            return
        if self._safe_rename_controller.running:
            self._rename_dialog.show_warning("已有安全重命名任务正在运行。")
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

    def open_lyrics_match(self) -> None:
        self._show_window(LyricsMatchDialog(self))

    def open_history(self) -> None:
        self._show_window(HistoryDialog(self))

    def open_settings(self) -> None:
        self._show_window(SettingsDialog(self))

    def create_playlist(self) -> None:
        dialog = CreatePlaylistDialog(self)
        if dialog.exec() != dialog.DialogCode.Accepted:
            return
        name = dialog.name_input.text().strip()
        if not name:
            return
        self.sidebar.add_playlist(name)
        page = LibraryPage(name, [], display_count=0, playlist_name=name)
        self._add_page(f"playlist:{name}", page)
        self.navigate(f"playlist:{name}")

    def _confirm_delete(self, page: LibraryPage, records: list[dict]) -> None:
        if not records:
            return
        self._create_delete_dialog(page, records).exec()

    def _create_delete_dialog(self, page: LibraryPage, records: list[dict]) -> QDialog:
        if page.playlist_name:
            return RemovePlaylistItemsDialog(len(records), self)
        if page.kind == "music":
            return DeleteConfirmDialog(records, self)
        if page.kind == "lyrics":
            return DeleteLyricsConfirmDialog(records, self)
        raise ValueError(f"不支持的页面类型：{page.kind}")

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._has_running_background_task():
            self._close_pending = True
            if self._scan_controller is not None and self._scan_controller.running:
                self._scan_controller.request_cancel()
            if self._metadata_preview_controller is not None and self._metadata_preview_controller.running:
                self._metadata_preview_controller.request_cancel()
            if self._safe_rename_controller is not None and self._safe_rename_controller.running:
                self._safe_rename_controller.request_cancel()
            event.ignore()
            return
        super().closeEvent(event)

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
        )
