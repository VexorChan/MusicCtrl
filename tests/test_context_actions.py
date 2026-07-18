from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest
from unittest import mock
import wave

from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication

from database import DatabaseConfig
from repositories import IndexBatchItem, LibraryRepository
from services.library_scan_controller import AudioAssetSnapshot, LibraryScanController
from services.lyrics_match_controller import LyricsMatchController
from services.metadata_preview import MetadataPreviewController
from services.safe_rename import SafeRenameController
from ui.main_window import MainWindow
from ui.music_page import LibraryPage


class ContextActionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.config = DatabaseConfig(self.root / "library.sqlite3")

    @staticmethod
    def _wav(path: Path) -> None:
        with wave.open(str(path), "wb") as stream:
            stream.setnchannels(1)
            stream.setsampwidth(2)
            stream.setframerate(8000)
            stream.writeframes(b"\0\0" * 80)

    def _seed_audio(self, root: Path, name: str):
        root.mkdir(exist_ok=True)
        path = root / name
        self._wav(path)
        metadata = path.stat()
        with LibraryRepository(self.config) as repository:
            session = repository.create_scan_session(mode="audio", source_folder=root)
            asset = repository.index_scan_batch(
                session.id,
                (IndexBatchItem(path, metadata.st_size, metadata.st_mtime_ns),),
            )[0].asset
            repository.finish_scan_session(session.id, status="completed")
        return asset

    def _wait(self, controller, timeout: float = 5) -> None:
        deadline = time.monotonic() + timeout
        while controller.running and time.monotonic() < deadline:
            self.app.processEvents()
        self.app.processEvents()
        self.assertFalse(controller.running)

    def _flush_deferred_deletes(self) -> None:
        for _ in range(3):
            self.app.processEvents()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            self.app.processEvents()

    def test_menu_is_strict_and_emits_creation_time_selection_copy(self) -> None:
        first = {"title": "A", "artist": "甲", "duration": "—", "format": "MP3", "size": "1 B", "status": "未检查"}
        second = {"title": "B", "artist": "乙", "duration": "—", "format": "MP3", "size": "1 B", "status": "未检查"}
        page = LibraryPage("所有音乐", (first, second), live_mode=True)
        self.addCleanup(page.close)
        page.table.selectRow(0)
        menu = page.create_context_menu()
        self.assertEqual(
            [action.text() for action in menu.actions()],
            ["打开所在文件夹", "重命名", "重新匹配歌词"],
        )
        opened: list[object] = []
        renamed: list[object] = []
        rematched: list[object] = []
        page.open_location_requested.connect(opened.append)
        page.rename_context_requested.connect(renamed.append)
        page.rematch_lyrics_requested.connect(rematched.append)

        page.table.selectRow(1)
        page.visible_data[0]["title"] = "已变化"
        for action in menu.actions():
            action.trigger()

        for payloads in (opened, renamed, rematched):
            self.assertEqual(len(payloads), 1)
            payload = payloads[0]
            self.assertEqual(len(payload), 1)
            self.assertEqual(payload[0]["title"], "A")
            payload[0]["title"] = "调用方修改"
        self.assertEqual(page.visible_data[0]["title"], "已变化")

    def test_revalidation_uses_index_provenance_and_rejects_drift(self) -> None:
        audio_root = self.root / "audio"
        asset = self._seed_audio(audio_root, "晴天-周杰伦.wav")
        controller = LibraryScanController(self.config)
        snapshot = AudioAssetSnapshot(
            asset.id,
            asset.canonical_path,
            asset.size_bytes,
            asset.mtime_ns,
        )
        record = controller.revalidate_audio_records((snapshot,))[0]
        self.assertEqual(record.asset_id, asset.id)
        self.assertEqual(record.allowed_root, audio_root)

        asset.canonical_path.write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "外部变化"):
            controller.revalidate_audio_records((snapshot,))

    def test_open_location_requires_one_revalidated_record_and_uses_argument_list(self) -> None:
        audio_root = self.root / "audio special,括号"
        asset = self._seed_audio(audio_root, "歌 名-歌手.wav")
        scan = LibraryScanController(self.config)
        window = MainWindow(scan_controller=scan)
        self.addCleanup(window.close)
        record = scan.load_library()[0]
        with mock.patch("ui.main_window.QProcess.startDetached", return_value=True) as launch:
            window._open_selected_location((dict(record),))
        launch.assert_called_once()
        program, arguments = launch.call_args.args
        self.assertEqual(program, "explorer.exe")
        self.assertEqual(arguments[0], "/select,")
        self.assertEqual(Path(arguments[1]), asset.canonical_path)

        with mock.patch("ui.main_window.QProcess.startDetached") as launch:
            window._open_selected_location((dict(record), dict(record)))
        launch.assert_not_called()
        self.assertIn("必须且只能选择", window.pages["所有音乐"].status.text())

        with (
            mock.patch("ui.main_window.QProcess.startDetached", return_value=False),
            mock.patch("ui.main_window.QDesktopServices.openUrl", return_value=True) as fallback,
        ):
            window._open_selected_location((dict(record),))
        fallback.assert_called_once()
        fallback_url = fallback.call_args.args[0]
        self.assertEqual(Path(fallback_url.toLocalFile()), asset.canonical_path.parent)

    def test_context_actions_reject_non_active_snapshot_and_forged_provenance(self) -> None:
        audio_root = self.root / "audio"
        self._seed_audio(audio_root, "A-甲.wav")
        scan = LibraryScanController(self.config)
        window = MainWindow(scan_controller=scan)
        self.addCleanup(window.close)
        record = dict(scan.load_library()[0])

        for state in ("missing", "external_changed"):
            changed = dict(record)
            changed["_file_state"] = state
            with mock.patch("ui.main_window.QProcess.startDetached") as launch:
                window._open_selected_location((changed,))
            launch.assert_not_called()
            self.assertIn("不是可操作状态", window.pages["所有音乐"].status.text())

        forged = dict(record)
        forged["_allowed_root"] = self.root
        with mock.patch("ui.main_window.QProcess.startDetached") as launch:
            window._open_selected_location((forged,))
        launch.assert_not_called()
        self.assertIn("扫描来源", window.pages["所有音乐"].status.text())

    def test_context_rename_passes_revalidated_frozen_records(self) -> None:
        audio_root = self.root / "audio"
        asset = self._seed_audio(audio_root, "A-甲.wav")
        scan = LibraryScanController(self.config)
        window = MainWindow(
            scan_controller=scan,
            metadata_preview_controller=MetadataPreviewController(),
            safe_rename_controller=SafeRenameController(
                lambda: LibraryRepository(self.config)
            ),
        )
        self.addCleanup(window.close)
        captured: list[object] = []
        with mock.patch.object(window, "open_rename", side_effect=captured.append):
            window._rename_selected_context((dict(scan.load_library()[0]),))
        self.assertEqual(len(captured), 1)
        record = captured[0][0]
        self.assertEqual(record["_asset_id"], asset.id)
        self.assertEqual(record["_canonical_path"], asset.canonical_path)
        self.assertEqual(record["_file_state"], "active")

    def test_context_rename_never_falls_back_to_mock_without_real_controllers(self) -> None:
        audio_root = self.root / "audio"
        self._seed_audio(audio_root, "A-甲.wav")
        scan = LibraryScanController(self.config)
        window = MainWindow(
            scan_controller=scan,
            metadata_preview_controller=MetadataPreviewController(),
        )
        self.addCleanup(window.close)
        with mock.patch.object(window, "open_rename") as open_rename:
            window._rename_selected_context((dict(scan.load_library()[0]),))
        open_rename.assert_not_called()
        self.assertIsNone(window._rename_dialog)
        self.assertIn("未启用真实安全重命名", window.pages["所有音乐"].status.text())

    def test_lyrics_scope_is_selected_only_across_multiple_audio_roots(self) -> None:
        first_root = self.root / "A"
        second_root = self.root / "B"
        third_root = self.root / "C"
        first = self._seed_audio(first_root, "晴天-周杰伦.wav")
        second = self._seed_audio(second_root, "夜曲-周杰伦.wav")
        third = self._seed_audio(third_root, "稻香-周杰伦.wav")
        lyrics_root = self.root / "lyrics"
        lyrics_root.mkdir()
        (lyrics_root / "晴天-周杰伦.lrc").write_text("[ti:晴天]\n[ar:周杰伦]", encoding="utf-8")
        (lyrics_root / "夜曲-周杰伦.lrc").write_text("[ti:夜曲]\n[ar:周杰伦]", encoding="utf-8")
        (lyrics_root / "稻香-周杰伦.lrc").write_text("[ti:稻香]\n[ar:周杰伦]", encoding="utf-8")

        scan = LibraryScanController(self.config)
        records = {record["_asset_id"]: record for record in scan.load_library()}
        validated = scan.revalidate_audio_records(
            (
                self._audio_snapshot(records[first.id]),
                self._audio_snapshot(records[second.id]),
            )
        )
        lyrics = LyricsMatchController(self.config)
        results: list[object] = []
        lyrics.results_ready.connect(results.append)
        lyrics.set_scope(validated)
        lyrics.start_scan(lyrics_root)
        self._wait(lyrics)
        self.assertIsNone(lyrics.pending_scope)
        self.assertEqual(
            {item.audio_asset_id for item in results[-1].items},
            {first.id, second.id},
        )
        self.assertNotIn(third.id, {item.audio_asset_id for item in results[-1].items})

        results.clear()
        lyrics.start_scan(lyrics_root)
        self._wait(lyrics)
        self.assertEqual(
            {item.audio_asset_id for item in results[-1].items},
            {first.id, second.id, third.id},
        )

    def test_context_lyrics_opens_scoped_dialog_without_automatic_scan_and_close_clears(self) -> None:
        audio_root = self.root / "audio"
        self._seed_audio(audio_root, "A-甲.wav")
        scan = LibraryScanController(self.config)
        lyrics = LyricsMatchController(self.config)
        window = MainWindow(scan_controller=scan, lyrics_match_controller=lyrics)
        self.addCleanup(window.close)

        window._rematch_selected_lyrics((dict(scan.load_library()[0]),))
        self.app.processEvents()
        self.assertIsNotNone(window._lyrics_dialog)
        self.assertIsNotNone(lyrics.pending_scope)
        self.assertFalse(lyrics.running)
        self.assertIn("请选择歌词目录并点击开始", window._lyrics_dialog.summary.text())

        window._lyrics_dialog.close()
        self.app.processEvents()
        self.assertIsNone(lyrics.pending_scope)

    def test_main_close_destroys_unstarted_scoped_dialog_and_clears_tracking(self) -> None:
        audio_root = self.root / "audio"
        self._seed_audio(audio_root, "A-甲.wav")
        scan = LibraryScanController(self.config)
        lyrics = LyricsMatchController(self.config)
        window = MainWindow(scan_controller=scan, lyrics_match_controller=lyrics)
        self.addCleanup(window.close)
        window.show()
        self.app.processEvents()
        window._rematch_selected_lyrics((dict(scan.load_library()[0]),))
        self.app.processEvents()
        dialog = window._lyrics_dialog
        self.assertIsNotNone(dialog)
        self.assertTrue(dialog.isVisible())
        self.assertIsNotNone(lyrics.pending_scope)

        window.close()
        self.assertFalse(window.isVisible())
        self.assertFalse(dialog.isVisible())
        self._flush_deferred_deletes()
        self.assertIsNone(lyrics.pending_scope)
        self.assertIsNone(window._lyrics_dialog)
        self.assertEqual(window._open_windows, [])

    def test_main_close_waits_for_running_scoped_scan_then_destroys_all_windows(self) -> None:
        audio_root = self.root / "audio"
        self._seed_audio(audio_root, "A-甲.wav")
        lyrics_root = self.root / "lyrics"
        lyrics_root.mkdir()
        for index in range(40):
            (lyrics_root / f"A-甲-{index:02d}.lrc").write_text(
                "[ti:A]\n[ar:甲]",
                encoding="utf-8",
            )
        scan = LibraryScanController(self.config)
        lyrics = LyricsMatchController(self.config)
        window = MainWindow(scan_controller=scan, lyrics_match_controller=lyrics)
        self.addCleanup(window.close)
        window.show()
        self.app.processEvents()
        window._rematch_selected_lyrics((dict(scan.load_library()[0]),))
        dialog = window._lyrics_dialog
        window._start_lyrics_scan(lyrics_root)
        self.assertTrue(lyrics.running)

        window.close()
        window.close()
        self.assertTrue(window.isVisible())
        self.assertTrue(dialog.isVisible())
        self.assertTrue(window._close_pending)

        self._wait(lyrics)
        self._flush_deferred_deletes()
        self.assertFalse(window.isVisible())
        self.assertIsNone(lyrics.pending_scope)
        self.assertIsNone(window._lyrics_dialog)
        self.assertEqual(window._open_windows, [])

    def test_main_close_closes_all_regular_auxiliary_windows_idempotently(self) -> None:
        window = MainWindow()
        self.addCleanup(window.close)
        window.show()
        self.app.processEvents()
        window.open_import()
        window.open_rename()
        window.open_lyrics_match()
        window.open_history()
        window.open_settings()
        self.app.processEvents()
        tracked = tuple(window._open_windows)
        self.assertEqual(len(tracked), 5)
        self.assertTrue(all(item.isVisible() for item in tracked))

        window.close()
        window.close()
        self.assertFalse(window.isVisible())
        self.assertTrue(all(not item.isVisible() for item in tracked))
        self._flush_deferred_deletes()
        self.assertEqual(window._open_windows, [])

    def test_main_close_waits_for_last_controller_and_clears_pending_queues(self) -> None:
        class FakeRunningController:
            def __init__(self, *, cancel_error: bool = False) -> None:
                self.running = True
                self.cancel_calls = 0
                self.cancel_error = cancel_error

            def request_cancel(self) -> None:
                self.cancel_calls += 1
                if self.cancel_error:
                    raise RuntimeError("deterministic cancel failure")

        window = MainWindow()
        self.addCleanup(window.close)
        window.show()
        window.open_history()
        self.app.processEvents()
        child = window._open_windows[0]
        first = FakeRunningController(cancel_error=True)
        second = FakeRunningController()
        window._scan_controller = first
        window._lyrics_match_controller = second
        window._pending_refresh_roots.append(("audio", self.root))
        window._playlist_add_queue.append(("测试", ()))

        window.close()
        window.close()
        self.assertEqual(first.cancel_calls, 1)
        self.assertEqual(second.cancel_calls, 1)
        self.assertEqual(window._pending_refresh_roots, [])
        self.assertEqual(window._playlist_add_queue, [])
        self.assertTrue(window.isVisible())
        self.assertTrue(child.isVisible())

        first.running = False
        window._background_running_changed(False)
        self.app.processEvents()
        self.assertTrue(window.isVisible())
        self.assertTrue(child.isVisible())

        second.running = False
        window._background_running_changed(False)
        self._flush_deferred_deletes()
        self.assertFalse(window.isVisible())
        self.assertEqual(window._open_windows, [])
        self.assertEqual(window._pending_refresh_roots, [])
        self.assertEqual(window._playlist_add_queue, [])

    def test_context_lyrics_scope_is_one_shot_until_toolbar_resets_it(self) -> None:
        audio_root = self.root / "audio"
        self._seed_audio(audio_root, "A-甲.wav")
        lyrics_root = self.root / "lyrics"
        lyrics_root.mkdir()
        (lyrics_root / "A-甲.lrc").write_text("[ti:A]\n[ar:甲]", encoding="utf-8")
        scan = LibraryScanController(self.config)
        lyrics = LyricsMatchController(self.config)
        window = MainWindow(scan_controller=scan, lyrics_match_controller=lyrics)
        self.addCleanup(window.close)

        window._rematch_selected_lyrics((dict(scan.load_library()[0]),))
        self.app.processEvents()
        window._start_lyrics_scan(lyrics_root)
        self._wait(lyrics)
        self.assertTrue(window._lyrics_context_scope_active)
        self.assertIsNone(lyrics.pending_scope)

        with mock.patch.object(lyrics, "start_scan") as start:
            window._start_lyrics_scan(lyrics_root)
        start.assert_not_called()
        self.assertIn("限定范围已使用", window._lyrics_dialog.summary.text())

        window.open_lyrics_match()
        self.assertFalse(window._lyrics_context_scope_active)
        with mock.patch.object(lyrics, "start_scan") as start:
            window._start_lyrics_scan(lyrics_root)
        start.assert_called_once_with(lyrics_root)

    def test_scoped_worker_revalidates_drift_before_creating_lyric_session(self) -> None:
        audio_root = self.root / "audio"
        asset = self._seed_audio(audio_root, "A-甲.wav")
        lyrics_root = self.root / "lyrics"
        lyrics_root.mkdir()
        (lyrics_root / "A-甲.lrc").write_text("[ti:A]\n[ar:甲]", encoding="utf-8")
        scan = LibraryScanController(self.config)
        records = {record["_asset_id"]: record for record in scan.load_library()}
        validated = scan.revalidate_audio_records((self._audio_snapshot(records[asset.id]),))
        asset.canonical_path.write_bytes(b"drift")

        lyrics = LyricsMatchController(self.config)
        failures: list[str] = []
        lyrics.failed.connect(failures.append)
        lyrics.set_scope(validated)
        lyrics.start_scan(lyrics_root)
        self._wait(lyrics)
        self.assertEqual(len(failures), 1)
        with LibraryRepository(self.config) as repository:
            self.assertEqual(repository.list_assets(kind="lyric"), ())
            self.assertEqual(repository.list_lyrics_matches(), ())

    @staticmethod
    def _audio_snapshot(record: dict[str, object]) -> AudioAssetSnapshot:
        return AudioAssetSnapshot(
            record["_asset_id"],
            record["_canonical_path"],
            record["_size_bytes"],
            record["_mtime_ns"],
            record["_allowed_root"],
        )


if __name__ == "__main__":
    unittest.main()
