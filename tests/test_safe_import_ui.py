from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest
from unittest import mock

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication, QMessageBox

from dialogs.import_dialog import ImportDialog
from services.safe_import import SafeImportController
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


if __name__ == "__main__":
    unittest.main()
