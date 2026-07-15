from __future__ import annotations

import hashlib
import time
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from PySide6.QtWidgets import QApplication

from database import DatabaseConfig
from repositories import IndexBatchItem, LibraryRepository
from services.backup_manager import (
    BACKUP_MANIFEST_KEY,
    BackupController,
    BackupInput,
)


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

    def _manifest_value(
        self,
        *,
        entry_id: str,
        backup_path: Path,
        sha256: str,
    ) -> dict[str, object]:
        return {
            "id": entry_id,
            "asset_id": self.asset.id,
            "kind": "audio",
            "original_path": str(self.file),
            "backup_path": str(backup_path),
            "sha256": sha256,
            "created_at": "2000-01-01T00:00:00+00:00",
            "restored_at": None,
        }

    def _set_manifest(self, entries: list[dict[str, object]]) -> None:
        with LibraryRepository(self.config) as repository:
            repository.set_setting(BACKUP_MANIFEST_KEY, entries)

    def _raw_manifest(self) -> object:
        with LibraryRepository(self.config) as repository:
            setting = repository.get_setting(BACKUP_MANIFEST_KEY)
        return None if setting is None else setting.value

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

    def test_cleanup_rejects_manifest_path_outside_backup_root(self) -> None:
        outside = self.root / "outside" / self.file.name
        outside.parent.mkdir()
        outside.write_bytes(b"must not be deleted")
        self._set_manifest(
            [
                self._manifest_value(
                    entry_id="forged-entry",
                    backup_path=outside,
                    sha256=hashlib.sha256(outside.read_bytes()).hexdigest(),
                )
            ]
        )

        self.controller.start_cleanup(retention_days=0)
        self._wait()

        self.assertEqual(outside.read_bytes(), b"must not be deleted")
        self.assertEqual(len(self._raw_manifest()), 1)  # type: ignore[arg-type]

    def test_cleanup_rejects_same_root_directory_not_matching_entry_id(self) -> None:
        forged = self.backup_root / "other-entry" / self.file.name
        forged.parent.mkdir(parents=True)
        forged.write_bytes(b"same root but wrong entry directory")
        self._set_manifest(
            [
                self._manifest_value(
                    entry_id="claimed-entry",
                    backup_path=forged,
                    sha256=hashlib.sha256(forged.read_bytes()).hexdigest(),
                )
            ]
        )

        self.controller.start_cleanup(retention_days=0)
        self._wait()

        self.assertEqual(forged.read_bytes(), b"same root but wrong entry directory")
        self.assertEqual(len(self._raw_manifest()), 1)  # type: ignore[arg-type]

    def test_tampered_backup_is_not_moved_during_restore(self) -> None:
        self.controller.start_backup(
            (BackupInput(self.asset.id, self.file, self.media_root, "audio"),)
        )
        self._wait()
        entry = self.controller.list_entries()[0]
        entry.backup_path.write_bytes(b"tampered backup payload")

        self.controller.start_restore((entry.id,))
        self._wait()

        self.assertFalse(self.file.exists())
        self.assertEqual(entry.backup_path.read_bytes(), b"tampered backup payload")
        self.assertIsNone(self.controller.list_entries()[0].restored_at)

    def test_tampered_backup_is_not_deleted_during_cleanup(self) -> None:
        self.controller.start_backup(
            (BackupInput(self.asset.id, self.file, self.media_root, "audio"),)
        )
        self._wait()
        entry = self.controller.list_entries()[0]
        entry.backup_path.write_bytes(b"tampered backup payload")

        self.controller.start_cleanup(retention_days=0)
        self._wait()

        self.assertEqual(entry.backup_path.read_bytes(), b"tampered backup payload")
        self.assertEqual(len(self.controller.list_entries()), 1)

    def test_cleanup_removes_verified_expired_backup_and_manifest(self) -> None:
        self.controller.start_backup(
            (BackupInput(self.asset.id, self.file, self.media_root, "audio"),)
        )
        self._wait()
        entry = self.controller.list_entries()[0]

        self.controller.start_cleanup(retention_days=0)
        self._wait()

        self.assertFalse(entry.backup_path.exists())
        self.assertEqual(self.controller.list_entries(), ())

    def test_restore_manifest_save_failure_rolls_file_back_to_backup(self) -> None:
        self.controller.start_backup(
            (BackupInput(self.asset.id, self.file, self.media_root, "audio"),)
        )
        self._wait()
        entry = self.controller.list_entries()[0]

        original_set_setting = LibraryRepository.set_setting

        def fail_manifest_save(repository, key, value):
            if key == BACKUP_MANIFEST_KEY:
                raise RuntimeError("deterministic manifest failure")
            return original_set_setting(repository, key, value)

        with patch.object(LibraryRepository, "set_setting", new=fail_manifest_save):
            self.controller.start_restore((entry.id,))
            self._wait()

        self.assertFalse(self.file.exists())
        self.assertEqual(entry.backup_path.read_bytes(), self.payload)
        self.assertIsNone(self.controller.list_entries()[0].restored_at)

    def test_cleanup_manifest_save_failure_keeps_file_and_manifest(self) -> None:
        self.controller.start_backup(
            (BackupInput(self.asset.id, self.file, self.media_root, "audio"),)
        )
        self._wait()
        entry = self.controller.list_entries()[0]

        original_set_setting = LibraryRepository.set_setting

        def fail_manifest_save(repository, key, value):
            if key == BACKUP_MANIFEST_KEY:
                raise RuntimeError("deterministic manifest failure")
            return original_set_setting(repository, key, value)

        with patch.object(LibraryRepository, "set_setting", new=fail_manifest_save):
            self.controller.start_cleanup(retention_days=0)
            self._wait()

        self.assertEqual(entry.backup_path.read_bytes(), self.payload)
        self.assertEqual(len(self.controller.list_entries()), 1)

    def test_retention_setting_round_trip(self) -> None:
        self.assertEqual(self.controller.retention_days(), 7)
        self.controller.set_retention_days(30)
        self.assertEqual(self.controller.retention_days(), 30)
        self.controller.set_retention_days(None)
        self.assertIsNone(self.controller.retention_days())


if __name__ == "__main__":
    unittest.main()
