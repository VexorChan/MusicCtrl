from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtWidgets import QApplication, QWidget

from dialogs.delete_confirm_dialog import DeleteConfirmDialog
from dialogs.history_dialog import HistoryDialog
from dialogs.import_dialog import ImportDialog
from dialogs.lyrics_match_dialog import LyricsMatchDialog
from dialogs.rename_preview_dialog import RenamePreviewDialog
from dialogs.settings_dialog import SettingsDialog
from main import build_app
from ui.main_window import MainWindow


ROOT = Path(__file__).resolve().parent
SHOT_DIR = ROOT / "screenshots"


def settle(app: QApplication, milliseconds: int = 180) -> None:
    loop = QEventLoop()
    QTimer.singleShot(milliseconds, loop.quit)
    loop.exec()
    app.processEvents()


def save_widget(app: QApplication, widget: QWidget, filename: str) -> None:
    widget.show()
    widget.raise_()
    widget.activateWindow()
    settle(app)
    widget.repaint()
    settle(app, 80)
    pixmap = widget.grab()
    if pixmap.isNull():
        raise RuntimeError(f"无法捕获窗口：{filename}")
    target = SHOT_DIR / filename
    if not pixmap.save(str(target), "PNG"):
        raise RuntimeError(f"无法保存截图：{target}")
    print(target)


def assert_main_page(main: MainWindow, key: str) -> None:
    page = main.pages[key]
    if main.stack.currentWidget() is not page:
        raise RuntimeError(f"截图页面未切换到：{key}")
    button = main.sidebar._buttons[key]
    if not button.isChecked():
        raise RuntimeError(f"截图导航未选中：{key}")


def run() -> None:
    SHOT_DIR.mkdir(parents=True, exist_ok=True)
    app = build_app()
    app.setQuitOnLastWindowClosed(False)
    main = MainWindow()
    main.resize(1200, 760)
    main.move(80, 50)
    assert_main_page(main, "所有音乐")
    save_widget(app, main, "01_all_music.png")

    main.navigate("所有歌词")
    settle(app)
    assert_main_page(main, "所有歌词")
    save_widget(app, main, "02_all_lyrics.png")

    main.navigate("playlist:粤语")
    settle(app)
    assert_main_page(main, "playlist:粤语")
    cantonese_page = main.pages["playlist:粤语"]
    if cantonese_page.playlist_name != "粤语" or cantonese_page.count_label.text() != "共 36 首":
        raise RuntimeError("粤语歌单截图状态不正确")
    if cantonese_page.playlist_note is None or "不会删除音乐文件" not in cantonese_page.playlist_note.text():
        raise RuntimeError("粤语歌单安全提示缺失")
    save_widget(app, main, "03_playlist_cantonese.png")
    if (SHOT_DIR / "01_all_music.png").read_bytes() == (SHOT_DIR / "03_playlist_cantonese.png").read_bytes():
        raise RuntimeError("歌单截图与所有音乐截图完全相同")

    dialogs: list[QWidget] = [
        ImportDialog(main),
        RenamePreviewDialog(main),
        LyricsMatchDialog(main),
        HistoryDialog(main),
        SettingsDialog(main),
        DeleteConfirmDialog(parent=main),
    ]
    names = [
        "04_import.png",
        "05_rename_preview.png",
        "06_lyrics_match.png",
        "07_history.png",
        "08_settings.png",
        "09_delete_confirm.png",
    ]
    for dialog, name in zip(dialogs, names):
        save_widget(app, dialog, name)
        dialog.hide()
        settle(app, 80)

    main.navigate("所有音乐")
    main.pages["所有音乐"].apply_search_immediately("没有这首歌")
    settle(app)
    assert_main_page(main, "所有音乐")
    save_widget(app, main, "10_search_empty.png")
    main.close()


if __name__ == "__main__":
    run()
