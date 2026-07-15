from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QDialog, QHBoxLayout, QMainWindow, QStackedWidget, QVBoxLayout, QWidget

from dialogs.delete_confirm_dialog import DeleteConfirmDialog, DeleteLyricsConfirmDialog, RemovePlaylistItemsDialog
from dialogs.history_dialog import HistoryDialog
from dialogs.import_dialog import ImportDialog
from dialogs.lyrics_match_dialog import LyricsMatchDialog
from dialogs.playlist_dialog import CreatePlaylistDialog
from dialogs.rename_preview_dialog import RenamePreviewDialog
from dialogs.settings_dialog import SettingsDialog
from mock.data import LYRICS, PLAYLIST_MAP, SONGS
from ui.music_page import LibraryPage
from ui.sidebar import Sidebar
from ui.toolbars import GlobalToolbar


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
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

        self._add_page("所有音乐", LibraryPage("所有音乐", SONGS, display_count=268))
        self._add_page("所有歌词", LibraryPage("所有歌词", LYRICS, kind="lyrics", display_count=214))
        display_counts = {"我喜欢的": 62, "粤语": 36, "通勤": 28, "怀旧": 41, "古巨基": 17}
        for name, indices in PLAYLIST_MAP.items():
            records = [SONGS[index] for index in indices]
            self._add_page(
                f"playlist:{name}",
                LibraryPage(name, records, display_count=display_counts.get(name, len(records)), playlist_name=name),
            )
        self.navigate("所有音乐")

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
        self._show_window(ImportDialog(self))

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
