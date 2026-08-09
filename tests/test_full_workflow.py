from __future__ import annotations

import os
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest
import wave

from PySide6.QtWidgets import QApplication

from database import DatabaseConfig
from repositories import LibraryRepository
from services.backup_manager import BackupController, BackupInput
from services.history_service import HistoryService
from services.library_scan_controller import LibraryScanController
from services.lyrics_match_controller import LyricsMatchController
from services.playlist_controller import PlaylistAudioInput, PlaylistController
from services.safe_import import SafeImportController
from services.safe_rename import SafeRenameController, SafeRenameInput


@unittest.skipUnless(os.name == "nt", "完整流程包含 Windows .lnk")
class FullWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.database_config = DatabaseConfig(self.root / "library.sqlite3")

    def wait(self, controller: object) -> None:
        deadline = time.monotonic() + 8
        while bool(getattr(controller, "running")) and time.monotonic() < deadline:
            self.app.processEvents()
        self.app.processEvents()
        self.assertFalse(bool(getattr(controller, "running")), type(controller).__name__)

    def test_scan_rename_lyrics_playlist_import_backup_history_and_undo(self) -> None:
        media = self.root / "media"
        lyrics_root = self.root / "lyrics"
        playlists = self.root / "playlists"
        import_source = self.root / "incoming"
        import_target = self.root / "imported"
        backup_root = self.root / "backups"
        for directory in (media, lyrics_root, playlists, import_source, import_target):
            directory.mkdir()

        original = media / "旧名-歌手.wav"
        with wave.open(str(original), "wb") as stream:
            stream.setnchannels(1)
            stream.setsampwidth(2)
            stream.setframerate(8000)
            stream.writeframes(b"\0\0" * 80)
        scan = LibraryScanController(self.database_config)
        scan.start_scan(media)
        self.wait(scan)
        with LibraryRepository(self.database_config) as repository:
            asset = repository.list_assets(kind="audio")[0]

        renamed = media / "晴天-周杰伦.wav"
        rename = SafeRenameController(lambda: LibraryRepository(self.database_config))
        rename.start(
            (
                SafeRenameInput(
                    asset.id,
                    original,
                    renamed,
                    media,
                    original.stat().st_size,
                    original.stat().st_mtime_ns,
                ),
            )
        )
        self.wait(rename)
        self.assertTrue(renamed.is_file())
        scan.start_scan(media)
        self.wait(scan)

        lyric = lyrics_root / "晴天-周杰伦.lrc"
        lyric.write_text("[ti:晴天]\n[ar:周杰伦]\n[00:01.00]临时歌词", encoding="utf-8")
        lyrics = LyricsMatchController(self.database_config)
        lyrics.start_scan(lyrics_root)
        self.wait(lyrics)
        review = lyrics.review_snapshot()
        self.assertIsNotNone(review)
        manual = tuple(
            item.token
            for item in review.items
            if item.lyric_asset_id is not None and item.requires_confirmation
        )
        if manual:
            lyrics.commit_candidates(manual)
            self.wait(lyrics)
        with LibraryRepository(self.database_config) as repository:
            renamed_asset = repository.get_asset_by_id(asset.id)
            self.assertEqual(renamed_asset.canonical_path, renamed)
            self.assertEqual(
                len(repository.list_lyrics_matches(current_only=True)),
                1,
                repr(review.items),
            )
        music_record = scan.load_library()[0]
        self.assertEqual(music_record["status"], "已匹配")
        self.assertEqual(music_record["file_status"], "正常")
        self.assertEqual(lyrics.load_lyrics_library()[0]["file_status"], "正常")

        playlist = PlaylistController(self.database_config)
        playlist.set_root(playlists)
        playlist.create_playlist("通勤")
        playlist.start_add(
            ("通勤"),
            (PlaylistAudioInput(asset.id, renamed, media, "active"),),
        )
        self.wait(playlist)
        playlist_rows = playlist.load_playlist("通勤")
        self.assertEqual(len(playlist_rows), 1)
        self.assertNotIn("status", playlist_rows[0])
        self.assertNotIn("format", playlist_rows[0])
        self.assertNotIn("size", playlist_rows[0])
        self.assertEqual(playlist_rows[0]["file_status"], "正常")

        incoming = import_source / "新歌-歌手.mp3"
        incoming.write_bytes(b"temporary imported audio")
        safe_import = SafeImportController(
            lambda: LibraryRepository(self.database_config)
        )
        safe_import.start_preview(import_source, import_target, "audio")
        self.wait(safe_import)
        self.assertIsNotNone(safe_import.current_plan)
        safe_import.start_execute(safe_import.current_plan.id)
        self.wait(safe_import)
        imported = import_target / incoming.name
        self.assertTrue(imported.is_file())

        backup = BackupController(
            backup_root=backup_root,
            repository_factory=lambda: LibraryRepository(self.database_config),
        )
        backup.start_backup((BackupInput(asset.id, renamed, media, "audio"),))
        self.wait(backup)
        entry = backup.list_entries()[0]
        self.assertFalse(renamed.exists())
        backup.start_restore((entry.id,))
        self.wait(backup)
        self.assertTrue(renamed.is_file())

        history = HistoryService(
            import_controller=safe_import,
            rename_controller=rename,
            backup_controller=backup,
            playlist_controller=playlist,
            lyrics_controller=lyrics,
        ).load()
        self.assertEqual(
            {record.category for record in history.records},
            {"import", "rename", "delete", "playlist", "lyrics"},
        )
        self.assertTrue(any(record.undoable for record in history.records))

        safe_import.undo_last_complete()
        self.wait(safe_import)
        self.assertTrue(incoming.is_file())
        self.assertFalse(imported.exists())


if __name__ == "__main__":
    unittest.main()
