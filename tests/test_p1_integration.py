from __future__ import annotations

from pathlib import Path
import threading
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from PySide6.QtCore import QCoreApplication, QEvent, QEventLoop, QStandardPaths, QThread, QTimer, Qt
from PySide6.QtWidgets import QApplication

from database import DatabaseConfig, open_database
from dialogs.import_dialog import ImportDialog
from dialogs.read_only_scan_dialog import ReadOnlyScanDialog
from main import build_app, build_production_database_config
from mock.data import SONGS
from repositories import IndexBatchItem, LibraryRepository
from services.library_scan_controller import LAST_SUCCESSFUL_ROOT_KEY, LibraryScanController
from services.scan_worker import ReadOnlyScanWorker
from services.safe_import import SafeImportController
from ui.main_window import MainWindow
from ui.music_page import LibraryPage


class P1IntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = build_app()

    def setUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.database_path = self.root / "app-data" / "library.sqlite3"
        self.database_path.parent.mkdir()
        self.config = DatabaseConfig(self.database_path, timeout_seconds=1.0, busy_timeout_ms=1000)

    def flush_deletes(self) -> None:
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        QApplication.processEvents()

    def wait_until(self, predicate, *, timeout_ms: int = 5000, poll=None) -> None:
        if predicate():
            return
        loop = QEventLoop()
        timer = QTimer()
        timer.setInterval(0)

        def check() -> None:
            if poll is not None:
                poll()
            if predicate():
                loop.quit()

        timer.timeout.connect(check)
        timeout = QTimer()
        timeout.setSingleShot(True)
        expired = []
        timeout.timeout.connect(lambda: (expired.append(True), loop.quit()))
        timer.start()
        timeout.start(timeout_ms)
        loop.exec()
        timer.stop()
        timeout.stop()
        self.assertFalse(expired, "condition did not become true before timeout")

    def seed_existing_library(self, successful_root: Path) -> None:
        source = self.root / "existing" / "remembered.mp3"
        with LibraryRepository(self.config) as repository:
            session = repository.create_scan_session(mode="audio", source_folder=source.parent)
            repository.index_scan_batch(session.id, (IndexBatchItem(source, 7, 11),))
            repository.finish_scan_session(session.id, status="completed")
            repository.set_setting(LAST_SUCCESSFUL_ROOT_KEY, str(successful_root))

    def test_opening_scan_dialog_prefills_root_without_starting_scan(self) -> None:
        remembered = self.root / "remembered"
        remembered.mkdir()
        with LibraryRepository(self.config) as repository:
            repository.set_setting(LAST_SUCCESSFUL_ROOT_KEY, str(remembered))
        controller = LibraryScanController(self.config)
        window = MainWindow(controller)
        self.addCleanup(window.close)

        window.open_import()
        QApplication.processEvents()

        self.assertIsInstance(window._scan_dialog, ReadOnlyScanDialog)
        self.assertEqual(window._scan_dialog.path_input.text(), str(remembered))
        self.assertFalse(controller.running)
        connection = open_database(self.config)
        try:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM scan_sessions").fetchone()[0], 0)
        finally:
            connection.close()

    def test_broken_database_still_opens_scan_dialog_and_reports_visible_error(self) -> None:
        database_bytes = b"not a sqlite database"
        self.database_path.write_bytes(database_bytes)
        controller = LibraryScanController(self.config)
        window = MainWindow(controller)
        self.addCleanup(window.close)

        window.open_import()
        QApplication.processEvents()

        self.assertIsInstance(window._scan_dialog, ReadOnlyScanDialog)
        assert window._scan_dialog is not None
        self.assertEqual(window._scan_dialog.path_input.text(), "")
        self.assertIn("无法读取上次扫描目录", window._scan_dialog.summary.text())
        self.assertEqual(self.database_path.read_bytes(), database_bytes)
        self.assertFalse(controller.running)

    def test_explicit_scan_indexes_six_formats_updates_real_page_and_preserves_media(self) -> None:
        scan_root = self.root / "media"
        scan_root.mkdir()
        names = ("a.MP3", "b.flac", "c.WAV", "d.m4a", "e.OGG", "nested/f.aac")
        snapshots = {}
        for index, name in enumerate(names):
            path = scan_root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(f"fixture-{index}".encode("utf-8"))
            snapshots[path] = (path.read_bytes(), path.stat().st_mtime_ns)
        ignored = scan_root / "ignore.txt"
        ignored.write_bytes(b"ignored")
        snapshots[ignored] = (ignored.read_bytes(), ignored.stat().st_mtime_ns)

        controller = LibraryScanController(self.config)
        completed = []
        controller.completed.connect(completed.append)
        window = MainWindow(controller)
        window.show()
        self.addCleanup(window.close)
        window.open_import()
        dialog = window._scan_dialog
        assert dialog is not None
        dialog.path_input.setText(str(scan_root))

        dialog.start_button.click()
        self.wait_until(lambda: not controller.running)

        self.assertEqual(completed, [6])
        connection = open_database(self.config)
        try:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM assets").fetchone()[0], 6)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM scan_items").fetchone()[0], 6)
            self.assertEqual(connection.execute("SELECT status FROM scan_sessions").fetchone()[0], "completed")
            remembered_json = connection.execute(
                "SELECT value_json FROM settings WHERE key = ?", (LAST_SUCCESSFUL_ROOT_KEY,)
            ).fetchone()[0]
            self.assertIn(str(scan_root).replace("\\", "\\\\"), remembered_json)
        finally:
            connection.close()

        page = window.pages["所有音乐"]
        self.assertEqual(len(page.all_data), 6)
        self.assertEqual({record["title"] for record in page.all_data}, {Path(name).stem for name in names})
        self.assertTrue(all(record["artist"] == "待识别" for record in page.all_data))
        self.assertFalse(any(record["title"] == SONGS[0]["title"] for record in page.all_data))
        self.assertEqual(dialog.table.rowCount(), 6)
        table_names = [dialog.table.item(row, 1).text() for row in range(dialog.table.rowCount())]
        self.assertTrue(all(not Path(name).is_absolute() for name in table_names))
        self.assertTrue(all(str(scan_root) not in name for name in table_names))
        for path, snapshot in snapshots.items():
            self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), snapshot)

    def test_combined_production_import_entry_reaches_read_only_scan_and_refreshes_library(self) -> None:
        scan_root = self.root / "existing-music"
        target_root = self.root / "unused-import-target"
        scan_root.mkdir()
        target_root.mkdir()
        names = ("a.mp3", "b.FLAC", "c.wav", "d.M4A", "e.ogg", "nested/f.AAC")
        snapshots: dict[Path, tuple[bytes, int]] = {}
        for index, name in enumerate(names):
            path = scan_root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(f"existing-{index}".encode("utf-8"))
            snapshots[path] = (path.read_bytes(), path.stat().st_mtime_ns)

        scan_controller = LibraryScanController(self.config)
        safe_import_controller = SafeImportController(lambda: LibraryRepository(self.config))
        window = MainWindow(scan_controller, safe_import_controller=safe_import_controller)
        window.show()
        self.addCleanup(window.close)

        window.open_import()
        QApplication.processEvents()
        import_dialog = window._import_dialog
        self.assertIsInstance(import_dialog, ImportDialog)
        assert import_dialog is not None
        self.assertEqual(import_dialog.windowTitle(), "导入")
        self.assertEqual(import_dialog.read_only_scan_button.text(), "只读扫描已有音乐")
        self.assertEqual(import_dialog.move_button.text(), "开始安全移动导入")
        self.assertFalse(safe_import_controller.running)

        import_dialog.read_only_scan_button.click()
        QApplication.processEvents()
        scan_dialog = window._scan_dialog
        self.assertIsInstance(scan_dialog, ReadOnlyScanDialog)
        assert scan_dialog is not None
        scan_dialog.path_input.setText(str(scan_root))
        scan_dialog.start_button.click()
        self.wait_until(lambda: not scan_controller.running)

        self.assertEqual(len(window.pages["所有音乐"].all_data), 6)
        self.assertEqual(scan_dialog.table.rowCount(), 6)
        self.assertFalse(safe_import_controller.running)
        self.assertFalse(import_dialog.isVisible())
        for path, snapshot in snapshots.items():
            self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), snapshot)
        self.assertEqual(tuple(target_root.iterdir()), ())

    def test_cancel_and_failure_keep_old_root_and_reload_committed_library_after_finished(self) -> None:
        old_root = self.root / "old-root"
        old_root.mkdir()
        self.seed_existing_library(old_root)

        started = threading.Event()
        release = threading.Event()

        class BlockingWorker(ReadOnlyScanWorker):
            def run(inner_self) -> None:
                started.set()
                if not release.wait(5):
                    return
                super().run()

        controller = LibraryScanController(self.config)
        changed = []
        cancelled = []
        controller.library_changed.connect(changed.append)
        controller.cancelled.connect(cancelled.append)
        scan_root = self.root / "cancel-root"
        scan_root.mkdir()
        with patch("services.library_scan_controller.ReadOnlyScanWorker", BlockingWorker):
            controller.start_scan(scan_root)

            def cancel_when_started() -> None:
                if started.is_set() and not release.is_set():
                    controller.request_cancel()
                    release.set()

            self.wait_until(lambda: not controller.running, poll=cancel_when_started)

        self.assertEqual(cancelled, [0])
        self.assertEqual(controller.remembered_root(), old_root)
        self.assertTrue(changed and changed[-1][0]["title"] == "remembered")

        failed = []
        changed.clear()
        controller.failed.connect(failed.append)
        controller.start_scan(self.root / "missing")
        self.wait_until(lambda: not controller.running)
        self.assertEqual(len(failed), 1)
        self.assertEqual(controller.remembered_root(), old_root)
        self.assertTrue(changed and changed[-1][0]["title"] == "remembered")

    def test_reload_failure_preserves_current_library_and_reports_warning_after_terminal(self) -> None:
        controller = LibraryScanController(self.config)
        events: list[tuple[str, object]] = []
        controller.library_changed.connect(lambda records: events.append(("library", records)))
        controller.completed.connect(lambda count: events.append(("completed", count)))
        controller.warning.connect(lambda message: events.append(("warning", message)))

        class FinishedWorker:
            def deleteLater(self) -> None:
                events.append(("deleted", True))

        controller._worker = FinishedWorker()  # type: ignore[assignment]
        controller._active_root = self.root / "scan"
        controller._terminal = ("completed", 3)
        with (
            patch.object(controller, "_remember_successful_root"),
            patch.object(controller, "load_library", side_effect=RuntimeError("reload failed")),
        ):
            controller._worker_finished()

        self.assertNotIn("library", [kind for kind, _value in events])
        self.assertIn(("completed", 3), events)
        self.assertTrue(any(kind == "warning" and "reload failed" in str(value) for kind, value in events))
        self.assertLess(
            [kind for kind, _value in events].index("completed"),
            [kind for kind, _value in events].index("warning"),
        )
        self.assertFalse(controller.running)

    def test_repeated_start_is_rejected_and_main_window_close_cancels_without_leak(self) -> None:
        started = threading.Event()
        release = threading.Event()

        class BlockingWorker(ReadOnlyScanWorker):
            def run(inner_self) -> None:
                started.set()
                if not release.wait(5):
                    return
                super().run()

        scan_root = self.root / "scan"
        scan_root.mkdir()
        controller = LibraryScanController(self.config)
        window = MainWindow(controller)
        window.show()
        with patch("services.library_scan_controller.ReadOnlyScanWorker", BlockingWorker):
            controller.start_scan(scan_root)
            self.wait_until(started.is_set)
            with self.assertRaisesRegex(RuntimeError, "正在运行"):
                controller.start_scan(scan_root)
            window.close()
            self.assertTrue(window.isVisible())
            release.set()
            self.wait_until(lambda: not controller.running)
            self.wait_until(lambda: not window.isVisible())
        self.flush_deletes()
        self.assertIsNone(controller._worker)
        self.assertEqual(controller.findChildren(QThread), [])

    def test_replace_data_clears_selection_sort_and_checks_but_keeps_search(self) -> None:
        page = LibraryPage("测试", SONGS[:3])
        page.apply_search_immediately("风")
        if page.table.rowCount():
            page.table.selectRow(0)
            page.table.item(0, 0).setCheckState(Qt.CheckState.Checked)
        page.sort_key = "title"
        page.sort_descending = True

        page.replace_data(
            (
                {"title": "风继续吹", "artist": "待识别", "duration": "—", "format": "MP3", "size": "1 B", "status": "未检查"},
                {"title": "无关", "artist": "待识别", "duration": "—", "format": "FLAC", "size": "2 B", "status": "未检查"},
            )
        )

        self.assertEqual(page.search.text(), "风")
        self.assertFalse(page.search_timer.isActive())
        self.assertEqual([record["title"] for record in page.visible_data], ["风继续吹"])
        self.assertEqual([record["_index"] for record in page.all_data], [0, 1])
        self.assertIsNone(page.sort_key)
        self.assertEqual(page.checkable_header.check_state(), Qt.CheckState.Unchecked)
        self.assertFalse(page.add_button.isEnabled())
        self.assertFalse(page.delete_button.isEnabled())
        self.assertEqual(page.count_label.text(), "共 2 首")

    def test_main_window_without_controller_keeps_mock_library_and_import_dialog(self) -> None:
        window = MainWindow()
        self.addCleanup(window.close)
        self.assertEqual(len(window.pages["所有音乐"].all_data), len(SONGS))

        window.open_import()
        QApplication.processEvents()

        self.assertIsInstance(window._open_windows[0], ImportDialog)

    def test_production_database_path_uses_only_absolute_app_data_location(self) -> None:
        project_root = Path(__file__).resolve().parents[1]
        before = set(project_root.glob("*.sqlite3"))
        app_data = self.root / "production-app-data"
        with patch.object(QStandardPaths, "writableLocation", return_value=str(app_data)):
            config = build_production_database_config()
        self.assertEqual(config.path, app_data / "library.sqlite3")
        self.assertTrue(app_data.is_dir())
        self.assertFalse(config.path.exists())
        self.assertEqual(set(project_root.glob("*.sqlite3")), before)
        for invalid in ("", "relative/app-data"):
            with self.subTest(invalid=invalid):
                with patch.object(QStandardPaths, "writableLocation", return_value=invalid):
                    with self.assertRaises(RuntimeError):
                        build_production_database_config()

    def test_ui_modules_do_not_import_database_or_repository_layers(self) -> None:
        project_root = Path(__file__).resolve().parents[1]
        for relative in (
            "ui/main_window.py",
            "ui/music_page.py",
            "dialogs/read_only_scan_dialog.py",
        ):
            text = (project_root / relative).read_text(encoding="utf-8")
            self.assertNotIn("import sqlite3", text)
            self.assertNotIn("from database", text)
            self.assertNotIn("from repositories", text)


if __name__ == "__main__":
    unittest.main()
