from __future__ import annotations

import os
import time
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QAbstractTableModel, Qt
from PySide6.QtWidgets import QApplication, QTableView

from ui.music_page import LibraryPage


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _record(index: int, *, title: str | None = None, artist: str | None = None):
    return {
        "_asset_id": f"asset-{index}",
        "title": title or f"Song {index}",
        "artist": artist or f"Artist {index % 100}",
        "duration": "03:00",
        "format": "MP3",
        "size": "5 MB",
        "status": "未检查",
        "file_status": "正常",
    }


class LibraryModelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        _app()

    def test_production_page_uses_checks_as_operation_source_of_truth(self) -> None:
        page = LibraryPage(
            "所有音乐",
            (_record(0), _record(1)),
            use_model_view=True,
            live_mode=True,
        )
        self.assertIsInstance(page.table, QTableView)
        self.assertIsInstance(page.table.model(), QAbstractTableModel)
        self.assertEqual(page.table.model().columnCount(), 8)
        self.assertEqual(
            [
                page.table.model().headerData(column, Qt.Orientation.Horizontal)
                for column in range(8)
            ],
            ["", "歌名", "歌手", "时长", "格式", "大小", "歌词状态", "文件状态"],
        )
        self.assertEqual(
            page.table.model().data(page.table.model().index(0, 7)),
            "正常",
        )
        page.table.selectRow(0)
        model = page.table.model()
        self.assertEqual(page._checked_rows(), [0])
        self.assertTrue(
            model.setData(
                model.index(1, 0),
                Qt.CheckState.Checked,
                Qt.ItemDataRole.CheckStateRole,
            )
        )
        self.assertEqual(
            [record["_asset_id"] for record in page.selected_records()],
            ["asset-0", "asset-1"],
        )
        self.assertIn("索引来自用户选择目录", page.status.text())

        page.table.clearSelection()
        self.assertEqual(
            [record["_asset_id"] for record in page.selected_records()],
            ["asset-0", "asset-1"],
        )

    def test_new_row_selection_checks_shift_or_ctrl_selected_rows_without_unchecking_old(self) -> None:
        page = LibraryPage(
            "所有音乐",
            tuple(_record(index) for index in range(5)),
            use_model_view=True,
            live_mode=True,
        )
        selection = page.table.selectionModel()
        model = page.table.model()
        selection.select(
            model.index(1, 0),
            selection.SelectionFlag.ClearAndSelect | selection.SelectionFlag.Rows,
        )
        selection.select(
            model.index(3, 0),
            selection.SelectionFlag.Select | selection.SelectionFlag.Rows,
        )
        self.assertEqual(page._checked_rows(), [1, 3])

        selection.clearSelection()
        self.assertEqual(page._checked_rows(), [1, 3])
        model.setData(model.index(1, 0), Qt.CheckState.Unchecked, Qt.ItemDataRole.CheckStateRole)
        self.assertEqual(
            [record["_asset_id"] for record in page.selected_records()],
            ["asset-3"],
        )
        self.assertIn("已选择 1 项", page.status.text())

    def test_playlist_menu_click_immediately_emits_one_destination_with_checked_records(self) -> None:
        page = LibraryPage(
            "所有音乐",
            (_record(0), _record(1)),
            use_model_view=True,
            live_mode=True,
            playlist_names=("通勤", "怀旧"),
        )
        model = page.table.model()
        model.setData(model.index(1, 0), Qt.CheckState.Checked, Qt.ItemDataRole.CheckStateRole)
        emitted: list[tuple[object, object]] = []
        page.add_to_playlists_requested.connect(
            lambda records, name: emitted.append((records, name))
        )

        menu = page.create_playlist_menu()
        menu.playlist_actions["通勤"].trigger()

        self.assertFalse(hasattr(menu, "confirm_button"))
        self.assertEqual(len(emitted), 1)
        records, name = emitted[0]
        self.assertEqual(name, "通勤")
        self.assertEqual([record["_asset_id"] for record in records], ["asset-1"])

    def test_model_search_is_title_first_and_sort_is_stable(self) -> None:
        records = (
            _record(0, title="目标歌曲", artist="甲"),
            _record(1, title="普通歌曲", artist="目标歌手"),
            _record(2, title="目标歌曲", artist="乙"),
        )
        page = LibraryPage("所有音乐", records, use_model_view=True)
        page.apply_search_immediately("目标")
        self.assertEqual(
            [record["_asset_id"] for record in page.visible_data],
            ["asset-0", "asset-2"],
        )
        page._sort_records("title", False)
        self.assertEqual(
            [record["_asset_id"] for record in page.visible_data],
            ["asset-0", "asset-2"],
        )
        self.assertTrue(page.table.horizontalHeader().isSortIndicatorShown())

        page._sort_records("file_status", False)
        self.assertEqual(page.sort_key, "file_status")

    def test_lyrics_model_places_file_status_after_lyrics_status(self) -> None:
        record = _record(0)
        record.update({"format": "LRC", "status": "已匹配", "file_status": "外部变化"})
        page = LibraryPage("所有歌词", (record,), kind="lyrics", use_model_view=True)
        model = page.table.model()

        self.assertEqual(model.columnCount(), 7)
        self.assertEqual(
            [model.headerData(column, Qt.Orientation.Horizontal) for column in range(7)],
            ["", "歌名", "歌手", "格式", "大小", "歌词状态", "文件状态"],
        )
        self.assertEqual(model.data(model.index(0, 5)), "已匹配")
        self.assertEqual(model.data(model.index(0, 6)), "外部变化")

    def test_model_reload_clears_selection_checks_and_sort_but_keeps_search(self) -> None:
        page = LibraryPage(
            "所有音乐",
            (_record(0, title="旧目标"), _record(1, title="旧普通")),
            use_model_view=True,
        )
        page.apply_search_immediately("目标")
        page._sort_records("title", True)
        model = page.table.model()
        model.setData(model.index(0, 0), Qt.CheckState.Checked, Qt.ItemDataRole.CheckStateRole)
        page.table.selectRow(0)

        page.replace_data((_record(2, title="新目标"), _record(3, title="新普通")))

        self.assertEqual(page.search.text(), "目标")
        self.assertEqual([record["_asset_id"] for record in page.visible_data], ["asset-2"])
        self.assertEqual(page.selected_records(), ())
        self.assertEqual(page._checked_rows(), [])
        self.assertIsNone(page.sort_key)
        self.assertFalse(page.table.horizontalHeader().isSortIndicatorShown())

    def test_ten_thousand_records_remain_practical_for_local_use(self) -> None:
        records = tuple(_record(index) for index in range(10_000))
        started = time.perf_counter()
        page = LibraryPage("所有音乐", records, use_model_view=True)
        construct_seconds = time.perf_counter() - started
        started = time.perf_counter()
        page.apply_search_immediately("song 99")
        search_seconds = time.perf_counter() - started
        started = time.perf_counter()
        page._sort_records("title", False)
        sort_seconds = time.perf_counter() - started
        self.assertLess(construct_seconds, 2.0)
        self.assertLess(search_seconds, 1.0)
        self.assertLess(sort_seconds, 1.0)
        self.assertGreater(len(page.visible_data), 0)


if __name__ == "__main__":
    unittest.main()
