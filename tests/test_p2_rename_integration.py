from __future__ import annotations

import base64
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEvent, QObject, Qt, Signal
from PySide6.QtWidgets import QApplication, QMessageBox

from database import DatabaseConfig
from repositories import AssetUpsert, LibraryRepository
import main as main_module
from services.metadata_preview import MetadataPreviewResult
from services.safe_rename import SafeRenameController, SafeRenameInput, SafeRenameRunResult
from services.safe_metadata import _read_title_artist
from tests.test_safe_metadata import _AUDIO_FIXTURES
from ui.main_window import MainWindow


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _flush_deferred_deletes() -> None:
    app = _app()
    app.processEvents()


def _wait_until(predicate, timeout_ms: int = 5000) -> bool:
    app = _app()
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        QCoreApplication.processEvents()
    return predicate()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()


class _FakeScanController(QObject):
    batch_committed = Signal(object)
    library_changed = Signal(object)
    completed = Signal(int)
    cancelled = Signal(int)
    failed = Signal(str)
    warning = Signal(str)
    running_changed = Signal(bool)

    def __init__(self, records) -> None:
        super().__init__()
        self.records = tuple(records)
        self.running = False
        self.cancel_requests = 0
        self.load_calls = 0

    def load_library(self):
        self.load_calls += 1
        return self.records

    def remembered_root(self):
        return None

    def start_scan(self, _root: Path) -> None:
        if self.running:
            raise RuntimeError("already running")
        self.running = True
        self.running_changed.emit(True)

    def request_cancel(self) -> None:
        self.cancel_requests += 1


class _FakeMetadataController(QObject):
    results_ready = Signal(object)
    cancelled = Signal(int)
    failed = Signal(str)
    running_changed = Signal(bool)

    def __init__(self) -> None:
        super().__init__()
        self.running = False
        self.starts: list[tuple] = []
        self.cancel_requests = 0

    def start(self, items) -> None:
        if self.running:
            raise RuntimeError("already running")
        self.starts.append(tuple(items))
        self.running = True
        self.running_changed.emit(True)

    def publish(self, results) -> None:
        self.results_ready.emit(tuple(results))
        self.running = False
        self.running_changed.emit(False)

    def request_cancel(self) -> None:
        self.cancel_requests += 1


class _FakeSafeRenameController(QObject):
    completed = Signal(object)
    cancelled = Signal(object)
    failed = Signal(str)
    running_changed = Signal(bool)

    def __init__(self) -> None:
        super().__init__()
        self.running = False
        self.starts: list[tuple[SafeRenameInput, ...]] = []
        self.cancel_requests = 0

    def start(self, items) -> None:
        if self.running:
            raise RuntimeError("already running")
        self.starts.append(tuple(items))
        self.running = True
        self.running_changed.emit(True)

    def request_cancel(self) -> None:
        self.cancel_requests += 1

    def publish_completed(self, result: SafeRenameRunResult) -> None:
        self.completed.emit(result)
        self.running = False
        self.running_changed.emit(False)


