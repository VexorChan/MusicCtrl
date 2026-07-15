from __future__ import annotations

import ast
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import threading
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEvent, QObject, Signal, Qt
from PySide6.QtWidgets import QApplication

from database import DatabaseConfig
from dialogs.rename_preview_dialog import RenamePreviewDialog
from repositories import AssetRecord, AssetUpsert, IndexBatchItem, LibraryRepository
from services.library_scan_controller import (
    LAST_SUCCESSFUL_ROOT_KEY,
    LibraryScanController,
    asset_to_music_record,
)
from services.metadata_preview import MetadataPreviewController, MetadataPreviewResult
from ui.main_window import MainWindow
from ui.music_page import LibraryPage


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _flush_deferred_deletes() -> None:
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    QApplication.processEvents()


def _wait_until(predicate, timeout_ms: int = 3000) -> bool:
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        QApplication.processEvents()
        if predicate():
            return True
        time.sleep(0.005)
    QApplication.processEvents()
    return bool(predicate())


class _FakeScanController(QObject):
    batch_committed = Signal(object)
    library_changed = Signal(object)
    completed = Signal(int)
    cancelled = Signal(int)
    failed = Signal(str)
    warning = Signal(str)
    running_changed = Signal(bool)

    def __init__(self, root: Path, records=()) -> None:
        super().__init__()
        self.root = root
        self.records = tuple(records)
        self.running = False
        self.start_calls: list[Path] = []
        self.cancel_calls = 0

    def load_library(self):
        return self.records

    def remembered_root(self):
        return self.root

    def start_scan(self, root: Path) -> None:
        self.start_calls.append(root)

    def request_cancel(self) -> None:
        self.cancel_calls += 1

    def set_running(self, running: bool) -> None:
        self.running = running
        self.running_changed.emit(running)


class _FakeMetadataController(QObject):
    results_ready = Signal(object)
    cancelled = Signal(int)
    failed = Signal(str)
    running_changed = Signal(bool)

    def __init__(self) -> None:
        super().__init__()
        self.running = False
        self.start_calls: list[tuple[object, ...]] = []
        self.cancel_calls = 0

    def start(self, items) -> None:
        self.start_calls.append(tuple(items))
        self.running = True
        self.running_changed.emit(True)

    def request_cancel(self) -> None:
        self.cancel_calls += 1

    def set_running(self, running: bool) -> None:
        self.running = running
        self.running_changed.emit(running)


