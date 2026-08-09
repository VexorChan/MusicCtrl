"""Launch the production UI with all persistent state confined to project work/.

This entry point exists only for human-style UI acceptance testing.  It never
selects or scans a media directory automatically; the tester must still choose
every source and target through the normal product dialogs.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
import sys

from PySide6.QtCore import QTimer

from database import DatabaseConfig
from main import build_app
from repositories import LibraryRepository
from services.backup_manager import BackupController
from services.library_scan_controller import LibraryScanController
from services.lyrics_match_controller import LyricsMatchController
from services.metadata_preview import MetadataPreviewController
from services.playlist_controller import PlaylistController
from services.safe_import import SafeImportController
from services.safe_rename import SafeRenameController
from ui.main_window import MainWindow


ROOT = Path(__file__).resolve().parent


def _create_run_root() -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    root = ROOT / "work" / f"manual-ui-{stamp}"
    root.mkdir(parents=True, exist_ok=False)
    return root


def main() -> int:
    app = build_app()
    run_root = _create_run_root()
    database_config = DatabaseConfig(run_root / "library.sqlite3")
    repository_factory = lambda: LibraryRepository(database_config)

    scan_controller = LibraryScanController(database_config)
    lyrics_controller = LyricsMatchController(database_config)
    playlist_controller = PlaylistController(database_config)
    safe_import_controller = SafeImportController(repository_factory)
    backup_controller = BackupController(
        backup_root=run_root / "backups",
        repository_factory=repository_factory,
    )
    window = MainWindow(
        scan_controller,
        MetadataPreviewController(),
        SafeRenameController(repository_factory),
        lyrics_controller,
        playlist_controller,
        safe_import_controller,
        backup_controller,
        use_model_view=True,
    )
    window.setWindowTitle("乐库整理助手（真人测试）")
    window.setProperty("manualTestRoot", str(run_root))
    window.show()
    QTimer.singleShot(0, window.start_pending_safe_import_recovery)
    QTimer.singleShot(0, window.start_pending_playlist_retarget_recovery)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
