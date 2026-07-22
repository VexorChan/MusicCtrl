from __future__ import annotations

from contextlib import ExitStack
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import time
from types import SimpleNamespace
import unittest
from unittest import mock

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication, QMessageBox

import main as app_main
from database import DatabaseConfig
from dialogs.import_dialog import ImportDialog
from repositories import LibraryRepository
from services.safe_import import PENDING_IMPORT_KEY, SafeImportController, _journal_for_plan
from ui.main_window import MainWindow


class _ScanController(QObject):
    library_changed = Signal(object)
    running_changed = Signal(bool)
    warning = Signal(str)
    batch_committed = Signal(object)
    completed = Signal(object)
    cancelled = Signal(object)
    failed = Signal(str)

    running = False

    def load_library(self):
        return ()

    def remembered_root(self):
        return None

    def request_cancel(self):
        pass


class _StartupSafeImportController(QObject):
    completed = Signal(object)
    cancelled = Signal(object)
    failed = Signal(str)
    warning = Signal(str)
    running_changed = Signal(bool)

    def __init__(self) -> None:
        super().__init__()
        self.running = False
        self.phase = "recovery"
        self.starts = 0

    def start_recovery(self) -> None:
        self.starts += 1
        self.running = True
        self.running_changed.emit(True)

    def finish(self) -> None:
        self.running = False
        self.running_changed.emit(False)

    def request_cancel(self) -> None:
        self.finish()


class _StartupPlaylistController(QObject):
    completed = Signal(object)
    cancelled = Signal(object)
    failed = Signal(str)
    warning = Signal(str)
    running_changed = Signal(bool)
    recovery_detected = Signal()
    snapshot_ready = Signal(object)
    root_changed = Signal(object)

    def __init__(self) -> None:
        super().__init__()
        self.running = False
        self.starts = 0
        self.last_terminal_kind = ""

    def remembered_root(self):
        return None

    def start_pending_retarget_recovery(self) -> None:
        self.starts += 1
        self.running = True
        self.running_changed.emit(True)

    def finish(self) -> None:
        self.running = False
        self.running_changed.emit(False)

    def request_cancel(self) -> None:
        self.finish()


class SafeImportUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.source = self.root / "source"
        self.target = self.root / "target"
        self.source.mkdir()
        self.target.mkdir()

    def _wait(self, controller: SafeImportController) -> None:
        deadline = time.monotonic() + 5
        while controller.running and time.monotonic() < deadline:
            self.app.processEvents()
        self.app.processEvents()
        self.assertFalse(controller.running)

    def test_live_dialog_keeps_read_only_entry_and_requires_preview(self) -> None:
        dialog = ImportDialog(live_mode=True)
        self.addCleanup(dialog.close)
        self.assertEqual(dialog.move_button.text(), "开始安全移动导入")
        self.assertTrue(dialog.read_only_scan_button.isVisible() or not dialog.isVisible())
        self.assertFalse(dialog.move_button.isEnabled())
        self.assertEqual(dialog.table.columnCount(), 4)
        self.assertEqual(dialog.table.horizontalHeaderItem(1).text(), "源文件")
        self.assertEqual(dialog.table.horizontalHeaderItem(2).text(), "目标文件")

    def test_main_window_preview_invalidation_and_confirmed_execution(self) -> None:
        source = self.source / "song.mp3"
        source.write_bytes(b"audio")
        controller = SafeImportController()
        window = MainWindow(safe_import_controller=controller)
        self.addCleanup(window.close)
        window.open_import()
        dialog = window._import_dialog
        self.assertIsNotNone(dialog)
        dialog.scan_path.setText(str(self.source))
        dialog.target_path.setText(str(self.target))
        dialog.preview_button.click()
        self._wait(controller)
        self.assertIsNotNone(controller.current_plan)
        self.assertEqual(dialog.table.rowCount(), 1)
        self.assertTrue(dialog.move_button.isEnabled())

        dialog.scan_path.setText(str(self.source) + "-changed")
        self.assertIsNone(controller.current_plan)
        self.assertFalse(dialog.move_button.isEnabled())

        dialog.clear_preview()
        self.assertEqual(dialog.table.rowCount(), 0)
        self.assertFalse(dialog.move_button.isEnabled())

        dialog.scan_path.setText(str(self.source))
        dialog.preview_button.click()
        self._wait(controller)
        self.assertIsNotNone(controller.current_plan)
        with mock.patch.object(window, "_has_running_background_task", return_value=True):
            window._execute_safe_import(controller.current_plan.id)
        self.assertIsNotNone(controller.current_plan)
        self.assertTrue(source.exists())
        confirmation: list[str] = []

        def confirm(_parent, _title, message, *_args):
            confirmation.append(message)
            return QMessageBox.StandardButton.Yes

        with mock.patch.object(
            QMessageBox,
            "question",
            side_effect=confirm,
        ):
            dialog.move_button.click()
        self._wait(controller)
        self.assertIn("模式：音频", confirmation[0])
        self.assertIn("可执行 1", confirmation[0])
        self.assertIn("重复 0", confirmation[0])
        self.assertIn("冲突 0", confirmation[0])
        self.assertIn("失败 0", confirmation[0])
        self.assertIn(str(self.source), confirmation[0])
        self.assertIn(str(self.target), confirmation[0])
        self.assertFalse(source.exists())
        self.assertEqual((self.target / source.name).read_bytes(), b"audio")

    def test_execute_rechecks_background_state_after_confirmation(self) -> None:
        source = self.source / "song.mp3"
        source.write_bytes(b"audio")
        controller = SafeImportController()
        window = MainWindow(safe_import_controller=controller)
        self.addCleanup(window.close)
        window.open_import()
        dialog = window._import_dialog
        dialog.scan_path.setText(str(self.source))
        dialog.target_path.setText(str(self.target))
        dialog.preview_button.click()
        self._wait(controller)
        plan = controller.current_plan
        self.assertIsNotNone(plan)
        with (
            mock.patch.object(window, "_has_running_background_task", side_effect=[False, True]),
            mock.patch.object(
                QMessageBox,
                "question",
                return_value=QMessageBox.StandardButton.Yes,
            ),
        ):
            window._execute_safe_import(plan.id)
        self.assertFalse(controller.running)
        self.assertIs(controller.current_plan, plan)
        self.assertTrue(source.exists())
        self.assertIn("确认期间", dialog.summary.text())

    def test_switching_to_read_only_scan_clears_controller_and_dialog_plan(self) -> None:
        (self.source / "song.mp3").write_bytes(b"audio")
        controller = SafeImportController()
        scan = _ScanController()
        window = MainWindow(scan, safe_import_controller=controller)
        self.addCleanup(window.close)
        window.open_import()
        dialog = window._import_dialog
        dialog.scan_path.setText(str(self.source))
        dialog.target_path.setText(str(self.target))
        dialog.preview_button.click()
        self._wait(controller)
        self.assertIsNotNone(controller.current_plan)
        self.assertTrue(dialog.move_button.isEnabled())
        window.open_read_only_scan()
        self.assertIsNone(controller.current_plan)
        self.assertIsNone(dialog._plan_id)
        self.assertEqual(dialog.table.rowCount(), 0)
        self.assertFalse(dialog.move_button.isEnabled())

    def test_startup_recovery_is_silent_without_pending_journal(self) -> None:
        config = DatabaseConfig(self.root / "silent.sqlite3")
        controller = SafeImportController(lambda: LibraryRepository(config))
        window = MainWindow(safe_import_controller=controller)
        self.addCleanup(window.close)

        window.start_pending_safe_import_recovery()
        self._wait(controller)

        self.assertFalse(controller.running)
        self.assertIsNone(window._import_dialog)

    def test_startup_recovery_rolls_forward_target_only_and_shows_result(self) -> None:
        source = self.source / "interrupted.mp3"
        payload = b"verified startup recovery"
        source.write_bytes(payload)
        config = DatabaseConfig(self.root / "startup-recovery.sqlite3")
        preparing = SafeImportController(lambda: LibraryRepository(config))
        preparing.start_preview(self.source, self.target, "audio")
        self._wait(preparing)
        plan = preparing.current_plan
        self.assertIsNotNone(plan)
        journal = _journal_for_plan(plan)
        journal["items"][0]["state"] = "target_placed"
        target = self.target / source.name
        target.write_bytes(payload)
        source.unlink()
        with LibraryRepository(config) as repository:
            repository.set_setting(PENDING_IMPORT_KEY, journal)

        controller = SafeImportController(lambda: LibraryRepository(config))
        window = MainWindow(safe_import_controller=controller)
        self.addCleanup(window.close)
        window.start_pending_safe_import_recovery()
        self._wait(controller)

        self.assertIsNotNone(window._import_dialog)
        self.assertEqual(target.read_bytes(), payload)
        self.assertIn("成功 1", window._import_dialog.summary.text())
        with LibraryRepository(config) as repository:
            self.assertIsNone(repository.get_setting(PENDING_IMPORT_KEY))

    def test_startup_recovery_keeps_corrupt_journal_and_shows_error(self) -> None:
        config = DatabaseConfig(self.root / "corrupt-recovery.sqlite3")
        corrupt = {"version": 999, "batch_id": "bad"}
        with LibraryRepository(config) as repository:
            repository.set_setting(PENDING_IMPORT_KEY, corrupt)
        controller = SafeImportController(lambda: LibraryRepository(config))
        window = MainWindow(safe_import_controller=controller)
        self.addCleanup(window.close)

        window.start_pending_safe_import_recovery()
        self._wait(controller)

        self.assertIsNotNone(window._import_dialog)
        self.assertIn("恢复日志已保留", window._import_dialog.summary.text())
        with LibraryRepository(config) as repository:
            self.assertEqual(repository.get_setting(PENDING_IMPORT_KEY).value, corrupt)

    def test_startup_recovery_detection_never_opens_database_on_ui_thread(self) -> None:
        config = DatabaseConfig(self.root / "slow-detection.sqlite3")
        entered = threading.Event()
        release = threading.Event()

        def slow_factory():
            entered.set()
            release.wait(2)
            return LibraryRepository(config)

        controller = SafeImportController(slow_factory)
        window = MainWindow(safe_import_controller=controller)
        self.addCleanup(window.close)
        started_at = time.monotonic()

        window.start_pending_safe_import_recovery()

        self.assertLess(time.monotonic() - started_at, 0.1)
        deadline = time.monotonic() + 1
        while not entered.is_set() and time.monotonic() < deadline:
            self.app.processEvents()
        self.assertTrue(entered.is_set())
        self.assertTrue(controller.running)
        self.assertIsNone(window._import_dialog)
        release.set()
        self._wait(controller)
        self.assertIsNone(window._import_dialog)

    def test_playlist_recovery_queues_behind_import_and_reports_status(self) -> None:
        safe_import = _StartupSafeImportController()
        playlist = _StartupPlaylistController()
        window = MainWindow(
            playlist_controller=playlist,
            safe_import_controller=safe_import,
        )
        self.addCleanup(window.close)

        window.start_pending_safe_import_recovery()
        window.start_pending_playlist_retarget_recovery()
        self.assertEqual(safe_import.starts, 1)
        self.assertEqual(playlist.starts, 0)
        self.assertTrue(window._playlist_retarget_recovery_queued)

        safe_import.finish()
        self.assertEqual(playlist.starts, 1)
        self.assertFalse(window._playlist_retarget_recovery_queued)
        playlist.recovery_detected.emit()
        self.assertIn("正在安全重试", window.pages["所有音乐"].status.text())

        playlist.warning.emit("日志收尾失败")
        self.assertIn("日志收尾失败", window.pages["所有音乐"].status.text())
        playlist.completed.emit(
            SimpleNamespace(
                action="retarget",
                success_count=1,
                skipped_count=0,
                failure_count=1,
            )
        )
        self.assertIn("下次启动会自动重试", window.pages["所有音乐"].status.text())
        playlist.finish()

    def test_close_pending_discards_queued_playlist_recovery(self) -> None:
        playlist = _StartupPlaylistController()
        window = MainWindow(playlist_controller=playlist)
        self.addCleanup(window.close)
        window._playlist_retarget_recovery_queued = True
        window._close_pending = True

        window._background_running_changed(False)

        self.assertFalse(window._playlist_retarget_recovery_queued)
        self.assertEqual(playlist.starts, 0)

    def test_production_main_schedules_recovery_after_show(self) -> None:
        config = DatabaseConfig(self.root / "main-wiring.sqlite3")
        fake_app = mock.Mock()
        fake_app.exec.return_value = 23
        fake_window = mock.Mock()
        controller_names = (
            "LibraryScanController",
            "MetadataPreviewController",
            "SafeRenameController",
            "LyricsMatchController",
            "PlaylistController",
            "SafeImportController",
            "BackupController",
        )

        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(app_main, "build_app", return_value=fake_app))
            stack.enter_context(mock.patch.object(
                app_main, "build_production_database_config", return_value=config
            ))
            stack.enter_context(
                mock.patch.object(app_main, "MainWindow", return_value=fake_window)
            )
            single_shot = stack.enter_context(
                mock.patch.object(app_main.QTimer, "singleShot")
            )
            for name in controller_names:
                stack.enter_context(
                    mock.patch.object(app_main, name, return_value=mock.Mock())
                )
            result = app_main.main()

        self.assertEqual(result, 23)
        fake_window.show.assert_called_once_with()
        self.assertEqual(
            single_shot.call_args_list,
            [
                mock.call(0, fake_window.start_pending_safe_import_recovery),
                mock.call(0, fake_window.start_pending_playlist_retarget_recovery),
            ],
        )


if __name__ == "__main__":
    unittest.main()
