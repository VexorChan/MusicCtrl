from __future__ import annotations

import unittest

from PySide6.QtCore import QAbstractTableModel
from PySide6.QtWidgets import QApplication, QTableView

from dialogs.history_dialog import HistoryDialog
from dialogs.import_dialog import ImportDialog
from dialogs.lyrics_match_dialog import LyricsMatchDialog
from dialogs.read_only_scan_dialog import ReadOnlyScanDialog
from dialogs.rename_preview_dialog import RenamePreviewDialog
from services.history_service import HistorySnapshot
from ui.music_page import LibraryPage
from ui.tables import StatusBadgeDelegate


class ProductionTableTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def assert_model_table(self, table: object) -> None:
        self.assertIsInstance(table, QTableView)
        self.assertIsInstance(table.model(), QAbstractTableModel)

    def test_all_formal_pages_and_dialogs_use_model_view(self) -> None:
        music = LibraryPage("所有音乐", (), live_mode=True, use_model_view=True)
        lyrics = LibraryPage(
            "所有歌词", (), kind="lyrics", live_mode=True, use_model_view=True
        )
        import_dialog = ImportDialog(live_mode=True)
        rename_dialog = RenamePreviewDialog(live_mode=True)
        match_dialog = LyricsMatchDialog(live_mode=True)
        history_dialog = HistoryDialog(snapshot=HistorySnapshot(()))
        scan_dialog = ReadOnlyScanDialog()

        for table in (
            music.table,
            lyrics.table,
            import_dialog.table,
            rename_dialog.table,
            match_dialog.unmatched_results,
            match_dialog.conflict_results,
            history_dialog.table,
            history_dialog.detail_table,
            scan_dialog.table,
        ):
            with self.subTest(table=type(table).__name__):
                self.assert_model_table(table)

        self.assertIsInstance(
            import_dialog.table.itemDelegateForColumn(3), StatusBadgeDelegate
        )
        self.assertIsInstance(
            rename_dialog.table.itemDelegateForColumn(5), StatusBadgeDelegate
        )
        self.assertIsInstance(
            music.table.itemDelegateForColumn(6), StatusBadgeDelegate
        )
        self.assertIsInstance(
            lyrics.table.itemDelegateForColumn(5), StatusBadgeDelegate
        )
        self.assertEqual(history_dialog.table.rowCount(), 0)


if __name__ == "__main__":
    unittest.main()
