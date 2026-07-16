from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import time
import unittest
from unittest.mock import patch

from PySide6.QtWidgets import QApplication

from database import DatabaseConfig
from repositories import IndexBatchItem, LibraryRepository
from services.backup_manager import (
    BACKUP_HISTORY_KEY,
    BACKUP_MANIFEST_KEY,
    BackupController,
    BackupError,
    BackupInput,
    BackupWorker,
)


class BackupHistoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.media_root = self.root / "media"
        self.backup_root = self.root / "backup"
        self.media_root.mkdir()
        self.first = self.media_root / "first.mp3"
        self.second = self.media_root / "second.flac"
        self.first.write_bytes(b"first temporary payload")
        self.second.write_bytes(b"second temporary payload")
        self.original_bytes = {
            self.first: self.first.read_bytes(),
            self.second: self.second.read_bytes(),
        }
        self.config = DatabaseConfig(self.root / "library.sqlite3")
        with LibraryRepository(self.config) as repository:
            session = repository.create_scan_session(
                mode="audio", source_folder=self.media_root
            )
            indexed = repository.index_scan_batch(
                session.id,
                tuple(
                    IndexBatchItem(
                        path,
                        path.stat().st_size,
                        path.stat().st_mtime_ns,
                    )
                    for path in (self.first, self.second)
                ),
            )
            repository.complete_scan_and_reconcile(session.id)
        self.assets = {
            record.asset.canonical_path: record.asset for record in indexed
        }
        self.controller = BackupController(
            backup_root=self.backup_root,
            repository_factory=lambda: LibraryRepository(self.config),
        )

    def _input(self, path: Path) -> BackupInput:
        return BackupInput(
            self.assets[path].id,
            path,
            self.media_root,
            "audio",
        )

    def _wait_controller(self) -> None:
        deadline = time.monotonic() + 5
        while self.controller.running and time.monotonic() < deadline:
            self.app.processEvents()
        self.app.processEvents()
        self.assertFalse(self.controller.running)

    def _wait_worker(self, worker: BackupWorker) -> None:
        deadline = time.monotonic() + 5
        while worker.isRunning() and time.monotonic() < deadline:
            self.app.processEvents()
        self.app.processEvents()
        self.assertFalse(worker.isRunning())

    def _start_backup(self, *paths: Path) -> object:
        results: list[object] = []
        self.controller.completed.connect(results.append)
        self.controller.start_backup(tuple(self._input(path) for path in paths))
        self._wait_controller()
        self.assertTrue(results)
        return results[-1]

    def _raw_manifest(self) -> object:
        with LibraryRepository(self.config) as repository:
            setting = repository.get_setting(BACKUP_MANIFEST_KEY)
        return None if setting is None else setting.value

    @staticmethod
    def _raw_record(index: int) -> dict[str, object]:
        created = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(
            seconds=index
        )
        return {
            "id": f"history-{index:03d}",
            "action": "cleanup",
            "status": "completed",
            "created_at": created.isoformat(),
            "success_count": 0,
            "failure_count": 0,
            "items": [],
        }

    def test_backup_restore_and_cleanup_history_survives_manifest_changes(self) -> None:
        self._start_backup(self.first, self.second)
        entries = self.controller.list_entries()
        first_entry = next(entry for entry in entries if entry.original_path == self.first)
        second_entry = next(entry for entry in entries if entry.original_path == self.second)

        self.controller.start_restore((first_entry.id,))
        self._wait_controller()
        after_restore = self.controller.list_entries()
        self.assertEqual(tuple(entry.id for entry in after_restore), (second_entry.id,))
        self.controller.start_cleanup(retention_days=0)
        self._wait_controller()

        self.assertEqual(self.first.read_bytes(), self.original_bytes[self.first])
        self.assertFalse(second_entry.backup_path.exists())
        current_entries = self.controller.list_entries()
        self.assertEqual(current_entries, ())

        history = self.controller.list_history()
        self.assertEqual([record.action for record in history], ["cleanup", "restore", "backup"])
        backup = history[-1]
        restore = history[-2]
        cleanup = history[-3]
        self.assertEqual((backup.success_count, backup.failure_count), (2, 0))
        self.assertEqual(set(backup.restore_ids), {first_entry.id, second_entry.id})
        self.assertEqual(restore.items[0].restore_target, self.first)
        self.assertEqual(cleanup.items[0].entry_id, second_entry.id)
        self.assertEqual(cleanup.items[0].backup_path, second_entry.backup_path)
        self.assertEqual(cleanup.items[0].result, "success")
        for record in history:
            self.assertIsNotNone(datetime.fromisoformat(record.created_at).utcoffset())
            for item in record.items:
                self.assertIsNotNone(datetime.fromisoformat(item.completed_at).utcoffset())

        restarted = BackupController(
            backup_root=self.backup_root,
            repository_factory=lambda: LibraryRepository(self.config),
        )
        self.assertEqual(restarted.list_history(), history)
        self.assertEqual(restarted.list_operation_history(), history)

    def test_legacy_restored_manifest_entry_is_not_reauthorized_and_next_write_compacts_it(self) -> None:
        self._start_backup(self.first)
        raw = self._raw_manifest()
        self.assertIsInstance(raw, list)
        raw[0]["restored_at"] = datetime.now(timezone.utc).isoformat()  # type: ignore[index]
        with LibraryRepository(self.config) as repository:
            repository.set_setting(BACKUP_MANIFEST_KEY, raw)

        self.assertEqual(self.controller.list_entries(), ())
        results: list[object] = []
        self.controller.completed.connect(results.append)
        self.controller.start_restore((str(raw[0]["id"]),))  # type: ignore[index]
        self._wait_controller()
        self.assertFalse(self.first.exists())
        self.assertEqual((results[-1].success_count, results[-1].failure_count), (0, 1))

        self._start_backup(self.second)

        compacted = self._raw_manifest()
        self.assertIsInstance(compacted, list)
        self.assertEqual(len(compacted), 1)
        self.assertEqual(compacted[0]["asset_id"], self.assets[self.second].id)  # type: ignore[index]

    def test_partial_failure_is_permanent_file_level_history(self) -> None:
        self.second.unlink()

        result = self._start_backup(self.first, self.second)

        self.assertEqual((result.success_count, result.failure_count), (1, 1))
        record = self.controller.list_history()[0]
        self.assertEqual((record.status, record.success_count, record.failure_count), ("completed", 1, 1))
        self.assertEqual([item.result for item in record.items], ["success", "failed"])
        failed = record.items[1]
        self.assertEqual((failed.asset_id, failed.kind, failed.source_path), (self.assets[self.second].id, "audio", self.second))
        self.assertTrue(failed.message)

    def test_pre_cancelled_worker_records_cancelled_items_without_touching_media(self) -> None:
        results: list[object] = []
        self.backup_root.mkdir()
        worker = BackupWorker(
            action="backup",
            payload=(self._input(self.first), self._input(self.second)),
            backup_root=self.backup_root,
            repository_factory=lambda: LibraryRepository(self.config),
        )
        worker.completed.connect(results.append)
        worker.request_cancel()
        worker.start()
        self._wait_worker(worker)

        self.assertEqual(self.first.read_bytes(), self.original_bytes[self.first])
        self.assertEqual(self.second.read_bytes(), self.original_bytes[self.second])
        self.assertEqual(results[0].status, "cancelled")
        history = self.controller.list_history()
        self.assertEqual(history[0].status, "cancelled")
        self.assertEqual([item.result for item in history[0].items], ["cancelled", "cancelled"])
        self.assertEqual((history[0].success_count, history[0].failure_count), (0, 0))

    def test_history_write_failure_warns_but_does_not_undo_completed_backup(self) -> None:
        results: list[object] = []
        self.controller.completed.connect(results.append)
        original_set_setting = LibraryRepository.set_setting

        def fail_history(repository, key, value):
            if key == BACKUP_HISTORY_KEY:
                raise RuntimeError("deterministic history write failure")
            return original_set_setting(repository, key, value)

        with patch.object(LibraryRepository, "set_setting", new=fail_history):
            self.controller.start_backup((self._input(self.first),))
            self._wait_controller()

        self.assertFalse(self.first.exists())
        self.assertEqual(self.controller.list_entries()[0].backup_path.read_bytes(), self.original_bytes[self.first])
        self.assertIsNone(results[0].history_id)
        self.assertTrue(any("永久操作历史保存失败" in message for message in results[0].messages))
        self.assertEqual(self.controller.list_history(), ())

    def test_restore_history_write_failure_keeps_restored_file_and_other_manifest_entries(self) -> None:
        self._start_backup(self.first, self.second)
        entries = self.controller.list_entries()
        first_entry = next(entry for entry in entries if entry.original_path == self.first)
        second_entry = next(entry for entry in entries if entry.original_path == self.second)
        results: list[object] = []
        self.controller.completed.connect(results.append)
        original_set_setting = LibraryRepository.set_setting

        def fail_history(repository, key, value):
            if key == BACKUP_HISTORY_KEY:
                raise RuntimeError("deterministic restore history failure")
            return original_set_setting(repository, key, value)

        with patch.object(LibraryRepository, "set_setting", new=fail_history):
            self.controller.start_restore((first_entry.id,))
            self._wait_controller()

        self.assertEqual(self.first.read_bytes(), self.original_bytes[self.first])
        self.assertFalse(first_entry.backup_path.exists())
        remaining = self.controller.list_entries()
        self.assertEqual(tuple(entry.id for entry in remaining), (second_entry.id,))
        self.assertTrue(second_entry.backup_path.exists())
        self.assertTrue(any("永久操作历史保存失败" in value for value in results[-1].messages))
        self.assertEqual([record.action for record in self.controller.list_history()], ["backup"])

    def test_writer_keeps_newest_200_records_and_reader_rejects_more(self) -> None:
        with LibraryRepository(self.config) as repository:
            repository.set_setting(
                BACKUP_HISTORY_KEY,
                [self._raw_record(index) for index in range(200)],
            )

        result = self._start_backup(self.first)

        self.assertIsNotNone(result.history_id)
        history = self.controller.list_history()
        self.assertEqual(len(history), 200)
        self.assertEqual(history[0].id, result.history_id)
        self.assertNotIn("history-000", {record.id for record in history})

        with LibraryRepository(self.config) as repository:
            repository.set_setting(
                BACKUP_HISTORY_KEY,
                [self._raw_record(index) for index in range(201)],
            )
        with self.assertRaisesRegex(BackupError, "超过 200"):
            self.controller.list_history()

    def test_reader_fails_closed_for_corrupt_history(self) -> None:
        cases: tuple[tuple[str, object], ...] = (
            ("not-list", {"id": "wrong"}),
            (
                "bad-action",
                [{**self._raw_record(1), "action": "erase"}],
            ),
            (
                "naive-time",
                [{**self._raw_record(1), "created_at": "2026-01-01T00:00:00"}],
            ),
            (
                "bad-count",
                [{**self._raw_record(1), "success_count": 1}],
            ),
            (
                "relative-path",
                [
                    {
                        **self._raw_record(1),
                        "failure_count": 1,
                        "items": [
                            {
                                "entry_id": None,
                                "asset_id": self.assets[self.first].id,
                                "kind": "audio",
                                "source_path": "relative.mp3",
                                "backup_path": None,
                                "restore_target": None,
                                "result": "failed",
                                "message": "bad",
                                "completed_at": datetime.now(timezone.utc).isoformat(),
                            }
                        ],
                    }
                ],
            ),
        )
        for label, value in cases:
            with self.subTest(label=label):
                with LibraryRepository(self.config) as repository:
                    repository.set_setting(BACKUP_HISTORY_KEY, value)
                with self.assertRaises(BackupError):
                    self.controller.list_history()

    def test_list_history_uses_repository_in_calling_thread_and_is_read_only(self) -> None:
        self._start_backup(self.first)
        with LibraryRepository(self.config) as repository:
            before = repository.get_setting(BACKUP_HISTORY_KEY)
        self.assertIsNotNone(before)

        observed: list[object] = []

        def load() -> None:
            observed.append(self.controller.list_history())

        thread = threading.Thread(target=load)
        thread.start()
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(observed[0]), 1)
        with LibraryRepository(self.config) as repository:
            after = repository.get_setting(BACKUP_HISTORY_KEY)
        self.assertEqual(after, before)

    def test_fatal_manifest_error_records_failed_terminal_without_file_changes(self) -> None:
        with LibraryRepository(self.config) as repository:
            repository.set_setting(BACKUP_MANIFEST_KEY, {"corrupt": True})
        failures: list[str] = []
        self.controller.failed.connect(failures.append)

        self.controller.start_cleanup(retention_days=0)
        self._wait_controller()

        self.assertTrue(failures)
        self.assertEqual(self.first.read_bytes(), self.original_bytes[self.first])
        history = self.controller.list_history()
        self.assertEqual((history[0].action, history[0].status), ("cleanup", "failed"))
        self.assertEqual(history[0].items, ())


if __name__ == "__main__":
    unittest.main()
