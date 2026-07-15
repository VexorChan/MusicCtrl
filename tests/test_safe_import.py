from __future__ import annotations

import hashlib
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import unittest

from PySide6.QtWidgets import QApplication
from database import DatabaseConfig
from repositories import LibraryRepository

from services.safe_import import (
    SafeImportError,
    cleanup_stale_candidates,
    enumerate_import_files,
    import_one,
    SafeImportController,
)


class SafeImportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.source = self.root / "source"
        self.target = self.root / "target"
        self.source.mkdir()
        self.target.mkdir()

    def test_success_uses_verified_move_and_removes_source(self) -> None:
        source = self.source / "晴天-周杰伦.mp3"
        payload = os.urandom(1024 * 1024 + 13)
        source.write_bytes(payload)
        result = import_one(source, source_root=self.source, target_root=self.target)
        self.assertEqual(result.status, "success")
        self.assertFalse(source.exists())
        self.assertEqual(result.target_path.read_bytes(), payload)
        self.assertEqual(result.sha256, hashlib.sha256(payload).hexdigest())
        self.assertEqual(tuple(self.target.glob(".musicctrl-import-*.tmp")), ())

    def test_duplicate_and_conflict_never_delete_or_overwrite_source(self) -> None:
        duplicate = self.source / "same.mp3"
        duplicate.write_bytes(b"same")
        (self.target / duplicate.name).write_bytes(b"same")
        self.assertEqual(
            import_one(duplicate, source_root=self.source, target_root=self.target).status,
            "duplicate",
        )
        self.assertTrue(duplicate.exists())

        conflict = self.source / "conflict.mp3"
        conflict.write_bytes(b"source")
        target = self.target / conflict.name
        target.write_bytes(b"target")
        before = target.read_bytes()
        self.assertEqual(
            import_one(conflict, source_root=self.source, target_root=self.target).status,
            "conflict",
        )
        self.assertEqual(target.read_bytes(), before)
        self.assertTrue(conflict.exists())

    def test_cancel_during_copy_keeps_source_and_cleans_candidate(self) -> None:
        source = self.source / "large.flac"
        source.write_bytes(os.urandom(2 * 1024 * 1024))

        class CancellingEvent:
            calls = 0

            def is_set(inner_self) -> bool:
                inner_self.calls += 1
                return inner_self.calls >= 2

        with self.assertRaises(InterruptedError):
            import_one(
                source,
                source_root=self.source,
                target_root=self.target,
                cancel_event=CancellingEvent(),  # type: ignore[arg-type]
            )
        self.assertTrue(source.exists())
        self.assertFalse((self.target / source.name).exists())
        self.assertEqual(tuple(self.target.glob(".musicctrl-import-*.tmp")), ())

    def test_root_boundaries_extensions_and_stale_cleanup(self) -> None:
        (self.source / "a.MP3").write_bytes(b"a")
        (self.source / "b.lrc").write_bytes(b"b")
        (self.source / "c.txt").write_bytes(b"c")
        self.assertEqual([path.name for path in enumerate_import_files(self.source, mode="audio")], ["a.MP3"])
        self.assertEqual([path.name for path in enumerate_import_files(self.source, mode="lyrics")], ["b.lrc"])
        with self.assertRaises(SafeImportError):
            import_one(self.source / "a.MP3", source_root=self.source, target_root=self.source)
        stale = self.target / ".musicctrl-import-old.tmp"
        stale.write_bytes(b"stale")
        sentinel = self.target / "keep.tmp"
        sentinel.write_bytes(b"keep")
        self.assertEqual(cleanup_stale_candidates(self.target, min_age_seconds=0), 1)
        self.assertTrue(sentinel.exists())

    def test_controller_runs_in_background_and_emits_single_terminal(self) -> None:
        (self.source / "a.mp3").write_bytes(b"audio")
        controller = SafeImportController()
        completed = []
        failed = []
        controller.completed.connect(completed.append)
        controller.failed.connect(failed.append)
        controller.start(self.source, self.target, "audio")
        import time

        deadline = time.monotonic() + 5
        while controller.running and time.monotonic() < deadline:
            self.app.processEvents()
        self.app.processEvents()
        self.assertFalse(controller.running)
        self.assertEqual(len(completed), 1)
        self.assertEqual(failed, [])
        self.assertEqual(completed[0].success_count, 1)

    def test_complete_import_is_persisted_and_can_be_undone(self) -> None:
        source = self.source / "undo.mp3"
        source.write_bytes(b"undo payload")
        config = DatabaseConfig(self.root / "history.sqlite3")
        controller = SafeImportController(lambda: LibraryRepository(config))
        completed = []
        controller.completed.connect(completed.append)

        def wait() -> None:
            import time
            deadline = time.monotonic() + 5
            while controller.running and time.monotonic() < deadline:
                self.app.processEvents()
            self.app.processEvents()
            self.assertFalse(controller.running)

        controller.start(self.source, self.target, "audio")
        wait()
        self.assertFalse(source.exists())
        self.assertTrue((self.target / source.name).exists())
        self.assertEqual(len(controller.list_history()), 1)
        self.assertTrue(controller.list_history()[0]["complete"])

        controller.undo_last_complete()
        wait()
        self.assertEqual(source.read_bytes(), b"undo payload")
        self.assertFalse((self.target / source.name).exists())
        self.assertIsNotNone(controller.list_history()[0]["undone_at"])
        self.assertEqual(completed[-1].action, "undo")


if __name__ == "__main__":
    unittest.main()
