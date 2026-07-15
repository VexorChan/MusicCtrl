from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import QTimer, Qt
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import QApplication, QDialog, QHBoxLayout, QMainWindow, QStackedWidget, QVBoxLayout, QWidget

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


class MainWindow(QMainWindow):
    def __init__(self, scan_controller: LibraryScanController | None = None) -> None:
        super().__init__()
        self._scan_controller = scan_controller
        self._scan_dialog: ReadOnlyScanDialog | None = None
        self._close_pending = False
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
        self._add_page("所有音乐", LibraryPage("所有音乐", music_data, display_count=music_count))
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
            scan_controller.running_changed.connect(self._scan_running_changed)
            try:
                self._replace_music_library(scan_controller.load_library())
            except Exception as error:
                scan_controller.warning.emit(f"无法加载音乐索引：{error}")

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
        remembered = self._scan_controller.remembered_root()
        if remembered is not None:
            dialog.path_input.setText(str(remembered))
        dialog.set_running(self._scan_controller.running)
        self._show_window(dialog)

    def _start_read_only_scan(self, root) -> None:
        if self._scan_controller is None:
            return
        try:
            self._scan_controller.start_scan(root)
        except Exception as error:
            if self._scan_dialog is not None:
                self._scan_dialog.show_failed(str(error))

    def _replace_music_library(self, records) -> None:
        self.pages["所有音乐"].replace_data(records)

    def _scan_running_changed(self, running: bool) -> None:
        if not running and self._close_pending:
            QTimer.singleShot(0, self.close)

    def open_rename(self) -> None:
        self._show_window(RenamePreviewDialog(self))

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
        if self._scan_controller is not None and self._scan_controller.running:
            self._close_pending = True
            self._scan_controller.request_cancel()
            event.ignore()
            return
        super().closeEvent(event)