class P2RenameIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        _app()

    def setUp(self) -> None:
        self._temporary = TemporaryDirectory()
        self.root = Path(self._temporary.name)
        self.music = self.root / "music"
        self.music.mkdir()

    def tearDown(self) -> None:
        self._temporary.cleanup()
        _flush_deferred_deletes()

    def _record(
        self,
        path: Path,
        *,
        asset_id: str = "asset-1",
        allowed_root: Path | None = None,
        content: bytes = b"fixture",
    ):
        path.write_bytes(content)
        metadata = path.stat()
        return {
            "title": path.stem,
            "artist": "",
            "duration": "00:00",
            "format": path.suffix.removeprefix(".").upper(),
            "size": "7 B",
            "lyrics": "未检查",
            "_asset_id": asset_id,
            "_canonical_path": path,
            "_allowed_root": allowed_root or path.parent,
            "_file_state": "active",
            "_size_bytes": metadata.st_size,
            "_mtime_ns": metadata.st_mtime_ns,
        }

    @staticmethod
    def _preview(path: Path, *, asset_id: str = "asset-1", stem: str = "new", status: str = "可预览", confirm: bool = False):
        return MetadataPreviewResult(
            asset_id=asset_id,
            canonical_path=path,
            original_name=path.name,
            suggested_stem=stem,
            extension=path.suffix,
            title="new",
            artist="artist",
            source="标签",
            status=status,
            message="ready",
            requires_confirmation=confirm,
        )

    @staticmethod
    def _dispose(window: MainWindow) -> None:
        window.close()
        window.deleteLater()
        _flush_deferred_deletes()

    def _live_window(self, records):
        scan = _FakeScanController(records)
        metadata = _FakeMetadataController()
        safe = _FakeSafeRenameController()
        window = MainWindow(scan, metadata, safe)
        window.show()
        return window, scan, metadata, safe

    def _select_first_row_and_open(self, window: MainWindow, metadata: _FakeMetadataController):
        page = window.pages["所有音乐"]
        page.table.selectRow(0)
        window.open_rename()
        self.assertEqual(len(metadata.starts), 1)
        self.assertIsNotNone(window._rename_dialog)
        return window._rename_dialog

    def test_selected_preview_edit_confirm_builds_frozen_safe_input(self) -> None:
        source = self.music / "old.mp3"
        record = self._record(source)
        window, _scan, metadata, safe = self._live_window((record,))
        try:
            dialog = self._select_first_row_and_open(window, metadata)
            metadata.publish((self._preview(source),))
            dialog.id3_checkbox.setChecked(False)
            dialog.table.item(0, 2).setText("edited")
            with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes):
                dialog.primary_button.click()
            self.assertEqual(len(safe.starts), 1)
            request = safe.starts[0][0]
            self.assertEqual(request.asset_id, "asset-1")
            self.assertEqual(request.source_path, source)
            self.assertEqual(request.target_path, self.music / "edited.mp3")
            self.assertEqual(request.allowed_root, self.music)
            self.assertEqual(request.expected_size_bytes, record["_size_bytes"])
            self.assertEqual(request.expected_mtime_ns, record["_mtime_ns"])
        finally:
            self._dispose(window)

    def test_default_sync_uses_last_hyphen_for_supported_formats_and_skips_wav(self) -> None:
        flac = self.music / "old.flac"
        wav = self.music / "old.wav"
        records = (
            self._record(flac, asset_id="flac"),
            self._record(wav, asset_id="wav"),
        )
        window, _scan, metadata, safe = self._live_window(records)
        try:
            page = window.pages["所有音乐"]
            page.table.selectRow(0)
            page.table.item(1, 0).setCheckState(Qt.CheckState.Checked)
            window.open_rename()
            dialog = window._rename_dialog
            self.assertIsNotNone(dialog)
            metadata.publish(
                (
                    self._preview(flac, asset_id="flac", stem="歌-名-歌手"),
                    self._preview(wav, asset_id="wav", stem="波形歌-歌手"),
                )
            )
            with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes):
                dialog.primary_button.click()  # type: ignore[union-attr]
            self.assertEqual(len(safe.starts), 1)
            by_id = {item.asset_id: item for item in safe.starts[0]}
            self.assertTrue(by_id["flac"].sync_metadata)
            self.assertEqual((by_id["flac"].metadata_title, by_id["flac"].metadata_artist), ("歌-名", "歌手"))
            self.assertFalse(by_id["wav"].sync_metadata)
            self.assertIsNone(by_id["wav"].metadata_title)
        finally:
            self._dispose(window)

    def test_real_controller_sqlite_and_qthread_complete_confirmed_rename(self) -> None:
        source = self.music / "real-old.mp3"
        record = self._record(source, asset_id="placeholder")
        config = DatabaseConfig(self.root / "library.sqlite3")
        repository = LibraryRepository(config)
        try:
            asset = repository.upsert_asset(
                AssetUpsert(
                    source,
                    int(record["_size_bytes"]),
                    int(record["_mtime_ns"]),
                )
            )
        finally:
            repository.close()
        record["_asset_id"] = asset.id

        scan = _FakeScanController((record,))
        metadata = _FakeMetadataController()
        safe = SafeRenameController(lambda: LibraryRepository(config))
        completed: list[SafeRenameRunResult] = []
        safe.completed.connect(completed.append)
        window = MainWindow(scan, metadata, safe)
        window.show()
        target = self.music / "real-new.mp3"
        try:
            dialog = self._select_first_row_and_open(window, metadata)
            metadata.publish((self._preview(source, asset_id=asset.id, stem="real-new"),))
            dialog.id3_checkbox.setChecked(False)
            with patch.object(
                QMessageBox,
                "question",
                return_value=QMessageBox.StandardButton.Yes,
            ):
                dialog.primary_button.click()
            self.assertTrue(_wait_until(lambda: not safe.running and bool(completed)))
            self.assertFalse(source.exists())
            self.assertTrue(target.exists())
            self.assertEqual(target.read_bytes(), b"fixture")
            self.assertEqual(completed[0].success_count, 1)
            self.assertEqual(completed[0].failure_count, 0)

            readback = LibraryRepository(config)
            try:
                updated = readback.get_asset_by_id(asset.id)
                self.assertIsNotNone(updated)
                self.assertEqual(updated.canonical_path, target)  # type: ignore[union-attr]
            finally:
                readback.close()
        finally:
            self._dispose(window)

    def test_default_confirmed_mp3_syncs_tags_and_persists_new_fingerprint(self) -> None:
        source = self.music / "metadata-old.mp3"
        record = self._record(
            source,
            asset_id="placeholder",
            content=base64.b64decode(_AUDIO_FIXTURES[".mp3"]),
        )
        config = DatabaseConfig(self.root / "metadata-library.sqlite3")
        repository = LibraryRepository(config)
        try:
            asset = repository.upsert_asset(
                AssetUpsert(source, int(record["_size_bytes"]), int(record["_mtime_ns"]))
            )
        finally:
            repository.close()
        record["_asset_id"] = asset.id

        scan = _FakeScanController((record,))
        metadata = _FakeMetadataController()
        safe = SafeRenameController(lambda: LibraryRepository(config))
        completed: list[SafeRenameRunResult] = []
        safe.completed.connect(completed.append)
        window = MainWindow(scan, metadata, safe)
        window.show()
        target = self.music / "新标题-新歌手.mp3"
        try:
            dialog = self._select_first_row_and_open(window, metadata)
            metadata.publish((self._preview(source, asset_id=asset.id, stem="新标题-新歌手"),))
            self.assertTrue(dialog.id3_checkbox.isVisible())
            self.assertTrue(dialog.id3_checkbox.isChecked())
            with patch.object(
                QMessageBox,
                "question",
                return_value=QMessageBox.StandardButton.Yes,
            ):
                dialog.primary_button.click()
            self.assertTrue(_wait_until(lambda: not safe.running and bool(completed)))
            self.assertFalse(source.exists())
            self.assertEqual(_read_title_artist(target), ("新标题", "新歌手"))
            self.assertEqual(list(self.music.glob(".musicctrl-*")), [])
            stat_result = target.stat()
            readback = LibraryRepository(config)
            try:
                updated = readback.get_asset_by_id(asset.id)
                self.assertEqual((updated.size_bytes, updated.mtime_ns), (stat_result.st_size, stat_result.st_mtime_ns))  # type: ignore[union-attr]
                item = readback.list_rename_operation_items(completed[0].operations[0].id)[0]
                self.assertEqual(item.after["metadata_sync"]["written"]["title"], "新标题")
            finally:
                readback.close()
        finally:
            self._dispose(window)

    def test_final_confirmation_no_starts_no_worker_and_keeps_file(self) -> None:
        source = self.music / "old.mp3"
        window, _scan, metadata, safe = self._live_window((self._record(source),))
        try:
            dialog = self._select_first_row_and_open(window, metadata)
            metadata.publish((self._preview(source),))
            before = source.read_bytes()
            with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.No):
                dialog.primary_button.click()
            self.assertEqual(safe.starts, [])
            self.assertEqual(source.read_bytes(), before)
        finally:
            self._dispose(window)

    def test_row_selection_policy_requires_explicit_manual_and_disables_unsafe_results(self) -> None:
        paths = [self.music / f"{index}.mp3" for index in range(5)]
        records = tuple(self._record(path, asset_id=f"asset-{index}") for index, path in enumerate(paths))
        window, _scan, metadata, _safe = self._live_window(records)
        try:
            page = window.pages["所有音乐"]
            for row in range(5):
                page.table.item(row, 0).setCheckState(Qt.CheckState.Checked)
            window.open_rename()
            metadata.publish(
                (
                    self._preview(paths[0], asset_id="asset-0"),
                    self._preview(paths[1], asset_id="asset-1", status="外部变化", confirm=True),
                    self._preview(paths[2], asset_id="asset-2", status="待手动确认", confirm=True),
                    self._preview(paths[3], asset_id="asset-3", status="冲突", confirm=True),
                    self._preview(paths[4], asset_id="asset-4", status="分析失败", confirm=True),
                )
            )
            dialog = window._rename_dialog
            self.assertEqual(dialog.table.item(0, 0).checkState(), Qt.CheckState.Checked)
            self.assertEqual(dialog.table.item(1, 0).checkState(), Qt.CheckState.Unchecked)
            self.assertEqual(dialog.table.item(2, 0).checkState(), Qt.CheckState.Unchecked)
            self.assertFalse(bool(dialog.table.item(3, 0).flags() & Qt.ItemFlag.ItemIsUserCheckable))
            self.assertFalse(bool(dialog.table.item(4, 0).flags() & Qt.ItemFlag.ItemIsUserCheckable))
        finally:
            self._dispose(window)

    def test_invalid_or_duplicate_edited_names_show_warning_and_do_not_start(self) -> None:
        first = self.music / "one.mp3"
        second = self.music / "two.mp3"
        records = (self._record(first, asset_id="a"), self._record(second, asset_id="b"))
        for edited in ("", "CON", "bad?name", "same"):
            with self.subTest(edited=edited):
                window, _scan, metadata, safe = self._live_window(records)
                try:
                    page = window.pages["所有音乐"]
                    page.table.item(0, 0).setCheckState(Qt.CheckState.Checked)
                    page.table.item(1, 0).setCheckState(Qt.CheckState.Checked)
                    window.open_rename()
                    metadata.publish((self._preview(first, asset_id="a"), self._preview(second, asset_id="b")))
                    dialog = window._rename_dialog
                    dialog.table.item(0, 2).setText(edited)
                    if edited == "same":
                        dialog.table.item(1, 2).setText("SAME")
                    with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes):
                        dialog.primary_button.click()
                    self.assertEqual(safe.starts, [])
                    self.assertTrue(dialog.summary.text())
                finally:
                    self._dispose(window)

    def test_three_controllers_are_mutually_exclusive_and_close_requests_all_running(self) -> None:
        source = self.music / "old.mp3"
        window, scan, metadata, safe = self._live_window((self._record(source),))
        try:
            safe.running = True
            safe.running_changed.emit(True)
            window.open_import()
            self.assertFalse(scan.running)
            page = window.pages["所有音乐"]
            page.table.selectRow(0)
            window.open_rename()
            self.assertEqual(metadata.starts, [])
            scan.running = True
            metadata.running = True
            window.close()
            self.assertTrue(window.isVisible())
            self.assertEqual((scan.cancel_requests, metadata.cancel_requests, safe.cancel_requests), (1, 1, 1))
        finally:
            scan.running = metadata.running = safe.running = False
            scan.running_changed.emit(False)
            metadata.running_changed.emit(False)
            safe.running_changed.emit(False)
            self._dispose(window)

    def test_no_safe_controller_preserves_read_only_preview_and_m1_fallback(self) -> None:
        source = self.music / "old.mp3"
        scan = _FakeScanController((self._record(source),))
        metadata = _FakeMetadataController()
        window = MainWindow(scan, metadata)
        try:
            page = window.pages["所有音乐"]
            page.table.selectRow(0)
            window.open_rename()
            self.assertEqual(window._rename_dialog.primary_button.text(), "完成预览")
        finally:
            self._dispose(window)
        mock = MainWindow()
        try:
            mock.open_rename()
            self.assertTrue(any(type(child).__name__ == "RenamePreviewDialog" for child in mock._open_windows))
        finally:
            self._dispose(mock)

    def test_production_main_injects_safe_rename_controller_with_shared_config(self) -> None:
        config = DatabaseConfig(self.root / "production-test.sqlite3")
        app = Mock()
        app.exec.return_value = 23
        window = Mock()
        scan_controller = Mock()
        metadata_controller = Mock()
        safe_controller = Mock()
        lyrics_controller = Mock()
        playlist_controller = Mock()
        with (
            patch.object(main_module, "build_app", return_value=app),
            patch.object(
                main_module,
                "build_production_database_config",
                return_value=config,
            ),
            patch.object(
                main_module,
                "LibraryScanController",
                return_value=scan_controller,
            ) as scan_type,
            patch.object(
                main_module,
                "MetadataPreviewController",
                return_value=metadata_controller,
            ),
            patch.object(
                main_module,
                "SafeRenameController",
                return_value=safe_controller,
            ) as safe_type,
            patch.object(
                main_module,
                "LyricsMatchController",
                return_value=lyrics_controller,
            ),
            patch.object(
                main_module,
                "PlaylistController",
                return_value=playlist_controller,
            ),
            patch.object(main_module, "MainWindow", return_value=window) as window_type,
        ):
            self.assertEqual(main_module.main(), 23)

        scan_type.assert_called_once_with(config)
        repository_factory = safe_type.call_args.args[0]
        repository = repository_factory()
        try:
            self.assertEqual(repository._connection.execute("PRAGMA database_list").fetchone()[2], str(config.path))
        finally:
            repository.close()
            window_type.assert_called_once_with(
                scan_controller,
                metadata_controller,
                safe_controller,
                lyrics_controller,
                playlist_controller,
                use_model_view=True,
            )
        window.show.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
