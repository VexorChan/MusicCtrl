from __future__ import annotations

import os
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import threading
import unittest
from unittest.mock import patch

from PySide6.QtWidgets import QApplication

from database import DatabaseConfig
from repositories import IndexBatchItem, LibraryRepository
import services.playlist_controller as playlist_module
from services.playlist_controller import (
    PLAYLIST_HISTORY_KEY,
    PlaylistAudioInput,
    PlaylistController,
    PlaylistRemovalInput,
    PlaylistRetargetInput,
)
from ui.main_window import MainWindow


@unittest.skipUnless(os.name == "nt", "Windows .lnk integration requires Windows")
class PlaylistControllerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.audio_root = self.root / "audio"
        self.playlist_root = self.root / "playlists"
        self.audio_root.mkdir()
        self.playlist_root.mkdir()
        self.audio = self.audio_root / "晴天-周杰伦.mp3"
        self.audio.write_bytes(b"temporary playlist fixture")
        self.config = DatabaseConfig(self.root / "library.sqlite3")
        with LibraryRepository(self.config) as repository:
            session = repository.create_scan_session(mode="audio", source_folder=self.audio_root)
            batch = repository.index_scan_batch(
                session.id,
                (IndexBatchItem(self.audio, self.audio.stat().st_size, self.audio.stat().st_mtime_ns),),
            )[0]
            repository.complete_scan_and_reconcile(session.id)
        self.asset = batch.asset
        self.controller = PlaylistController(self.config)
        self.controller.set_root(self.playlist_root)
        self.controller.create_playlist("通勤")

    def _wait(self) -> None:
        deadline = time.monotonic() + 5
        while self.controller.running and time.monotonic() < deadline:
            self.app.processEvents()
        self.app.processEvents()
        self.assertFalse(self.controller.running, "playlist worker did not finish")

    def test_add_load_and_remove_shortcut_preserve_audio(self) -> None:
        snapshot = (self.audio.read_bytes(), self.audio.stat().st_mtime_ns)
        self.controller.start_add(
            "通勤",
            (PlaylistAudioInput(self.asset.id, self.audio, self.audio_root, "active"),),
        )
        self._wait()

        rows = self.controller.load_playlist("通勤")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["title"], "晴天")
        shortcut = rows[0]["_shortcut_path"]
        self.assertTrue(shortcut.is_file())

        self.controller.start_remove(
            "通勤",
            (PlaylistRemovalInput(shortcut, self.audio),),
        )
        self._wait()
        self.assertEqual(self.controller.load_playlist("通勤"), ())
        self.assertEqual((self.audio.read_bytes(), self.audio.stat().st_mtime_ns), snapshot)

    def test_duplicate_is_skipped_without_overwrite(self) -> None:
        results = []
        self.controller.completed.connect(results.append)
        item = PlaylistAudioInput(self.asset.id, self.audio, self.audio_root, "active")
        self.controller.start_add("通勤", (item,))
        self._wait()
        shortcut = self.controller.load_playlist("通勤")[0]["_shortcut_path"]
        before = shortcut.read_bytes()
        self.controller.start_add("通勤", (item,))
        self._wait()
        self.assertEqual(results[-1].skipped_count, 1)
        self.assertEqual(shortcut.read_bytes(), before)
        history = self.controller.list_history()
        self.assertEqual(history[0].status, "completed")
        self.assertEqual(history[0].items[0].result, "skipped")

    def test_retarget_updates_managed_shortcut_after_audio_rename(self) -> None:
        item = PlaylistAudioInput(self.asset.id, self.audio, self.audio_root, "active")
        self.controller.start_add("通勤", (item,))
        self._wait()
        old_shortcut = self.controller.load_playlist("通勤")[0]["_shortcut_path"]
        renamed = self.audio_root / "晴天现场版-周杰伦.mp3"
        os.rename(self.audio, renamed)

        self.controller.start_retarget(
            (PlaylistRetargetInput(self.audio, renamed, self.audio_root),)
        )
        self._wait()

        rows = self.controller.load_playlist("通勤")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["_target_path"], renamed)
        self.assertFalse(old_shortcut.exists())
        self.assertTrue(rows[0]["_shortcut_path"].is_file())

    def test_main_window_uses_dynamic_playlists_and_mock_fallback(self) -> None:
        window = MainWindow(playlist_controller=self.controller)
        try:
            self.assertIn("playlist:通勤", window.pages)
            self.assertNotIn("playlist:我喜欢的", window.pages)
        finally:
            window.close()
        mock = MainWindow()
        try:
            self.assertIn("playlist:我喜欢的", mock.pages)
        finally:
            mock.close()

    def test_missing_provenance_and_non_active_input_fail_per_item(self) -> None:
        results = []
        self.controller.completed.connect(results.append)
        self.controller.start_add(
            "通勤",
            (PlaylistAudioInput(self.asset.id, self.audio, self.audio_root, "missing"),),
        )
        self._wait()
        self.assertEqual(results[-1].failure_count, 1)
        self.assertEqual(self.controller.load_playlist("通勤"), ())
        history = self.controller.list_history()
        self.assertEqual(history[0].status, "completed")
        self.assertEqual(history[0].items[0].result, "failed")

    def test_cancelled_and_failed_operations_are_persisted_with_terminal_status(self) -> None:
        item = PlaylistAudioInput(self.asset.id, self.audio, self.audio_root, "active")
        create_shortcut = playlist_module.create_shortcut

        def create_then_cancel(**kwargs):
            result = create_shortcut(**kwargs)
            self.controller.request_cancel()
            return result

        cancelled_results = []
        self.controller.cancelled.connect(cancelled_results.append)
        with patch.object(playlist_module, "create_shortcut", side_effect=create_then_cancel):
            self.controller.start_add("通勤", (item, item))
            self._wait()

        self.assertEqual(len(cancelled_results), 1)
        history = self.controller.list_history()
        self.assertEqual(history[0].status, "cancelled")
        self.assertEqual([detail.result for detail in history[0].items], ["success", "cancelled"])

        failed_messages = []
        self.controller.failed.connect(failed_messages.append)
        self.controller.start_remove(
            "不存在的歌单",
            (PlaylistRemovalInput(self.playlist_root / "missing.lnk", self.audio),),
        )
        self._wait()
        self.assertEqual(len(failed_messages), 1)
        history = self.controller.list_history()
        self.assertEqual(history[0].action, "remove")
        self.assertEqual(history[0].status, "failed")
        self.assertEqual(history[0].failure_count, 1)
        self.assertEqual(len(history[0].items), 1)
        self.assertEqual(history[0].items[0].source_path, self.playlist_root / "missing.lnk")
        self.assertEqual(history[0].items[0].target_path, self.audio)
        self.assertEqual(history[0].items[0].result, "failed")
        self.assertIn("不存在", history[0].items[0].message)

    def test_create_add_remove_and_retarget_history_persists_across_restart(self) -> None:
        item = PlaylistAudioInput(self.asset.id, self.audio, self.audio_root, "active")
        self.controller.start_add("通勤", (item,))
        self._wait()
        shortcut = self.controller.load_playlist("通勤")[0]["_shortcut_path"]
        self.controller.start_remove(
            "通勤",
            (PlaylistRemovalInput(shortcut, self.audio),),
        )
        self._wait()
        self.controller.start_add("通勤", (item,))
        self._wait()
        renamed = self.audio_root / "晴天新版-周杰伦.mp3"
        os.rename(self.audio, renamed)
        self.controller.start_retarget(
            (PlaylistRetargetInput(self.audio, renamed, self.audio_root),)
        )
        self._wait()

        reopened = PlaylistController(self.config)
        history = reopened.list_history()
        self.assertEqual(
            {record.action for record in history},
            {"create", "add", "remove", "retarget"},
        )
        self.assertEqual(history[0].action, "retarget")
        self.assertTrue(all(record.created_at for record in history))
        self.assertTrue(all(record.status == "completed" for record in history))
        self.assertTrue(all(record.items for record in history))
        self.assertEqual(history[0].items[0].source_path, self.audio)
        self.assertEqual(history[0].items[0].target_path, renamed)

    def test_history_is_strict_capped_and_rejects_cross_thread_reads(self) -> None:
        for index in range(205):
            self.controller.create_playlist(f"历史{index:03d}")
        history = self.controller.list_history()
        self.assertEqual(len(history), 200)
        self.assertEqual(history[0].playlist_name, "历史204")
        self.assertNotIn("通勤", {record.playlist_name for record in history})

        errors: list[type[BaseException]] = []

        def cross_thread() -> None:
            try:
                self.controller.list_history()
            except BaseException as error:
                errors.append(type(error))

        thread = threading.Thread(target=cross_thread)
        thread.start()
        thread.join(timeout=5)
        self.assertEqual(errors, [RuntimeError])

        with LibraryRepository(self.config) as repository:
            repository.set_setting(PLAYLIST_HISTORY_KEY, [{"action": "forged"}])
        with self.assertRaisesRegex(ValueError, "历史"):
            PlaylistController(self.config).list_history()

        with LibraryRepository(self.config) as repository:
            repository.set_setting(
                PLAYLIST_HISTORY_KEY,
                [
                    {
                        "playlist_name": "伪造",
                        "success_count": 2,
                        "skipped_count": 0,
                        "failure_count": 0,
                        "messages": [],
                        "affected_playlists": ["伪造"],
                        "action": "create",
                        "status": "completed",
                        "created_at": "2026-07-16T00:00:00+00:00",
                        "items": [
                            {
                                "source_path": None,
                                "target_path": str(self.playlist_root / "伪造"),
                                "result": "success",
                                "message": "伪造",
                            }
                        ],
                    }
                ],
            )
        with self.assertRaisesRegex(ValueError, "计数"):
            PlaylistController(self.config).list_history()

        valid_empty = {
            "playlist_name": "空操作",
            "success_count": 0,
            "skipped_count": 0,
            "failure_count": 0,
            "messages": [],
            "affected_playlists": [],
            "action": "create",
            "status": "completed",
            "created_at": "2026-07-16T00:00:00+00:00",
            "items": [],
        }
        with LibraryRepository(self.config) as repository:
            repository.set_setting(PLAYLIST_HISTORY_KEY, [valid_empty] * 201)
        with self.assertRaisesRegex(ValueError, "超过 200"):
            PlaylistController(self.config).list_history()

        cancelled_without_cancelled_item = {
            **valid_empty,
            "playlist_name": "伪造取消",
            "success_count": 1,
            "status": "cancelled",
            "items": [
                {
                    "source_path": str(self.audio),
                    "target_path": str(self.playlist_root / "通勤" / "晴天-周杰伦.mp3.lnk"),
                    "result": "success",
                    "message": "已完成",
                }
            ],
        }
        with LibraryRepository(self.config) as repository:
            repository.set_setting(PLAYLIST_HISTORY_KEY, [cancelled_without_cancelled_item])
        with self.assertRaisesRegex(ValueError, "取消历史"):
            PlaylistController(self.config).list_history()

    def test_history_status_matches_worker_item_results(self) -> None:
        item = PlaylistAudioInput(self.asset.id, self.audio, self.audio_root, "active")
        failed_results = []
        self.controller.completed.connect(failed_results.append)
        with patch.object(playlist_module, "create_shortcut", side_effect=RuntimeError("写入失败")):
            self.controller.start_add("通勤", (item,))
            self._wait()
        self.assertEqual(failed_results[-1].status, "completed")
        self.assertEqual(self.controller.list_history()[0].status, "completed")
        self.assertEqual(self.controller.list_history()[0].items[0].result, "failed")

        original_create = playlist_module.create_shortcut
        calls = 0

        def fail_then_cancel(**kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                self.controller.request_cancel()
                raise RuntimeError("首项失败")
            return original_create(**kwargs)

        cancelled_results = []
        self.controller.cancelled.connect(cancelled_results.append)
        with patch.object(playlist_module, "create_shortcut", side_effect=fail_then_cancel):
            self.controller.start_add("通勤", (item, item))
            self._wait()
        self.assertEqual(cancelled_results[-1].status, "cancelled")
        history = self.controller.list_history()[0]
        self.assertEqual(history.status, "cancelled")
        self.assertEqual([detail.result for detail in history.items], ["failed", "cancelled"])


if __name__ == "__main__":
    unittest.main()
