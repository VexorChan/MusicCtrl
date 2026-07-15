from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import QStandardPaths, Qt
from PySide6.QtGui import QFont, QIcon
from PySide6.QtWidgets import QApplication

from database import DatabaseConfig
from repositories import LibraryRepository
from services.library_scan_controller import LibraryScanController
from services.lyrics_match_controller import LyricsMatchController
from services.metadata_preview import MetadataPreviewController
from services.safe_rename import SafeRenameController
from services.playlist_controller import PlaylistController
from services.safe_import import SafeImportController
from services.backup_manager import BackupController
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


def build_production_database_config() -> DatabaseConfig:
    location = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppLocalDataLocation)
    if not location:
        raise RuntimeError("系统未提供应用数据目录")
    directory = Path(location)
    if not directory.is_absolute():
        raise RuntimeError("应用数据目录必须是绝对路径")
    directory = Path(str(directory))
    if directory == ROOT or directory == Path.cwd():
        raise RuntimeError("数据库不能放在项目根或当前工作目录")
    directory.mkdir(parents=True, exist_ok=True)
    return DatabaseConfig(directory / "library.sqlite3")


def main() -> int:
    app = build_app()
    database_config = build_production_database_config()
    controller = LibraryScanController(database_config)
    metadata_preview_controller = MetadataPreviewController()
    safe_rename_controller = SafeRenameController(
        lambda: LibraryRepository(database_config)
    )
    lyrics_match_controller = LyricsMatchController(database_config)
    playlist_controller = PlaylistController(database_config)
    safe_import_controller = SafeImportController()
    backup_root = database_config.path.parent / "backups"
    backup_controller = BackupController(
        backup_root=backup_root,
        repository_factory=lambda: LibraryRepository(database_config),
    )
    window = MainWindow(
        controller,
        metadata_preview_controller,
        safe_rename_controller,
        lyrics_match_controller,
        playlist_controller,
        safe_import_controller,
        backup_controller,
        use_model_view=True,
    )
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
