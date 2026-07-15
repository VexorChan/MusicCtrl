from __future__ import annotations

import os
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest

from PySide6.QtWidgets import QApplication

from database import DatabaseConfig
from repositories import IndexBatchItem, LibraryRepository
from services.playlist_controller import (
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


if __name__ == "__main__":
    unittest.main()
