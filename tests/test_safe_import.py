from __future__ import annotations

import hashlib
from dataclasses import replace
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import time
import unittest
from unittest import mock

from PySide6.QtWidgets import QApplication
from database import DatabaseConfig
from repositories import LibraryRepository

from services.safe_import import (
    SafeImportError,
    SafeImportWorker,
    cleanup_stale_candidates,
    enumerate_import_files,
    iter_import_files,
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
        self.assertEqual(controller.list_history()[0]["mode"], "audio")
        self.assertEqual(completed[-1].mode, "audio")

        controller.undo_last_complete()
        wait()
        self.assertEqual(source.read_bytes(), b"undo payload")
        self.assertFalse((self.target / source.name).exists())
        self.assertIsNotNone(controller.list_history()[0]["undone_at"])
        self.assertEqual(completed[-1].action, "undo")
        self.assertEqual(completed[-1].mode, "audio")

    def test_history_requires_a_known_mode(self) -> None:
        config = DatabaseConfig(self.root / "bad-history.sqlite3")
        with LibraryRepository(config) as repository:
            repository.set_setting(
                "p6.import_history",
                [
                    {
                        "id": "legacy",
                        "source_root": str(self.source),
                        "target_root": str(self.target),
                        "complete": True,
                        "undone_at": None,
                        "items": [],
                    }
                ],
            )
        controller = SafeImportController(lambda: LibraryRepository(config))
        with self.assertRaisesRegex(SafeImportError, "模式"):
            controller.list_history()

    def test_lyrics_import_persists_mode_and_remains_undoable(self) -> None:
        lyric = self.source / "晴天-周杰伦.lrc"
        lyric.write_text("[00:00.00]晴天", encoding="utf-8")
        config = DatabaseConfig(self.root / "lyrics-history.sqlite3")
        controller = SafeImportController(lambda: LibraryRepository(config))
        completed: list[object] = []
        controller.completed.connect(completed.append)

        controller.start(self.source, self.target, "lyrics")
        self._wait_for_controller(controller)
        self.assertEqual(completed[-1].mode, "lyrics")
        self.assertEqual(controller.list_history()[0]["mode"], "lyrics")
        self.assertFalse(lyric.exists())

        controller.undo_last_complete()
        self._wait_for_controller(controller)
        self.assertEqual(completed[-1].mode, "lyrics")
        self.assertEqual(lyric.read_text(encoding="utf-8"), "[00:00.00]晴天")

    def test_undo_rejects_history_paths_outside_recorded_roots(self) -> None:
        source = self.source / "inside.mp3"
        source.write_bytes(b"payload")
        config = DatabaseConfig(self.root / "escape-history.sqlite3")
        controller = SafeImportController(lambda: LibraryRepository(config))
        completed: list[object] = []
        failed: list[str] = []
        controller.completed.connect(completed.append)
        controller.failed.connect(failed.append)

        controller.start(self.source, self.target, "audio")
        self._wait_for_controller(controller)
        outside = self.root / "outside.mp3"
        imported = self.target / source.name
        outside.write_bytes(imported.read_bytes())
        with LibraryRepository(config) as repository:
            history = repository.get_setting("p6.import_history").value
            history[0]["items"][0]["target_path"] = str(outside)
            repository.set_setting("p6.import_history", history)

        controller.undo_last_complete()
        self._wait_for_controller(controller)
        self.assertEqual(len(failed), 1)
        self.assertIn("路径", failed[0])
        self.assertTrue(imported.exists())
        self.assertTrue(outside.exists())
        self.assertFalse(source.exists())

    def test_undo_can_be_cancelled_without_controller_lifecycle_error(self) -> None:
        source = self.source / "cancel-undo.mp3"
        source.write_bytes(b"payload")
        config = DatabaseConfig(self.root / "cancel-history.sqlite3")
        controller = SafeImportController(lambda: LibraryRepository(config))
        controller.start(self.source, self.target, "audio")
        self._wait_for_controller(controller)

        entered = threading.Event()
        real_import_one = import_one

        def cancellable_import(*args, **kwargs):
            if Path(args[0]).parent == self.target:
                entered.set()
                cancel_event = kwargs.get("cancel_event")
                while cancel_event is not None and not cancel_event.is_set():
                    time.sleep(0.001)
                raise InterruptedError("用户取消撤销")
            return real_import_one(*args, **kwargs)

        cancelled: list[object] = []
        failed: list[str] = []
        controller.cancelled.connect(cancelled.append)
        controller.failed.connect(failed.append)
        with mock.patch("services.safe_import.import_one", side_effect=cancellable_import):
            controller.undo_last_complete()
            deadline = time.monotonic() + 5
            while not entered.is_set() and time.monotonic() < deadline:
                self.app.processEvents()
            self.assertTrue(entered.is_set())
            controller.request_cancel()
            self._wait_for_controller(controller)
        self.assertEqual(len(cancelled), 1)
        self.assertEqual(failed, [])
        self.assertEqual(cancelled[0].action, "undo")
        self.assertTrue((self.target / source.name).exists())
        self.assertFalse(source.exists())

    def test_undo_revalidates_target_after_preflight_before_moving(self) -> None:
        source = self.source / "tamper-after-preflight.mp3"
        source.write_bytes(b"original import")
        config = DatabaseConfig(self.root / "tamper-after-preflight.sqlite3")
        controller = SafeImportController(lambda: LibraryRepository(config))
        controller.start(self.source, self.target, "audio")
        self._wait_for_controller(controller)
        imported = self.target / source.name
        real_import_one = import_one
        tampered = False

        def tamper_before_move(path, *args, **kwargs):
            nonlocal tampered
            if Path(path) == imported and not tampered:
                tampered = True
                imported.write_bytes(b"attacker changed target after preflight")
            return real_import_one(path, *args, **kwargs)

        failed: list[str] = []
        controller.failed.connect(failed.append)
        with mock.patch("services.safe_import.import_one", side_effect=tamper_before_move):
            controller.undo_last_complete()
            self._wait_for_controller(controller)

        self.assertTrue(tampered)
        self.assertEqual(len(failed), 1)
        self.assertIn("SHA-256", failed[0])
        self.assertFalse(source.exists())
        self.assertEqual(imported.read_bytes(), b"attacker changed target after preflight")

    def test_undo_post_move_verification_failure_compensates_to_target(self) -> None:
        source = self.source / "post-move-mismatch.mp3"
        payload = b"original import"
        source.write_bytes(payload)
        config = DatabaseConfig(self.root / "post-move-mismatch.sqlite3")
        controller = SafeImportController(lambda: LibraryRepository(config))
        controller.start(self.source, self.target, "audio")
        self._wait_for_controller(controller)
        imported = self.target / source.name
        real_import_one = import_one

        def corrupt_report(path, *args, **kwargs):
            result = real_import_one(path, *args, **kwargs)
            if Path(path) == imported and result.status == "success":
                return replace(result, sha256="0" * 64)
            return result

        failed: list[str] = []
        controller.failed.connect(failed.append)
        with mock.patch("services.safe_import.import_one", side_effect=corrupt_report):
            controller.undo_last_complete()
            self._wait_for_controller(controller)

        self.assertEqual(len(failed), 1)
        self.assertFalse(source.exists())
        self.assertEqual(imported.read_bytes(), payload)
        self.assertIsNone(controller.list_history()[0]["undone_at"])

    def test_worker_emits_cancelled_when_iterator_stops_on_cancel(self) -> None:
        worker = SafeImportWorker(
            source_root=self.source,
            target_root=self.target,
            mode="audio",
        )
        completed: list[object] = []
        cancelled: list[object] = []
        failed: list[str] = []
        worker.completed.connect(completed.append)
        worker.cancelled.connect(cancelled.append)
        worker.failed.connect(failed.append)

        def stop_for_cancel(*_args, cancel_event, **_kwargs):
            cancel_event.set()
            if False:
                yield self.source / "never.mp3"

        with mock.patch("services.safe_import.iter_import_files", side_effect=stop_for_cancel):
            worker.start()
            deadline = time.monotonic() + 5
            while worker.isRunning() and time.monotonic() < deadline:
                self.app.processEvents()
            worker.wait(1000)
            self.app.processEvents()

        self.assertEqual(completed, [])
        self.assertEqual(len(cancelled), 1)
        self.assertEqual(failed, [])

    def test_lazy_enumeration_stops_consuming_entries_at_cancel_boundary(self) -> None:
        cancel = threading.Event()

        class FakeEntry:
            name = "ignored.txt"

            def stat(self, *, follow_symlinks: bool):
                return os.lstat(self.source)

            def is_symlink(self) -> bool:
                return False

            def is_dir(self, *, follow_symlinks: bool) -> bool:
                return False

            def is_file(self, *, follow_symlinks: bool) -> bool:
                return True

            def __init__(self, source: Path) -> None:
                self.source = source

        sentinel = self.source / "ignored.txt"
        sentinel.write_bytes(b"x")
        consumed = 0

        class ControlledScandir:
            def __enter__(inner_self):
                return inner_self

            def __exit__(inner_self, *_args):
                return False

            def __iter__(inner_self):
                return inner_self

            def __next__(inner_self):
                nonlocal consumed
                consumed += 1
                if consumed == 2:
                    cancel.set()
                if consumed > 2:
                    self.fail("取消后仍继续消费目录条目")
                return FakeEntry(sentinel)

        with mock.patch("services.safe_import.os.scandir", return_value=ControlledScandir()):
            self.assertEqual(list(iter_import_files(self.source, mode="audio", cancel_event=cancel)), [])
        self.assertEqual(consumed, 2)

    def _wait_for_controller(self, controller: SafeImportController) -> None:
        deadline = time.monotonic() + 5
        while controller.running and time.monotonic() < deadline:
            self.app.processEvents()
        self.app.processEvents()
        self.assertFalse(controller.running)


if __name__ == "__main__":
    unittest.main()
