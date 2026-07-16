from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication

from dialogs.history_dialog import HistoryDialog
from services.history_service import HistoryDetail, HistoryRecord, HistorySnapshot
from ui.main_window import MainWindow


class MutableImportController(QObject):
    completed = Signal(object)
    cancelled = Signal(object)
    failed = Signal(str)
    warning = Signal(str)
    running_changed = Signal(bool)

    def __init__(self, records=(), error: Exception | None = None) -> None:
        super().__init__()
        self.records = records
        self.error = error
        self.calls = 0
        self.running = False

    def list_history(self):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.records

    def request_cancel(self) -> None:
        pass


class HistoryUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        now = "2026-07-16T01:00:00+00:00"
        self.records = tuple(
            HistoryRecord(
                f"{category}-id",
                category,
                action,
                now,
                "成功",
                1,
                0,
                (
                    HistoryDetail(
                        f"{category}.mp3",
                        self.root / f"{category}-source.mp3",
                        self.root / f"{category}-target.mp3",
                        "success",
                        f"{category}-reason",
                        now,
                    ),
                ),
                ("backup-entry",) if category == "delete" else (),
                category == "import",
            )
            for category, action in (
                ("import", "导入音乐"),
                ("rename", "重命名"),
                ("delete", "删除到备份"),
                ("playlist", "添加到歌单"),
                ("lyrics", "歌词匹配"),
            )
        )

    def test_six_real_filters_and_selected_row_drive_file_details(self) -> None:
        dialog = HistoryDialog(snapshot=HistorySnapshot(self.records))
        self.addCleanup(dialog.close)

        self.assertEqual(tuple(dialog.filter_buttons), ("全部", "导入", "重命名", "删除", "歌单", "歌词匹配"))
        self.assertEqual(len(dialog.visible_records), 5)
        for label, category in (
            ("导入", "import"),
            ("重命名", "rename"),
            ("删除", "delete"),
            ("歌单", "playlist"),
            ("歌词匹配", "lyrics"),
        ):
            dialog.filter_buttons[label].click()
            self.app.processEvents()
            self.assertEqual([record.category for record in dialog.visible_records], [category])
            self.assertEqual(dialog.detail_table.rowCount(), 1)
            self.assertIn(category, dialog.detail_table.item(0, 0).text())
            self.assertIn(f"{category}-source", dialog.detail_table.item(0, 1).text())
            self.assertIn(f"{category}-target", dialog.detail_table.item(0, 2).text())
            self.assertEqual(dialog.detail_table.item(0, 4).text(), f"{category}-reason")

    def test_restore_and_undo_are_enabled_only_by_selected_record_eligibility(self) -> None:
        dialog = HistoryDialog(snapshot=HistorySnapshot(self.records))
        self.addCleanup(dialog.close)
        restored: list[object] = []
        undone: list[bool] = []
        dialog.restore_requested.connect(restored.append)
        dialog.undo_import_requested.connect(lambda: undone.append(True))

        dialog.filter_buttons["删除"].click()
        self.app.processEvents()
        self.assertTrue(dialog.restore_button.isEnabled())
        self.assertFalse(dialog.undo_import_button.isEnabled())
        dialog.restore_button.click()
        self.assertEqual(restored, [("backup-entry",)])

        dialog.filter_buttons["导入"].click()
        self.app.processEvents()
        self.assertFalse(dialog.restore_button.isEnabled())
        self.assertTrue(dialog.undo_import_button.isEnabled())
        dialog.undo_import_button.click()
        self.assertEqual(undone, [True])

        dialog.filter_buttons["重命名"].click()
        self.app.processEvents()
        self.assertFalse(dialog.restore_button.isEnabled())
        self.assertFalse(dialog.undo_import_button.isEnabled())

    def _import(self, identifier: str):
        return {
            "id": identifier,
            "created_at": "2026-07-16T01:00:00+00:00",
            "mode": "audio",
            "source_root": str(self.root),
            "target_root": str(self.root / "target"),
            "complete": True,
            "undone_at": None,
            "items": [
                {
                    "source_path": str(self.root / f"{identifier}.mp3"),
                    "target_path": str(self.root / "target" / f"{identifier}.mp3"),
                    "status": "success",
                    "message": "完成",
                }
            ],
        }

    def test_reopening_existing_live_dialog_reloads_instead_of_raising_stale_data(self) -> None:
        controller = MutableImportController((self._import("first"),))
        window = MainWindow(safe_import_controller=controller)
        self.addCleanup(window.close)

        window.open_history()
        dialog = window._history_dialog
        self.assertIsNotNone(dialog)
        self.assertEqual([record.id for record in dialog.visible_records], ["first"])  # type: ignore[union-attr]
        controller.records = (self._import("second"),)
        window.open_history()
        self.app.processEvents()

        self.assertIs(window._history_dialog, dialog)
        self.assertEqual(controller.calls, 2)
        self.assertEqual([record.id for record in dialog.visible_records], ["second"])  # type: ignore[union-attr]
        all_text = " ".join(
            dialog.table.item(row, column).text()
            for row in range(dialog.table.rowCount())  # type: ignore[union-attr]
            for column in range(4)
            if dialog.table.item(row, column) is not None  # type: ignore[union-attr]
        )
        self.assertNotIn("MusicCtrlDemo", all_text)

    def test_all_live_sources_can_fail_and_dialog_still_opens_empty_with_warning(self) -> None:
        controller = MutableImportController(error=RuntimeError("corrupt history"))
        window = MainWindow(safe_import_controller=controller)
        self.addCleanup(window.close)

        window.open_history()
        dialog = window._history_dialog

        self.assertIsNotNone(dialog)
        self.assertEqual(dialog.table.rowCount(), 0)  # type: ignore[union-attr]
        self.assertTrue(dialog.warning_label.isVisible())  # type: ignore[union-attr]
        self.assertIn("corrupt history", dialog.warning_label.text())  # type: ignore[union-attr]

    def test_mock_constructor_stays_compatible(self) -> None:
        dialog = HistoryDialog()
        self.addCleanup(dialog.close)
        self.assertGreater(dialog.table.rowCount(), 0)
        self.assertFalse(dialog.restore_button.isVisible())


if __name__ == "__main__":
    unittest.main()
