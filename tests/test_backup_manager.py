from __future__ import annotations

import time
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from PySide6.QtWidgets import QApplication

from database import DatabaseConfig
from repositories import IndexBatchItem, LibraryRepository
from services.backup_manager import BackupController, BackupInput


class BackupManagerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.media_root = self.root / "media"
        self.backup_root = self.root / "backups"
        self.media_root.mkdir()
        self.file = self.media_root / "song.mp3"
        self.file.write_bytes(b"temporary backup audio")
        self.payload = self.file.read_bytes()
        self.config = DatabaseConfig(self.root / "library.sqlite3")
        with LibraryRepository(self.config) as repository:
            session = repository.create_scan_session(mode="audio", source_folder=self.media_root)
            self.asset = repository.index_scan_batch(
                session.id,
                (IndexBatchItem(self.file, self.file.stat().st_size, self.file.stat().st_mtime_ns),),
            )[0].asset
            repository.complete_scan_and_reconcile(session.id)
        self.controller = BackupController(
            backup_root=self.backup_root,
            repository_factory=lambda: LibraryRepository(self.config),
        )

    def _wait(self) -> None:
        deadline = time.monotonic() + 5
        while self.controller.running and time.monotonic() < deadline:
            self.app.processEvents()
        self.app.processEvents()
        self.assertFalse(self.controller.running)

    def test_backup_and_restore_roundtrip_never_overwrite(self) -> None:
        self.controller.start_backup(
            (BackupInput(self.asset.id, self.file, self.media_root, "audio"),)
        )
        self._wait()
        self.assertFalse(self.file.exists())
        entries = self.controller.list_entries()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].backup_path.read_bytes(), self.payload)

        self.controller.start_restore((entries[0].id,))
        self._wait()
        self.assertEqual(self.file.read_bytes(), self.payload)
        self.assertFalse(entries[0].backup_path.exists())
        self.assertIsNotNone(self.controller.list_entries()[0].restored_at)

    def test_restore_conflict_keeps_backup_and_existing_file(self) -> None:
        self.controller.start_backup(
            (BackupInput(self.asset.id, self.file, self.media_root, "audio"),)
        )
        self._wait()
        entry = self.controller.list_entries()[0]
        self.file.write_bytes(b"external")
        self.controller.start_restore((entry.id,))
        self._wait()
        self.assertEqual(self.file.read_bytes(), b"external")
        self.assertTrue(entry.backup_path.exists())


if __name__ == "__main__":
    unittest.main()
