from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
import wave

from PySide6.QtCore import QCoreApplication, QEventLoop, QTimer
from PySide6.QtWidgets import QApplication

from database import DatabaseConfig
from dialogs.lyrics_match_dialog import LyricsMatchDialog
from main import build_app
from repositories import IndexBatchItem, LibraryRepository
from services.lyrics_match_controller import LyricsMatchController
from ui.main_window import MainWindow


class P4IntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = build_app()

    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.audio_root = self.root / "audio"
        self.lyrics_root = self.root / "lyrics"
        self.audio_root.mkdir()
        self.lyrics_root.mkdir()
        self.config = DatabaseConfig(self.root / "library.sqlite3")

    def seed(self) -> None:
        audio = self.audio_root / "晴天-周杰伦.wav"
        with wave.open(str(audio), "wb") as stream:
            stream.setnchannels(1)
            stream.setsampwidth(2)
            stream.setframerate(8000)
            stream.writeframes(b"\0\0" * 80)
        metadata = audio.stat()
        with LibraryRepository(self.config) as repository:
            session = repository.create_scan_session(mode="audio", source_folder=self.audio_root)
            repository.index_scan_batch(
                session.id,
                (IndexBatchItem(audio, metadata.st_size, metadata.st_mtime_ns),),
            )
            repository.finish_scan_session(session.id, status="completed")
        (self.lyrics_root / "晴天-周杰伦.lrc").write_text(
            "[ti:晴天]\n[ar:周杰伦]\n[00:01.00]歌词", encoding="utf-8"
        )

    def wait_until(self, predicate, timeout_ms: int = 5000) -> None:
        if predicate():
            return
        loop = QEventLoop()
        poll = QTimer()
        poll.setInterval(0)
        poll.timeout.connect(lambda: loop.quit() if predicate() else None)
        timeout = QTimer()
        timeout.setSingleShot(True)
        expired: list[bool] = []
        timeout.timeout.connect(lambda: (expired.append(True), loop.quit()))
        poll.start()
        timeout.start(timeout_ms)
        loop.exec()
        poll.stop()
        timeout.stop()
        self.assertFalse(expired)

    def test_open_live_dialog_does_not_scan_until_explicit_start_then_refreshes_page(self) -> None:
        self.seed()
        controller = LyricsMatchController(self.config)
        window = MainWindow(lyrics_match_controller=controller)
        self.addCleanup(window.close)
        window.open_lyrics_match()
        QApplication.processEvents()

        self.assertIsInstance(window._lyrics_dialog, LyricsMatchDialog)
        self.assertTrue(window._lyrics_dialog.live_mode)
        self.assertFalse(controller.running)
        with LibraryRepository(self.config) as repository:
            self.assertEqual(repository.list_assets(kind="lyric"), ())

        window._lyrics_dialog.path_input.setText(str(self.lyrics_root))
        window._lyrics_dialog._start_live_scan()
        self.wait_until(lambda: not controller.running)
        QApplication.processEvents()

        self.assertEqual(len(window.pages["所有歌词"].all_data), 1)
        self.assertEqual(window.pages["所有歌词"].all_data[0]["status"], "已匹配")
        self.assertIn("已索引 1 个 LRC", window._lyrics_dialog.summary.text())

    def test_no_p4_controller_keeps_m1_mock_dialog(self) -> None:
        window = MainWindow()
        self.addCleanup(window.close)
        window.open_lyrics_match()
        QApplication.processEvents()
        dialog = next(item for item in window._open_windows if isinstance(item, LyricsMatchDialog))
        self.assertFalse(dialog.live_mode)

    def test_ui_layers_do_not_import_repository_database_or_sqlite(self) -> None:
        for relative in ("dialogs/lyrics_match_dialog.py", "ui/main_window.py"):
            text = Path(relative).read_text(encoding="utf-8")
            self.assertNotIn("import sqlite3", text)
            self.assertNotIn("from repositories", text)
            self.assertNotIn("from database", text)


if __name__ == "__main__":
    unittest.main()
