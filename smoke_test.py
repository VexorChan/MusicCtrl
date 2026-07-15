from __future__ import annotations

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QFont, QFontMetrics
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QCheckBox, QLabel, QLineEdit

from dialogs.delete_confirm_dialog import DeleteConfirmDialog, DeleteLyricsConfirmDialog, RemovePlaylistItemsDialog
from dialogs.history_dialog import HistoryDialog
from dialogs.import_dialog import ImportDialog
from dialogs.lyrics_match_dialog import LyricsMatchDialog
from dialogs.rename_preview_dialog import RenamePreviewDialog
from dialogs.settings_dialog import SettingsDialog
from main import build_app
from ui.music_page import LibraryPage
from ui.main_window import MainWindow
from ui.sidebar import Sidebar
from ui.tables import CheckableHeaderView
from ui.toolbars import GlobalToolbar


def run() -> None:
    def assert_status_badges(table, column: int) -> None:
        assert table.rowCount() > 0
        for row in range(table.rowCount()):
            item = table.item(row, column)
            assert item is not None
            assert item.text() == ""
            status = item.data(Qt.ItemDataRole.UserRole)
            assert isinstance(status, str) and status
            assert item.toolTip() == status
            badge_host = table.cellWidget(row, column)
            assert badge_host is not None
            badge = badge_host.findChild(QLabel, "StatusBadge")
            assert badge is not None
            assert badge.text() == status

    def assert_no_private_paths(widget) -> None:
        private_user_fragment = "15" + "180"
        visible_texts = [label.text() for label in widget.findChildren(QLabel)]
        visible_texts.extend(line.text() for line in widget.findChildren(QLineEdit))
        for text in visible_texts:
            assert private_user_fragment not in text
            assert "C:\\Users\\" not in text

    app = build_app()
    assert not app.windowIcon().isNull()
    window = MainWindow()
    assert not window.windowIcon().isNull()
    brand_pixmap = window.sidebar.brand_icon.pixmap()
    assert brand_pixmap is not None and not brand_pixmap.isNull()
    assert window.minimumWidth() == 960
    assert window.minimumHeight() == 600
    assert set(["所有音乐", "所有歌词", "playlist:粤语"]).issubset(window.pages)
    assert window.toolbar.button_order == ["导入", "重命名", "匹配歌词", "操作历史", "设置"]
    toolbar_buttons = list(window.toolbar.buttons_by_text.values())
    assert all(button.objectName() == "ToolbarButton" for button in toolbar_buttons)
    assert all(button.icon().isNull() for button in toolbar_buttons)
    assert all(button.accessibleName() == button.text() for button in toolbar_buttons)
    assert all(button.focusPolicy() != Qt.FocusPolicy.NoFocus for button in toolbar_buttons)
    assert "ToolbarPrimary" not in app.styleSheet()
    assert "QPushButton#ToolbarButton:focus" in app.styleSheet()
    assert "QPushButton#ToolbarButton:hover" in app.styleSheet()
    assert "font-size: 14px" in app.styleSheet()
    assert "QPushButton#ToolbarButton:pressed" in app.styleSheet()

    standalone_toolbar = GlobalToolbar()
    standalone_toolbar.resize(700, 58)
    standalone_toolbar.show()
    standalone_toolbar.activateWindow()
    app.processEvents()
    for button in standalone_toolbar.buttons_by_text.values():
        initial_geometry = button.geometry()
        hover_font = QFont(button.font())
        hover_font.setPixelSize(14)
        text_width = QFontMetrics(hover_font).horizontalAdvance(button.text())
        assert text_width + 16 <= button.width()

        QTest.mouseMove(button, button.rect().center())
        app.processEvents()
        assert button.geometry() == initial_geometry
        hover_color = button.grab().toImage().pixelColor(button.width() - 8, button.height() // 2)

        QTest.mousePress(
            button,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
            button.rect().center(),
        )
        app.processEvents()
        assert button.geometry() == initial_geometry
        pressed_color = button.grab().toImage().pixelColor(button.width() - 8, button.height() // 2)
        assert pressed_color.red() < hover_color.red()

        QTest.mouseRelease(
            button,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
            button.rect().center(),
        )
        QTest.mouseMove(standalone_toolbar, QPoint(standalone_toolbar.width() - 2, standalone_toolbar.height() - 2))
        app.processEvents()
        assert button.geometry() == initial_geometry
        button.setFocus()
        app.processEvents()
        assert button.hasFocus()
    standalone_toolbar.close()

    for key, page in window.pages.items():
        window.navigate(key)
        assert window.stack.currentWidget() is page
        assert window.sidebar._buttons[key].isChecked()
    window.navigate("所有音乐")

    old_nav_characters = ("♫", "≡", "▤")
    for button in window.sidebar._buttons.values():
        assert not any(character in button.text() for character in old_nav_characters)
        assert button.property("navAlignment") == "center"
    assert "QPushButton#NavButton { text-align: center;" in app.styleSheet()

    window.sidebar.search.setText("粤")
    assert not window.sidebar._playlist_buttons["粤语"].isHidden()
    assert window.sidebar._playlist_buttons["通勤"].isHidden()
    window.sidebar.search.clear()
    assert all(not button.isHidden() for button in window.sidebar._playlist_buttons.values())

    standalone_sidebar = Sidebar()
    create_requests: list[bool] = []
    standalone_sidebar.create_playlist_requested.connect(lambda: create_requests.append(True))
    standalone_sidebar.add_playlist_button.click()
    assert create_requests == [True]
    standalone_sidebar.add_playlist("动态歌单")
    dynamic_button = standalone_sidebar._playlist_buttons["动态歌单"]
    assert dynamic_button.text() == "动态歌单"
    assert dynamic_button.property("navAlignment") == "center"
    assert not any(character in dynamic_button.text() for character in old_nav_characters)

    music = window.pages["所有音乐"]
    music.apply_search_immediately("  dear leslie  ")
    assert music.table.rowCount() == 1
    assert music.table.item(0, 1).text() == "Dear Leslie"
    music.apply_search_immediately("不存在的音乐")
    assert music.table.rowCount() == 0
    assert music.content_stack.currentIndex() == 1
    music.apply_search_immediately("")
    assert music.table.columnCount() == 7
    assert [music.table.horizontalHeaderItem(column).text() for column in range(7)] == [
        "", "歌名", "歌手", "时长", "格式", "大小", "歌词状态"
    ]
    checkable_header = music.table.require_checkable_header()
    assert isinstance(checkable_header, CheckableHeaderView)
    assert checkable_header.check_state() == Qt.CheckState.Unchecked
    assert checkable_header.checkbox.checkState() == Qt.CheckState.Unchecked
    assert not music.table.wordWrap()
    assert music.table.columnWidth(6) >= 132
    assert music.table.item(0, 6).text() == ""
    assert music.table.item(0, 6).toolTip() == "未检查"
    assert not music.add_button.isEnabled()
    assert not music.delete_button.isEnabled()

    music.table.item(0, 0).setCheckState(Qt.CheckState.Checked)
    assert checkable_header.check_state() == Qt.CheckState.PartiallyChecked
    music.apply_search_immediately("不存在的音乐")
    assert music.table.rowCount() == 0
    assert checkable_header.check_state() == Qt.CheckState.Unchecked
    assert checkable_header.checkbox.checkState() == Qt.CheckState.Unchecked
    checkable_header.checkbox.click()
    app.processEvents()
    assert checkable_header.check_state() == Qt.CheckState.Unchecked
    assert checkable_header.checkbox.checkState() == Qt.CheckState.Unchecked
    music.apply_search_immediately("")
    assert music.table.rowCount() > 0
    assert checkable_header.check_state() == Qt.CheckState.Unchecked
    assert checkable_header.checkbox.checkState() == Qt.CheckState.Unchecked

    music.table.selectRow(0)
    assert music.add_button.isEnabled()
    assert music.delete_button.isEnabled()
    assert checkable_header.check_state() == Qt.CheckState.Unchecked
    music.table.clearSelection()
    assert not music.add_button.isEnabled()
    assert not music.delete_button.isEnabled()

    music.table.item(0, 0).setCheckState(Qt.CheckState.Checked)
    assert checkable_header.check_state() == Qt.CheckState.PartiallyChecked
    for row in range(1, music.table.rowCount()):
        music.table.item(row, 0).setCheckState(Qt.CheckState.Checked)
    assert checkable_header.check_state() == Qt.CheckState.Checked
    window.show()
    app.processEvents()
    header_click = QPoint(
        checkable_header.sectionViewportPosition(0) + checkable_header.sectionSize(0) // 2,
        checkable_header.height() // 2,
    )
    QTest.mouseClick(
        checkable_header.viewport(),
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
        header_click,
    )
    assert checkable_header.check_state() == Qt.CheckState.Unchecked
    assert all(
        music.table.item(row, 0).checkState() == Qt.CheckState.Unchecked
        for row in range(music.table.rowCount())
    )
    QTest.mouseClick(
        checkable_header.viewport(),
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
        header_click,
    )
    assert checkable_header.check_state() == Qt.CheckState.Checked
    assert all(
        music.table.item(row, 0).checkState() == Qt.CheckState.Checked
        for row in range(music.table.rowCount())
    )
    QTest.mouseClick(
        checkable_header.viewport(),
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
        header_click,
    )
    assert checkable_header.check_state() == Qt.CheckState.Unchecked

    priority_page = LibraryPage(
        "搜索测试",
        [
            {"title": "周末", "artist": "许巍", "duration": "03:00", "format": "MP3", "size": "8 MB", "status": "未检查"},
            {"title": "晴天", "artist": "周杰伦", "duration": "04:29", "format": "MP3", "size": "9 MB", "status": "已匹配"},
            {"title": "晴天以后", "artist": "张三", "duration": "04:00", "format": "MP3", "size": "9 MB", "status": "未匹配"},
            {"title": "前奏晴天", "artist": "李四", "duration": "05:00", "format": "MP3", "size": "9 MB", "status": "未匹配"},
        ],
    )
    priority_page.apply_search_immediately("周")
    assert [item["title"] for item in priority_page.visible_data] == ["周末"]
    priority_page.apply_search_immediately("杰伦")
    assert [item["title"] for item in priority_page.visible_data] == ["晴天"]
    priority_page.apply_search_immediately("晴天")
    assert [item["title"] for item in priority_page.visible_data] == ["晴天", "晴天以后", "前奏晴天"]
    priority_page.apply_search_immediately("")
    priority_page._header_clicked(5)
    assert [item["size"] for item in priority_page.visible_data] == ["8 MB", "9 MB", "9 MB", "9 MB"]
    assert [item["_index"] for item in priority_page.visible_data[1:]] == [1, 2, 3]
    assert priority_page.table.horizontalHeader().sortIndicatorSection() == 5
    assert priority_page.table.horizontalHeader().sortIndicatorOrder() == Qt.SortOrder.AscendingOrder
    priority_page._header_clicked(5)
    assert [item["_index"] for item in priority_page.visible_data[:3]] == [1, 2, 3]
    assert priority_page.table.horizontalHeader().sortIndicatorOrder() == Qt.SortOrder.DescendingOrder
    checked_sort_actions = [action.text() for action in priority_page.create_sort_menu().actions() if action.isChecked()]
    assert checked_sort_actions == ["按大小降序"]

    music._header_clicked(0)
    assert all(music.table.item(row, 0).checkState() == Qt.CheckState.Checked for row in range(music.table.rowCount()))
    assert checkable_header.check_state() == Qt.CheckState.Checked
    assert music.add_button.isEnabled()
    assert music.delete_button.isEnabled()
    music._header_clicked(1)
    assert checkable_header.check_state() == Qt.CheckState.Unchecked
    assert all(
        music.table.item(row, 0).checkState() == Qt.CheckState.Unchecked
        for row in range(music.table.rowCount())
    )
    music._header_clicked(0)
    assert checkable_header.check_state() == Qt.CheckState.Checked
    music._header_clicked(0)
    assert not music.add_button.isEnabled()

    sort_menu = music.create_sort_menu()
    assert [action.text() for action in sort_menu.actions()] == [
        "按歌名升序", "按歌名降序", "按歌手升序", "按歌手降序",
        "按时长升序", "按时长降序", "按大小升序", "按大小降序",
    ]
    playlist_menu = music.create_playlist_menu()
    playlist_menu.search.setText("粤")
    assert playlist_menu.playlist_actions["粤语"].isVisible()
    assert not playlist_menu.playlist_actions["通勤"].isVisible()
    playlist_menu.playlist_actions["粤语"].setChecked(True)
    assert playlist_menu.confirm_button.isEnabled()
    playlist_menu_texts = [action.text() for action in playlist_menu.actions()]
    assert any("已存在，将自动跳过" in text for text in playlist_menu_texts)
    assert any("不会复制音乐文件" in text for text in playlist_menu_texts)

    context_menu = music.create_context_menu()
    assert [action.text() for action in context_menu.actions()] == [
        "打开所在文件夹", "重命名", "重新匹配歌词"
    ]
    before_double_click = list(music.visible_data)
    delete_requests: list[list[dict]] = []
    music.delete_requested.connect(delete_requests.append)
    music.table.doubleClicked.emit(music.table.model().index(0, 1))
    app.processEvents()
    assert music.visible_data == before_double_click
    assert delete_requests == []

    lyrics = window.pages["所有歌词"]
    assert lyrics.table.columnCount() == 6
    assert [lyrics.table.horizontalHeaderItem(column).text() for column in range(6)] == [
        "", "歌名", "歌手", "格式", "大小", "歌词状态"
    ]
    assert any(item["status"] == "已有内嵌歌词" for item in window.pages["所有音乐"].all_data)
    cantonese = window.pages["playlist:粤语"]
    assert cantonese.playlist_note is not None
    assert cantonese.playlist_note.text() == "从歌单移除只会删除快捷方式，不会删除音乐文件。"
    assert [cantonese.table.horizontalHeaderItem(column).text() for column in range(7)] == [
        "", "歌名", "歌手", "时长", "格式", "大小", "歌词状态"
    ]

    music_delete_dialog = window._create_delete_dialog(music, [music.all_data[0]])
    lyrics_delete_dialog = window._create_delete_dialog(lyrics, [lyrics.all_data[0]])
    playlist_remove_dialog = window._create_delete_dialog(cantonese, [cantonese.all_data[0]])
    assert type(music_delete_dialog) is DeleteConfirmDialog
    assert type(lyrics_delete_dialog) is DeleteLyricsConfirmDialog
    assert type(playlist_remove_dialog) is RemovePlaylistItemsDialog

    music_delete_text = " ".join(label.text() for label in music_delete_dialog.findChildren(QLabel))
    lyrics_delete_text = " ".join(label.text() for label in lyrics_delete_dialog.findChildren(QLabel))
    playlist_remove_text = " ".join(label.text() for label in playlist_remove_dialog.findChildren(QLabel))
    assert "备份" in music_delete_text
    assert music_delete_dialog.delete_lyrics.text() == "同时删除已匹配的歌词"
    assert not music_delete_dialog.delete_lyrics.isChecked()
    assert "引用" in lyrics_delete_text
    assert "备份" in lyrics_delete_text
    assert "本 M1 原型不会读取、移动或删除任何真实文件" in lyrics_delete_text
    assert "快捷方式" in playlist_remove_text
    assert "不会删除音乐文件" in playlist_remove_text
    music_delete_dialog.close()
    lyrics_delete_dialog.close()
    playlist_remove_dialog.close()

    window.show()
    for width, height in ((1200, 760), (960, 600)):
        window.resize(width, height)
        app.processEvents()
        assert window.size().toTuple() == (width, height)
        assert window.sidebar.width() == 216
        assert music.table.columnWidth(1) >= 135
        assert music.table.columnWidth(2) >= 135
        for button in window.toolbar.buttons_by_text.values():
            assert button.isVisible()
            assert button.geometry().right() <= window.toolbar.width()
    assert music.sort_button.width() == 88
    assert music.add_button.width() == 104

    opened_window_types = {
        "导入": ImportDialog,
        "重命名": RenamePreviewDialog,
        "匹配歌词": LyricsMatchDialog,
        "操作历史": HistoryDialog,
        "设置": SettingsDialog,
    }
    for text, expected_type in opened_window_types.items():
        button = window.toolbar.buttons_by_text[text]
        QTest.mouseClick(button, Qt.MouseButton.LeftButton)
        app.processEvents()
        assert window._open_windows
        opened_window = window._open_windows[-1]
        assert type(opened_window) is expected_type
        assert opened_window.isVisible()
        opened_window.close()
        window._open_windows.remove(opened_window)
        opened_window.deleteLater()
        app.processEvents()

    import_dialog = ImportDialog(window)
    rename_dialog = RenamePreviewDialog(window)
    history_dialog = HistoryDialog(window)
    settings_dialog = SettingsDialog(window)
    assert import_dialog.scan_path.text() == r"C:\MusicCtrlDemo\Downloads"
    assert import_dialog.target_path.text() == r"C:\MusicCtrlDemo\Music\所有音乐"
    assert_status_badges(import_dialog.table, 2)
    import_dialog.set_mode("lyrics")
    assert import_dialog.scan_path.text() == r"C:\MusicCtrlDemo\Downloads"
    assert import_dialog.target_path.text() == r"C:\MusicCtrlDemo\Music\歌词"
    assert_status_badges(import_dialog.table, 2)
    import_dialog.set_mode("audio")
    assert import_dialog.target_path.text() == r"C:\MusicCtrlDemo\Music\所有音乐"
    assert_status_badges(rename_dialog.table, 4)
    assert_status_badges(history_dialog.table, 4)
    for dialog in (import_dialog, history_dialog, settings_dialog):
        assert_no_private_paths(dialog)
    settings_paths = [line.text() for line in settings_dialog.findChildren(QLineEdit)]
    assert r"C:\MusicCtrlDemo\Downloads" in settings_paths
    assert r"C:\MusicCtrlDemo\Music" in settings_paths
    for dialog, expected_header in (
        (import_dialog, "No."),
        (rename_dialog, ""),
        (history_dialog, "时间"),
    ):
        assert not isinstance(dialog.table.horizontalHeader(), CheckableHeaderView)
        assert dialog.table.checkable_header() is None
        assert dialog.table.horizontalHeaderItem(0).text() == expected_header
        assert dialog.table.findChildren(QCheckBox, "HeaderCheckBox") == []

    dialogs = [
        import_dialog,
        rename_dialog,
        LyricsMatchDialog(window),
        history_dialog,
        settings_dialog,
        DeleteConfirmDialog(parent=window),
    ]
    for dialog in dialogs:
        dialog.show()
        app.processEvents()
        assert dialog.isVisible()
        dialog.close()
    window.close()
    print("SMOKE TEST PASSED")


if __name__ == "__main__":
    run()
