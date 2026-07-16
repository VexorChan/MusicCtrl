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
    BackupError,
    BackupInput,
    BackupWorker,
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
        results: list[object] = []
        self.controller.completed.connect(results.append)
        self.controller.start_backup(
            (BackupInput(self.asset.id, self.file, self.media_root, "audio"),)
        )
        self._wait()
        self.assertFalse(self.file.exists())
        entries = self.controller.list_entries()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].backup_path.read_bytes(), self.payload)
        self.assertEqual(entries[0].allowed_root, self.media_root)
        self.assertEqual(results[-1].affected_roots, (("audio", self.media_root),))

        self.controller.start_restore((entries[0].id,))
        self._wait()
        self.assertEqual(self.file.read_bytes(), self.payload)
        self.assertFalse(entries[0].backup_path.exists())
        self.assertIsNotNone(self.controller.list_entries()[0].restored_at)
        self.assertEqual(results[-1].action, "restore")
        self.assertEqual(results[-1].affected_roots, (("audio", self.media_root),))

    def test_successful_roots_are_stably_deduplicated(self) -> None:
        second = self.media_root / "second.mp3"
        second.write_bytes(b"second")
        other_root = self.root / "other-media"
        other_root.mkdir()
        third = other_root / "third.flac"
        third.write_bytes(b"third")
        with LibraryRepository(self.config) as repository:
            session = repository.create_scan_session(mode="audio", source_folder=self.media_root)
            records = repository.index_scan_batch(
                session.id,
                tuple(
                    IndexBatchItem(path, path.stat().st_size, path.stat().st_mtime_ns)
                    for path in (self.file, second)
                ),
            )
            repository.complete_scan_and_reconcile(session.id)
            other_session = repository.create_scan_session(mode="audio", source_folder=other_root)
            third_asset = repository.index_scan_batch(
                other_session.id,
                (IndexBatchItem(third, third.stat().st_size, third.stat().st_mtime_ns),),
            )[0].asset
            repository.complete_scan_and_reconcile(other_session.id)
        by_path = {record.asset.canonical_path: record.asset for record in records}
        results: list[object] = []
        self.controller.completed.connect(results.append)

        self.controller.start_backup(
            (
                BackupInput(by_path[self.file].id, self.file, self.media_root, "audio"),
                BackupInput(by_path[second].id, second, self.media_root, "audio"),
                BackupInput(third_asset.id, third, other_root, "audio"),
            )
        )
        self._wait()

        self.assertEqual(results[-1].success_count, 3)
        self.assertEqual(
            results[-1].affected_roots,
            (("audio", self.media_root), ("audio", other_root)),
        )

    def test_legacy_manifest_without_allowed_root_still_loads(self) -> None:
        self.controller.start_backup(
            (BackupInput(self.asset.id, self.file, self.media_root, "audio"),)
        )
        self._wait()
        manifest = self._raw_manifest()
        self.assertIsInstance(manifest, list)
        del manifest[0]["allowed_root"]  # type: ignore[index]
        self._set_manifest(manifest)  # type: ignore[arg-type]

        entries = self.controller.list_entries()

        self.assertEqual(len(entries), 1)
        self.assertIsNone(entries[0].allowed_root)

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
        results: list[object] = []
        self.controller.completed.connect(results.append)
        self.controller.start_backup(
            (BackupInput(self.asset.id, self.file, self.media_root, "audio"),)
        )
        self._wait()
        entry = self.controller.list_entries()[0]

        self.controller.start_cleanup(retention_days=0)
        self._wait()

        self.assertFalse(entry.backup_path.exists())
        self.assertEqual(self.controller.list_entries(), ())
        self.assertEqual(results[-1].action, "cleanup")
        self.assertEqual(results[-1].affected_roots, ())

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

    def test_backup_root_is_read_only_and_prepare_creates_valid_directory(self) -> None:
        self.assertEqual(self.controller.backup_root, self.backup_root)
        self.assertFalse(self.backup_root.exists())

        prepared = self.controller.prepare_backup_root()

        self.assertEqual(prepared, self.backup_root)
        self.assertTrue(prepared.is_dir())

    def test_prepare_backup_root_fails_closed_when_directory_validation_rejects(self) -> None:
        with patch.object(
            BackupWorker,
            "_validate_backup_root_path",
            side_effect=BackupError("deterministic reparse rejection"),
        ):
            with self.assertRaisesRegex(BackupError, "reparse rejection"):
                self.controller.prepare_backup_root()

    def test_cleanup_preview_reports_real_retention_root_and_eligible_count(self) -> None:
        initial = self.controller.cleanup_preview()
        self.assertEqual(
            (initial.backup_root, initial.retention_days, initial.eligible_count),
            (self.backup_root, 7, 0),
        )
        self.controller.start_backup(
            (BackupInput(self.asset.id, self.file, self.media_root, "audio"),)
        )
        self._wait()
        manifest = self._raw_manifest()
        self.assertIsInstance(manifest, list)
        manifest[0]["created_at"] = "2000-01-01T00:00:00+00:00"  # type: ignore[index]
        self._set_manifest(manifest)  # type: ignore[arg-type]

        preview = self.controller.cleanup_preview()

        self.assertEqual(
            (preview.backup_root, preview.retention_days, preview.eligible_count),
            (self.backup_root, 7, 1),
        )
        self.controller.set_retention_days(None)
        permanent = self.controller.cleanup_preview()
        self.assertEqual((permanent.retention_days, permanent.eligible_count), (None, 0))

    def test_cleanup_preview_rejects_invalid_manifest_timestamp(self) -> None:
        self.controller.start_backup(
            (BackupInput(self.asset.id, self.file, self.media_root, "audio"),)
        )
        self._wait()
        manifest = self._raw_manifest()
        self.assertIsInstance(manifest, list)
        manifest[0]["created_at"] = "not-a-timestamp"  # type: ignore[index]
        self._set_manifest(manifest)  # type: ignore[arg-type]

        with self.assertRaisesRegex(BackupError, "创建时间"):
            self.controller.cleanup_preview()


if __name__ == "__main__":
    unittest.main()
