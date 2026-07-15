from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont, QIcon
from PySide6.QtWidgets import QApplication

from ui.main_window import MainWindow


ROOT = Path(__file__).resolve().parent
APP_ICON_PATHS = (ROOT / "assets" / "app_icon.ico", ROOT / "assets" / "app_icon.png")


def load_app_icon() -> QIcon:
    for path in APP_ICON_PATHS:
        if not path.is_file():
            continue
        icon = QIcon(str(path))
        if not icon.isNull():
            return icon
    return QIcon()


def build_app() -> QApplication:
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("乐库整理助手")
    app.setOrganizationName("LocalMusicTools")
    app.setStyle("Fusion")
    app.setFont(QFont("Microsoft YaHei UI", 9))
    app.setWindowIcon(load_app_icon())
    app.setAttribute(Qt.ApplicationAttribute.AA_DontShowIconsInMenus, False)
    app.setStyleSheet((ROOT / "styles" / "theme.qss").read_text(encoding="utf-8"))
    return app


def main() -> int:
    app = build_app()
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