class P2PreviewIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        _app()

    def setUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        self.root = Path(self.temporary_directory.name).resolve()

    def tearDown(self) -> None:
        _flush_deferred_deletes()
        self.temporary_directory.cleanup()

    def _record(
        self,
        index: int,
        *,
        title: str,
        allowed_root: Path | None = None,
    ) -> dict[str, object]:
        path = self.root / f"{title}-歌手{index}.mp3"
        path.write_bytes(f"fixture-{index}".encode("utf-8"))
        metadata = path.stat()
        return {
            "_asset_id": f"asset-{index}",
            "_canonical_path": path,
            "_allowed_root": self.root if allowed_root is None else allowed_root,
            "_file_state": "active",
            "_size_bytes": metadata.st_size,
            "_mtime_ns": metadata.st_mtime_ns,
            "title": title,
            "artist": f"歌手{index}",
            "duration": "—",
            "format": "MP3",
            "size": f"{metadata.st_size} B",
            "status": "未检查",
        }

    def _asset(self, path: Path, *, asset_id: str = "asset-1", state: str = "active") -> AssetRecord:
        metadata = path.stat()
        return AssetRecord(
            id=asset_id,
            kind="audio",
            canonical_path=path,
            normalized_path=os.path.normcase(os.path.normpath(str(path))),
            file_name=path.name,
            extension=path.suffix,
            size_bytes=metadata.st_size,
            mtime_ns=metadata.st_mtime_ns,
            file_state=state,
            created_at="2026-01-01T00:00:00+00:00",
            updated_at="2026-01-01T00:00:00+00:00",
        )

    def _dispose_window(self, window: MainWindow) -> None:
        window.close()
        window.deleteLater()
        _flush_deferred_deletes()

    def _database_config(self) -> DatabaseConfig:
        return DatabaseConfig(
            self.root / "library.sqlite3",
            timeout_seconds=1.0,
            busy_timeout_ms=1000,
        )

    @staticmethod
    def _index_completed(
        repository: LibraryRepository,
        root: Path,
        paths: tuple[Path, ...],
    ) -> None:
        session = repository.create_scan_session(mode="audio", source_folder=root)
        repository.index_scan_batch(
            session.id,
            tuple(
                IndexBatchItem(
                    path,
                    path.stat().st_size,
                    path.stat().st_mtime_ns,
                )
                for path in paths
            ),
        )
        repository.complete_scan_and_reconcile(session.id)

    def test_asset_mapping_carries_typed_hidden_p1_snapshot(self) -> None:
        path = self.root / "标题-歌手.mp3"
        path.write_bytes(b"fixture")
        metadata = path.stat()
        asset = AssetRecord(
            id="asset-1",
            kind="audio",
            canonical_path=path,
            normalized_path=os.path.normcase(os.path.normpath(str(path))),
            file_name=path.name,
            extension=path.suffix,
            size_bytes=metadata.st_size,
            mtime_ns=metadata.st_mtime_ns,
            file_state="external_changed",
            created_at="2026-01-01T00:00:00+00:00",
            updated_at="2026-01-01T00:00:00+00:00",
        )
        record = asset_to_music_record(asset, allowed_root=self.root)
        self.assertEqual(record["_asset_id"], asset.id)
        self.assertIs(record["_canonical_path"], path)
        self.assertEqual(record["_allowed_root"], self.root)
        self.assertEqual(record["_file_state"], "external_changed")
        self.assertIsInstance(record["_size_bytes"], int)
        self.assertIsInstance(record["_mtime_ns"], int)

    def test_real_a_then_b_scan_roots_support_separate_and_same_batch_preview(self) -> None:
        config = self._database_config()
        root_a = self.root / "A"
        root_b = self.root / "B"
        root_a.mkdir()
        root_b.mkdir()
        path_a = root_a / "甲-歌手.mp3"
        path_b = root_b / "乙-歌手.flac"
        path_a.write_bytes(b"a-fixture")
        path_b.write_bytes(b"b-fixture")
        repository = LibraryRepository(config)
        try:
            self._index_completed(repository, root_a, (path_a,))
            self._index_completed(repository, root_b, (path_b,))
        finally:
            repository.close()

        scan = LibraryScanController(config)
        records = scan.load_library()
        roots_by_path = {
            record["_canonical_path"]: record["_allowed_root"]
            for record in records
        }
        self.assertEqual(roots_by_path, {path_a: root_a, path_b: root_b})
        metadata = _FakeMetadataController()
        window = MainWindow(scan, metadata)
        window.show()
        page = window.pages["所有音乐"]

        def select_path(path: Path) -> None:
            page.table.clearSelection()
            for row, record in enumerate(page.visible_data):
                if record["_canonical_path"] == path:
                    page.table.selectRow(row)
                    return
            self.fail(f"页面缺少路径：{path}")

        for expected_path, expected_root in ((path_a, root_a), (path_b, root_b)):
            select_path(expected_path)
            window.open_rename()
            item = metadata.start_calls[-1][0]
            self.assertEqual(item.canonical_path, expected_path)
            self.assertEqual(item.allowed_root, expected_root)
            metadata.set_running(False)
            window._rename_dialog.close()
            _flush_deferred_deletes()

        page.table.clearSelection()
        for row in range(page.table.rowCount()):
            page.table.item(row, 0).setCheckState(Qt.CheckState.Checked)
        window.open_rename()
        batch = metadata.start_calls[-1]
        self.assertEqual(
            {item.canonical_path: item.allowed_root for item in batch},
            {path_a: root_a, path_b: root_b},
        )
        metadata.set_running(False)
        self._dispose_window(window)

    def test_asset_without_completed_indexed_root_is_visible_but_preview_is_rejected(self) -> None:
        config = self._database_config()
        root = self.root / "unprovenanced"
        root.mkdir()
        path = root / "无来源-歌手.mp3"
        path.write_bytes(b"fixture")
        repository = LibraryRepository(config)
        try:
            repository.upsert_asset(
                AssetUpsert(path, path.stat().st_size, path.stat().st_mtime_ns)
            )
            repository.set_setting(LAST_SUCCESSFUL_ROOT_KEY, str(root))
        finally:
            repository.close()

        scan = LibraryScanController(config)
        records = scan.load_library()
        self.assertEqual(len(records), 1)
        self.assertIsNone(records[0]["_allowed_root"])
        metadata = _FakeMetadataController()
        window = MainWindow(scan, metadata)
        window.show()
        window.pages["所有音乐"].table.selectRow(0)
        window.open_rename()

        self.assertEqual(metadata.start_calls, [])
        self.assertIn("已完成扫描来源", window._rename_dialog.summary.text())
        self._dispose_window(window)

    def test_selected_records_is_union_in_visible_order_and_returns_copies(self) -> None:
        records = [
            self._record(0, title="丙"),
            self._record(1, title="甲"),
            self._record(2, title="乙"),
        ]
        page = LibraryPage("所有音乐", records)
        page._sort_records("title", False)
        visible_ids = [record["_asset_id"] for record in page.visible_data]
        page.table.selectRow(0)
        page.table.item(1, 0).setCheckState(Qt.CheckState.Checked)
        selected = page.selected_records()
        self.assertIsInstance(selected, tuple)
        self.assertEqual([record["_asset_id"] for record in selected], visible_ids[:2])

        selected[0]["title"] = "不得污染页面"
        self.assertNotEqual(page.visible_data[0]["title"], "不得污染页面")

        page.apply_search_immediately("乙")
        page.table.selectRow(0)
        filtered = page.selected_records()
        self.assertEqual([record["title"] for record in filtered], ["乙"])

    def test_ui_layers_do_not_import_repository_database_or_sqlite(self) -> None:
        project_root = Path(__file__).resolve().parents[1]
        for relative in (
            "dialogs/rename_preview_dialog.py",
            "ui/music_page.py",
            "ui/main_window.py",
        ):
            source = (project_root / relative).read_text(encoding="utf-8")
            tree = ast.parse(source)
            imported: set[str] = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported.update(alias.name.split(".")[0] for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imported.add(node.module.split(".")[0])
            self.assertTrue(
                {"sqlite3", "repositories", "database"}.isdisjoint(imported),
                f"{relative} 不能直接访问 repository/SQLite",
            )

    def test_live_dialog_only_allows_editing_suggested_stem_and_has_no_write_entry(self) -> None:
        source = self.root / "原文件名.mp3"
        source.write_bytes(b"fixture")
        ready = MetadataPreviewResult(
            asset_id="ready",
            canonical_path=source,
            original_name=source.name,
            suggested_stem="建议歌名-建议歌手",
            extension=".mp3",
            title="建议歌名",
            artist="建议歌手",
            source="标签",
            status="可预览",
            message="只读分析完成",
            requires_confirmation=False,
        )
        manual = MetadataPreviewResult(
            asset_id="manual",
            canonical_path=source.with_name("manual.mp3"),
            original_name="manual.mp3",
            suggested_stem=None,
            extension=".mp3",
            title=None,
            artist=None,
            source="无法识别",
            status="待手动确认",
            message="需要人工确认",
            requires_confirmation=True,
        )
        dialog = RenamePreviewDialog(live_mode=True)
        dialog.replace_results((ready, manual))

        self.assertTrue(dialog.id3_checkbox.isHidden())
        self.assertEqual(dialog.primary_button.text(), "完成预览")
        self.assertNotIn("应用", dialog.primary_button.text())
        self.assertNotIn("重命名", dialog.primary_button.text())
        self.assertIn("未修改", dialog.summary.text())
        self.assertEqual(dialog.table.horizontalHeaderItem(1).text(), "完整源路径")
        self.assertEqual(dialog.table.item(0, 1).text(), str(source))
        self.assertTrue(dialog.table.item(0, 2).flags() & Qt.ItemFlag.ItemIsEditable)
        for column in (1, 3, 4, 5):
            self.assertFalse(dialog.table.item(0, column).flags() & Qt.ItemFlag.ItemIsEditable)
        self.assertEqual(dialog.table.item(0, 0).checkState(), Qt.CheckState.Checked)
        self.assertEqual(dialog.table.item(1, 0).checkState(), Qt.CheckState.Unchecked)
        self.assertFalse(dialog.table.item(1, 2).flags() & Qt.ItemFlag.ItemIsEditable)
        dialog.close()

        mock_dialog = RenamePreviewDialog()
        self.assertFalse(mock_dialog.live_mode)
        self.assertFalse(mock_dialog.id3_checkbox.isHidden())
        self.assertEqual(mock_dialog.primary_button.text(), "应用重命名")
        self.assertGreater(mock_dialog.table.rowCount(), 0)
        mock_dialog.close()

    def test_real_selected_music_runs_controller_and_populates_live_dialog(self) -> None:
        path = self.root / "原始文件.mp3"
        path.write_bytes(b"fixture")
        before = (path.read_bytes(), path.stat().st_size, path.stat().st_mtime_ns, tuple(self.root.iterdir()))
        scan = _FakeScanController(
            self.root,
            (asset_to_music_record(self._asset(path), allowed_root=self.root),),
        )
        controller = MetadataPreviewController()
        window = MainWindow(scan, controller)
        window.show()
        page = window.pages["所有音乐"]
        page.table.selectRow(0)

        with patch(
            "services.metadata_preview.MutagenFile",
            return_value=type("Media", (), {"tags": {"title": ["真实标题"], "artist": ["真实歌手"]}})(),
        ):
            window.open_rename()
            self.assertIsNotNone(window._rename_dialog)
            self.assertTrue(_wait_until(lambda: not controller.running))

        dialog = window._rename_dialog
        self.assertIsNotNone(dialog)
        assert dialog is not None
        self.assertTrue(dialog.live_mode)
        self.assertEqual(dialog.table.rowCount(), 1)
        self.assertEqual(dialog.table.item(0, 1).text(), str(path))
        self.assertEqual(dialog.table.item(0, 2).text(), "真实标题-真实歌手")
        self.assertIn("未修改", dialog.summary.text())
        after = (path.read_bytes(), path.stat().st_size, path.stat().st_mtime_ns, tuple(self.root.iterdir()))
        self.assertEqual(before, after)
        self._dispose_window(window)

    def test_no_selection_wrong_page_and_missing_snapshot_never_start_worker(self) -> None:
        record = self._record(1, title="标题")
        scan = _FakeScanController(self.root, (record,))
        metadata = _FakeMetadataController()
        window = MainWindow(scan, metadata)
        window.show()

        window.open_rename()
        self.assertEqual(metadata.start_calls, [])
        self.assertIn("勾选或选中", window._rename_dialog.summary.text())
        window._rename_dialog.close()
        _flush_deferred_deletes()

        window.navigate("所有歌词")
        window.open_rename()
        self.assertEqual(metadata.start_calls, [])
        self.assertIn("所有音乐", window._rename_dialog.summary.text())
        window._rename_dialog.close()
        _flush_deferred_deletes()

        window.navigate("所有音乐")
        page = window.pages["所有音乐"]
        page.replace_data(
            ({
                "title": "缺少隐藏字段",
                "artist": "歌手",
                "duration": "—",
                "format": "MP3",
                "size": "1 B",
                "status": "未检查",
            },)
        )
        page.table.selectRow(0)
        window.open_rename()
        self.assertEqual(metadata.start_calls, [])
        self.assertIn("缺少 P1 索引信息", window._rename_dialog.summary.text())
        self._dispose_window(window)

    def test_start_freezes_selected_input_and_p1_p2_are_mutually_exclusive(self) -> None:
        record = self._record(2, title="冻结输入")
        scan = _FakeScanController(self.root, (record,))
        metadata = _FakeMetadataController()
        window = MainWindow(scan, metadata)
        window.show()
        page = window.pages["所有音乐"]
        page.table.selectRow(0)
        window.open_rename()
        self.assertEqual(len(metadata.start_calls), 1)
        items = metadata.start_calls[0]
        frozen = items[0]
        self.assertEqual(frozen.asset_id, record["_asset_id"])
        self.assertEqual(frozen.allowed_root, self.root)
        page.visible_data[0]["_asset_id"] = "mutated-after-start"
        self.assertNotEqual(frozen.asset_id, page.visible_data[0]["_asset_id"])

        window._start_read_only_scan(self.root)
        self.assertEqual(scan.start_calls, [])
        self.assertTrue(metadata.running)
        metadata.set_running(False)
        window._rename_dialog.close()
        _flush_deferred_deletes()

        page.table.selectRow(0)
        scan.set_running(True)
        window.open_rename()
        self.assertEqual(len(metadata.start_calls), 1)
        self.assertIn("扫描正在运行", window._rename_dialog.summary.text())
        scan.set_running(False)
        self._dispose_window(window)

    def test_main_window_close_requests_both_tasks_and_waits_for_both_terminals(self) -> None:
        scan = _FakeScanController(self.root)
        metadata = _FakeMetadataController()
        window = MainWindow(scan, metadata)
        window.show()
        scan.running = True
        metadata.running = True

        window.close()
        QApplication.processEvents()
        self.assertTrue(window.isVisible())
        self.assertEqual(scan.cancel_calls, 1)
        self.assertEqual(metadata.cancel_calls, 1)

        scan.set_running(False)
        QApplication.processEvents()
        self.assertTrue(window.isVisible())
        metadata.set_running(False)
        self.assertTrue(_wait_until(lambda: not window.isVisible()))
        window.deleteLater()
        _flush_deferred_deletes()

    def test_real_blocking_worker_is_cancelled_by_window_close_without_partial_preview(self) -> None:
        path = self.root / "阻塞-歌手.mp3"
        path.write_bytes(b"fixture")
        scan = _FakeScanController(
            self.root,
            (asset_to_music_record(self._asset(path), allowed_root=self.root),),
        )
        controller = MetadataPreviewController()
        window = MainWindow(scan, controller)
        window.show()
        window.pages["所有音乐"].table.selectRow(0)
        entered = threading.Event()
        release = threading.Event()
        calls: list[Path] = []

        def blocking_read(stream):
            calls.append(Path(stream.name))
            entered.set()
            self.assertTrue(release.wait(2))
            return "阻塞", "歌手", None, False

        with patch("services.metadata_preview._read_tags", side_effect=blocking_read):
            window.open_rename()
            self.assertTrue(entered.wait(2))
            dialog = window._rename_dialog
            self.assertIsNotNone(dialog)
            window.close()
            QApplication.processEvents()
            self.assertTrue(window.isVisible())
            release.set()
            self.assertTrue(_wait_until(lambda: not controller.running and not window.isVisible()))

        self.assertEqual(calls, [path])
        self.assertEqual(dialog.table.rowCount(), 0)
        self.assertEqual(controller.findChildren(QObject), [])
        window.deleteLater()
        _flush_deferred_deletes()

    def test_main_window_without_controllers_keeps_m1_mock_fallback(self) -> None:
        window = MainWindow()
        window.show()
        window.open_rename()
        self.assertEqual(len(window._open_windows), 1)
        dialog = window._open_windows[0]
        self.assertIsInstance(dialog, RenamePreviewDialog)
        self.assertFalse(dialog.live_mode)
        self.assertEqual(dialog.primary_button.text(), "应用重命名")
        self._dispose_window(window)


if __name__ == "__main__":
    unittest.main()
