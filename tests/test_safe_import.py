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
    IMPORT_HISTORY_KEY,
    IMPORT_PATHS_KEY,
    PENDING_IMPORT_KEY,
    SafeImportError,
    SafeImportPreviewWorker,
    cleanup_stale_candidates,
    enumerate_import_files,
    iter_import_files,
    import_one,
    SafeImportController,
    _journal_for_plan,
    _result_to_history,
    ImportItemResult,
    ImportRunResult,
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
        controller.start_preview(self.source, self.target, "audio")
        import time

        deadline = time.monotonic() + 5
        while controller.running and time.monotonic() < deadline:
            self.app.processEvents()
        self.app.processEvents()
        self.assertFalse(controller.running)
        self.assertIsNotNone(controller.current_plan)
        controller.start_execute(controller.current_plan.id)
        self._wait_for_controller(controller)
        self.assertEqual(len(completed), 1)
        self.assertEqual(failed, [])
        self.assertEqual(completed[0].success_count, 1)
        self.assertEqual(
            controller.remembered_paths("audio"),
            (self.source, self.target),
        )
        self.assertIsNone(controller.remembered_paths("lyrics"))

    def test_planned_journal_write_failure_causes_zero_file_change(self) -> None:
        source = self.source / "journal.mp3"
        payload = b"journal-write-must-precede-files"
        source.write_bytes(payload)

        class FailingRepository:
            def create_import_journal(inner_self, *, pending_key, batch_id, journal):
                self.assertEqual(pending_key, PENDING_IMPORT_KEY)
                self.assertEqual(batch_id, journal["batch_id"])
                raise RuntimeError("journal unavailable")

            def close(inner_self):
                return None

        controller = SafeImportController(lambda: FailingRepository())
        failures: list[str] = []
        controller.failed.connect(failures.append)
        controller.start_preview(self.source, self.target, "audio")
        self._wait_for_controller(controller)
        plan = controller.current_plan
        self.assertIsNotNone(plan)
        controller.start_execute(plan.id)
        self._wait_for_controller(controller)

        self.assertTrue(failures)
        self.assertEqual(source.read_bytes(), payload)
        self.assertEqual(tuple(self.target.iterdir()), ())

    def test_malformed_remembered_import_paths_fail_closed(self) -> None:
        config = DatabaseConfig(self.root / "remembered.sqlite3")
        malformed = {"audio": {"source_root": "relative", "target_root": str(self.target)}}
        with LibraryRepository(config) as repository:
            repository.set_setting(IMPORT_PATHS_KEY, malformed)
        controller = SafeImportController(lambda: LibraryRepository(config))
        warnings: list[str] = []
        controller.warning.connect(warnings.append)

        self.assertIsNone(controller.remembered_paths("audio"))
        self.assertTrue(warnings)
        with LibraryRepository(config) as repository:
            self.assertEqual(repository.get_setting(IMPORT_PATHS_KEY).value, malformed)

    def test_recovery_rolls_forward_target_only_then_real_undo_succeeds(self) -> None:
        source = self.source / "recovery.mp3"
        payload = b"verified target-only recovery"
        source.write_bytes(payload)
        config = DatabaseConfig(self.root / "recovery.sqlite3")
        controller = SafeImportController(lambda: LibraryRepository(config))
        controller.start_preview(self.source, self.target, "audio")
        self._wait_for_controller(controller)
        plan = controller.current_plan
        self.assertIsNotNone(plan)
        journal = _journal_for_plan(plan)
        target = self.target / source.name
        target.write_bytes(payload)
        source.unlink()
        journal["items"][0]["state"] = "source_deleted"
        with LibraryRepository(config) as repository:
            repository.set_setting(PENDING_IMPORT_KEY, journal)

        controller.start_recovery()
        self._wait_for_controller(controller)
        history = controller.list_history()
        self.assertEqual(len(history), 1)
        self.assertTrue(history[0]["complete"])
        with LibraryRepository(config) as repository:
            self.assertIsNone(repository.get_setting(PENDING_IMPORT_KEY))

        controller.undo_last_complete()
        self._wait_for_controller(controller)
        self.assertEqual(source.read_bytes(), payload)
        self.assertFalse(target.exists())

    def test_recovery_removes_owned_candidate_but_never_source(self) -> None:
        source = self.source / "candidate.mp3"
        payload = b"candidate"
        source.write_bytes(payload)
        config = DatabaseConfig(self.root / "candidate.sqlite3")
        controller = SafeImportController(lambda: LibraryRepository(config))
        controller.start_preview(self.source, self.target, "audio")
        self._wait_for_controller(controller)
        plan = controller.current_plan
        journal = _journal_for_plan(plan)
        candidate = Path(journal["items"][0]["candidate_path"])
        candidate.write_bytes(payload)
        journal["items"][0]["state"] = "candidate_ready"
        with LibraryRepository(config) as repository:
            repository.set_setting(PENDING_IMPORT_KEY, journal)

        controller.start_recovery()
        self._wait_for_controller(controller)
        self.assertEqual(source.read_bytes(), payload)
        self.assertFalse(candidate.exists())
        self.assertFalse((self.target / source.name).exists())
        self.assertFalse(controller.list_history()[0]["complete"])

    def test_recovery_planned_state_never_deletes_external_target(self) -> None:
        source = self.source / "external.mp3"
        payload = b"same bytes do not prove ownership"
        source.write_bytes(payload)
        config = DatabaseConfig(self.root / "planned-external.sqlite3")
        controller = SafeImportController(lambda: LibraryRepository(config))
        controller.start_preview(self.source, self.target, "audio")
        self._wait_for_controller(controller)
        plan = controller.current_plan
        journal = _journal_for_plan(plan)
        target = self.target / source.name
        target.write_bytes(payload)
        with LibraryRepository(config) as repository:
            repository.set_setting(PENDING_IMPORT_KEY, journal)

        failures: list[str] = []
        controller.failed.connect(failures.append)
        controller.start_recovery()
        self._wait_for_controller(controller)

        self.assertTrue(failures)
        self.assertEqual(source.read_bytes(), payload)
        self.assertEqual(target.read_bytes(), payload)
        with LibraryRepository(config) as repository:
            self.assertIsNotNone(repository.get_setting(PENDING_IMPORT_KEY))

    def test_recovery_done_state_with_recreated_source_never_deletes_target(self) -> None:
        source = self.source / "recreated.mp3"
        payload = b"completed target and newly recreated source"
        source.write_bytes(payload)
        config = DatabaseConfig(self.root / "done-recreated.sqlite3")
        controller = SafeImportController(lambda: LibraryRepository(config))
        controller.start_preview(self.source, self.target, "audio")
        self._wait_for_controller(controller)
        plan = controller.current_plan
        journal = _journal_for_plan(plan)
        journal["items"][0]["state"] = "done"
        target = self.target / source.name
        target.write_bytes(payload)
        with LibraryRepository(config) as repository:
            repository.set_setting(PENDING_IMPORT_KEY, journal)

        failures: list[str] = []
        controller.failed.connect(failures.append)
        controller.start_recovery()
        self._wait_for_controller(controller)

        self.assertTrue(failures)
        self.assertEqual(source.read_bytes(), payload)
        self.assertEqual(target.read_bytes(), payload)
        with LibraryRepository(config) as repository:
            self.assertIsNotNone(repository.get_setting(PENDING_IMPORT_KEY))

    def test_recovery_candidate_ready_keeps_both_source_and_target(self) -> None:
        source = self.source / "candidate-ready.mp3"
        payload = b"same content is not ownership proof"
        source.write_bytes(payload)
        config = DatabaseConfig(self.root / "candidate-ready-target.sqlite3")
        controller = SafeImportController(lambda: LibraryRepository(config))
        controller.start_preview(self.source, self.target, "audio")
        self._wait_for_controller(controller)
        plan = controller.current_plan
        journal = _journal_for_plan(plan)
        journal["items"][0]["state"] = "candidate_ready"
        target = self.target / source.name
        target.write_bytes(payload)
        with LibraryRepository(config) as repository:
            repository.set_setting(PENDING_IMPORT_KEY, journal)

        controller.start_recovery()
        self._wait_for_controller(controller)

        self.assertEqual(source.read_bytes(), payload)
        self.assertEqual(target.read_bytes(), payload)
        history = controller.list_history()
        self.assertEqual(history[-1]["terminal_status"], "failed")
        self.assertFalse(history[-1]["complete"])

    def test_recovery_removes_partial_planned_candidate(self) -> None:
        source = self.source / "partial.mp3"
        source.write_bytes(b"complete source payload")
        config = DatabaseConfig(self.root / "partial-candidate.sqlite3")
        controller = SafeImportController(lambda: LibraryRepository(config))
        controller.start_preview(self.source, self.target, "audio")
        self._wait_for_controller(controller)
        plan = controller.current_plan
        journal = _journal_for_plan(plan)
        candidate = Path(journal["items"][0]["candidate_path"])
        candidate.write_bytes(b"partial")
        with LibraryRepository(config) as repository:
            repository.set_setting(PENDING_IMPORT_KEY, journal)

        controller.start_recovery()
        self._wait_for_controller(controller)

        self.assertFalse(candidate.exists())
        self.assertTrue(source.exists())
        with LibraryRepository(config) as repository:
            self.assertIsNone(repository.get_setting(PENDING_IMPORT_KEY))

    def test_recovery_corrupt_journal_fails_closed_and_keeps_it(self) -> None:
        source = self.source / "keep.mp3"
        source.write_bytes(b"keep")
        config = DatabaseConfig(self.root / "corrupt.sqlite3")
        corrupt = {"version": 999, "batch_id": "bad"}
        with LibraryRepository(config) as repository:
            repository.set_setting(PENDING_IMPORT_KEY, corrupt)
        controller = SafeImportController(lambda: LibraryRepository(config))
        failures: list[str] = []
        controller.failed.connect(failures.append)
        controller.start_recovery()
        self._wait_for_controller(controller)
        self.assertTrue(failures)
        self.assertEqual(source.read_bytes(), b"keep")
        with LibraryRepository(config) as repository:
            self.assertEqual(repository.get_setting(PENDING_IMPORT_KEY).value, corrupt)

    def test_recovery_exact_history_residual_only_clears_pending_journal(self) -> None:
        source = self.source / "exact.mp3"
        payload = b"exact"
        source.write_bytes(payload)
        config = DatabaseConfig(self.root / "exact.sqlite3")
        controller = SafeImportController(lambda: LibraryRepository(config))
        controller.start_preview(self.source, self.target, "audio")
        self._wait_for_controller(controller)
        plan = controller.current_plan
        journal = _journal_for_plan(plan)
        target = self.target / source.name
        target.write_bytes(payload)
        source.unlink()
        journal["items"][0]["state"] = "done"
        result = ImportRunResult(
            self.source, self.target,
            (ImportItemResult(source, target, "success", "done",
                              hashlib.sha256(payload).hexdigest()),),
            1, 0, 0, 0, plan_id=plan.id,
        )
        entry = _result_to_history(result)
        with LibraryRepository(config) as repository:
            repository.set_setting(PENDING_IMPORT_KEY, journal)
            repository.set_setting(IMPORT_HISTORY_KEY, [entry])

        controller.start_recovery()
        self._wait_for_controller(controller)
        with LibraryRepository(config) as repository:
            self.assertIsNone(repository.get_setting(PENDING_IMPORT_KEY))
            self.assertEqual(repository.get_setting(IMPORT_HISTORY_KEY).value, [entry])

    def test_fault_after_unlink_leaves_target_only_and_recovery_is_idempotent(self) -> None:
        source = self.source / "after-unlink.mp3"
        payload = b"after unlink"
        source.write_bytes(payload)
        config = DatabaseConfig(self.root / "after-unlink.sqlite3")
        controller = SafeImportController(lambda: LibraryRepository(config))
        controller.start_preview(self.source, self.target, "audio")
        self._wait_for_controller(controller)
        plan = controller.current_plan
        original = LibraryRepository.set_setting
        injected = threading.Event()

        def fail_after_unlink(repository, key, value):
            items = value.get("items", []) if isinstance(value, dict) else []
            if (
                key == PENDING_IMPORT_KEY
                and any(item.get("state") == "source_deleted" for item in items)
                and not injected.is_set()
            ):
                injected.set()
                raise RuntimeError("fault after unlink")
            return original(repository, key, value)

        with mock.patch.object(LibraryRepository, "set_setting", new=fail_after_unlink):
            controller.start_execute(plan.id)
            self._wait_for_controller(controller)
        target = self.target / source.name
        self.assertTrue(injected.is_set())
        self.assertFalse(source.exists())
        self.assertEqual(target.read_bytes(), payload)

        controller.start_recovery()
        self._wait_for_controller(controller)
        self.assertTrue(controller.list_history()[0]["complete"])
        before = controller.list_history()
        controller.start_recovery()
        self._wait_for_controller(controller)
        self.assertEqual(controller.list_history(), before)
        self.assertEqual(target.read_bytes(), payload)

    def test_fault_after_rename_rolls_target_back_and_recovery_keeps_source(self) -> None:
        source = self.source / "after-rename.mp3"
        payload = b"after rename"
        source.write_bytes(payload)
        config = DatabaseConfig(self.root / "after-rename.sqlite3")
        controller = SafeImportController(lambda: LibraryRepository(config))
        controller.start_preview(self.source, self.target, "audio")
        self._wait_for_controller(controller)
        plan = controller.current_plan
        original = LibraryRepository.set_setting
        injected = threading.Event()

        def fail_after_rename(repository, key, value):
            items = value.get("items", []) if isinstance(value, dict) else []
            if (
                key == PENDING_IMPORT_KEY
                and any(item.get("state") == "target_placed" for item in items)
                and not injected.is_set()
            ):
                injected.set()
                raise RuntimeError("fault after rename")
            return original(repository, key, value)

        with mock.patch.object(LibraryRepository, "set_setting", new=fail_after_rename):
            controller.start_execute(plan.id)
            self._wait_for_controller(controller)
        self.assertTrue(injected.is_set())
        self.assertEqual(source.read_bytes(), payload)
        self.assertFalse((self.target / source.name).exists())

        controller.start_recovery()
        self._wait_for_controller(controller)
        self.assertEqual(source.read_bytes(), payload)
        self.assertFalse(controller.list_history()[0]["complete"])

    def test_preview_is_read_only_and_same_batch_targets_both_conflict(self) -> None:
        first = self.source / "one"
        second = self.source / "two"
        first.mkdir()
        second.mkdir()
        (first / "Same.MP3").write_bytes(b"one")
        (second / "same.mp3").write_bytes(b"two")
        before = {
            path.relative_to(self.root): (path.read_bytes(), path.stat().st_mtime_ns)
            for path in self.root.rglob("*") if path.is_file()
        }
        controller = SafeImportController()
        controller.start_preview(self.source, self.target, "audio")
        self._wait_for_controller(controller)
        plan = controller.current_plan
        self.assertIsNotNone(plan)
        self.assertEqual(plan.ready_count, 0)
        self.assertEqual(plan.conflict_count, 2)
        self.assertEqual(tuple(self.target.iterdir()), ())
        after = {
            path.relative_to(self.root): (path.read_bytes(), path.stat().st_mtime_ns)
            for path in self.root.rglob("*") if path.is_file()
        }
        self.assertEqual(after, before)

    def test_plan_is_one_shot_and_does_not_enumerate_new_file(self) -> None:
        planned = self.source / "planned.mp3"
        planned.write_bytes(b"planned")
        controller = SafeImportController()
        controller.start_preview(self.source, self.target, "audio")
        self._wait_for_controller(controller)
        plan = controller.current_plan
        self.assertIsNotNone(plan)
        late = self.source / "late.mp3"
        late.write_bytes(b"late")
        controller.start_execute(plan.id)
        self._wait_for_controller(controller)
        self.assertFalse(planned.exists())
        self.assertTrue((self.target / planned.name).exists())
        self.assertTrue(late.exists())
        self.assertFalse((self.target / late.name).exists())
        with self.assertRaisesRegex(SafeImportError, "失效"):
            controller.start_execute(plan.id)

    def test_target_created_after_preview_fails_closed(self) -> None:
        source = self.source / "race.mp3"
        source.write_bytes(b"source")
        controller = SafeImportController()
        completed: list[object] = []
        controller.completed.connect(completed.append)
        controller.start_preview(self.source, self.target, "audio")
        self._wait_for_controller(controller)
        plan = controller.current_plan
        self.assertIsNotNone(plan)
        target = self.target / source.name
        target.write_bytes(b"late target")
        controller.start_execute(plan.id)
        self._wait_for_controller(controller)
        self.assertEqual(completed[-1].failure_count, 1)
        self.assertTrue(source.exists())
        self.assertEqual(target.read_bytes(), b"late target")

    def test_cancelled_execution_persists_every_ready_item(self) -> None:
        for name in ("a.mp3", "b.mp3"):
            (self.source / name).write_bytes(name.encode("utf-8"))
        config = DatabaseConfig(self.root / "cancelled-history.sqlite3")
        controller = SafeImportController(lambda: LibraryRepository(config))
        controller.start_preview(self.source, self.target, "audio")
        self._wait_for_controller(controller)
        plan = controller.current_plan
        self.assertIsNotNone(plan)
        entered = threading.Event()

        def cancelled_import(*_args, cancel_event, **_kwargs):
            entered.set()
            while not cancel_event.is_set():
                time.sleep(0.001)
            raise InterruptedError("用户取消")

        with mock.patch("services.safe_import.import_one", side_effect=cancelled_import):
            controller.start_execute(plan.id)
            deadline = time.monotonic() + 5
            while not entered.is_set() and time.monotonic() < deadline:
                self.app.processEvents()
            controller.request_cancel()
            self._wait_for_controller(controller)
        history = controller.list_history()
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["terminal_status"], "cancelled")
        self.assertFalse(history[0]["complete"])
        self.assertEqual([item["status"] for item in history[0]["items"]], ["cancelled", "cancelled"])
        self.assertTrue(all((self.source / name).exists() for name in ("a.mp3", "b.mp3")))

    def test_completed_batch_with_duplicate_is_undoable_for_success_only(self) -> None:
        movable = self.source / "move.mp3"
        duplicate = self.source / "same.mp3"
        movable.write_bytes(b"move")
        duplicate.write_bytes(b"same")
        (self.target / duplicate.name).write_bytes(b"same")
        config = DatabaseConfig(self.root / "mixed-history.sqlite3")
        controller = SafeImportController(lambda: LibraryRepository(config))
        self._preview_and_execute(controller, mode="audio")
        history = controller.list_history()
        self.assertTrue(history[0]["complete"])
        self.assertEqual({item["status"] for item in history[0]["items"]}, {"success", "duplicate"})
        controller.undo_last_complete()
        self._wait_for_controller(controller)
        self.assertEqual(movable.read_bytes(), b"move")
        self.assertTrue(duplicate.exists())
        self.assertEqual((self.target / duplicate.name).read_bytes(), b"same")

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

        self._preview_and_execute(controller, mode="audio")
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

    def test_history_rejects_bad_times_and_duplicate_ids(self) -> None:
        source = self.source / "history.mp3"
        source.write_bytes(b"history")
        config = DatabaseConfig(self.root / "strict-history.sqlite3")
        controller = SafeImportController(lambda: LibraryRepository(config))
        self._preview_and_execute(controller, mode="audio")
        original = list(controller.list_history())
        for key, value in (
            ("created_at", "2026-01-01T00:00:00"),
            ("undone_at", 123),
        ):
            damaged = [dict(original[0])]
            damaged[0][key] = value
            with LibraryRepository(config) as repository:
                repository.set_setting("p6.import_history", damaged)
            with self.assertRaisesRegex(SafeImportError, "时间"):
                controller.list_history()
        duplicate = [dict(original[0]), dict(original[0])]
        with LibraryRepository(config) as repository:
            repository.set_setting("p6.import_history", duplicate)
        with self.assertRaisesRegex(SafeImportError, "重复"):
            controller.list_history()

    def test_legacy_history_overflow_is_read_only_compacted_on_next_append(self) -> None:
        config = DatabaseConfig(self.root / "legacy-overflow.sqlite3")

        def legacy(index: int) -> dict[str, object]:
            return {
                "id": f"legacy-{index:03d}",
                "created_at": f"2026-01-01T00:{index // 60:02d}:{index % 60:02d}+00:00",
                "mode": "audio",
                "source_root": str(self.source),
                "target_root": str(self.target),
                "undone_at": None,
                "complete": True,
                "items": [{
                    "source_path": str(self.source / f"old-{index}.mp3"),
                    "target_path": str(self.target / f"old-{index}.mp3"),
                    "status": "success",
                    "sha256": "0" * 64,
                    "message": "legacy",
                }],
            }

        raw = [legacy(index) for index in range(201)]
        with LibraryRepository(config) as repository:
            repository.set_setting("p6.import_history", raw)
        controller = SafeImportController(lambda: LibraryRepository(config))
        visible = controller.list_history()
        self.assertEqual(len(visible), 200)
        self.assertEqual(visible[0]["id"], "legacy-001")
        with LibraryRepository(config) as repository:
            self.assertEqual(len(repository.get_setting("p6.import_history").value), 201)

        (self.source / "new.mp3").write_bytes(b"new")
        self._preview_and_execute(controller, mode="audio")
        stored = controller.list_history()
        self.assertEqual(len(stored), 200)
        self.assertNotEqual(stored[-1]["id"], "legacy-200")
        with LibraryRepository(config) as repository:
            self.assertEqual(len(repository.get_setting("p6.import_history").value), 200)

        new_overflow = [legacy(index) for index in range(201)]
        for item in new_overflow:
            item["terminal_status"] = "completed"
            item["terminal_message"] = ""
            item["plan_id"] = None
        with LibraryRepository(config) as repository:
            repository.set_setting("p6.import_history", new_overflow)
        with self.assertRaisesRegex(SafeImportError, "新版"):
            controller.list_history()

    def test_sha256_cancel_is_checked_after_each_blocking_read(self) -> None:
        source = self.source / "large.mp3"
        source.write_bytes(os.urandom(2 * 1024 * 1024))

        class CancelAfterRead:
            calls = 0

            def is_set(inner_self) -> bool:
                inner_self.calls += 1
                return inner_self.calls >= 2

        with self.assertRaises(InterruptedError):
            import_one(
                source,
                source_root=self.source,
                target_root=self.target,
                cancel_event=CancelAfterRead(),  # type: ignore[arg-type]
            )
        self.assertTrue(source.exists())
        self.assertFalse((self.target / source.name).exists())

    def test_lyrics_import_persists_mode_and_remains_undoable(self) -> None:
        lyric = self.source / "晴天-周杰伦.lrc"
        lyric.write_text("[00:00.00]晴天", encoding="utf-8")
        config = DatabaseConfig(self.root / "lyrics-history.sqlite3")
        controller = SafeImportController(lambda: LibraryRepository(config))
        completed: list[object] = []
        controller.completed.connect(completed.append)

        self._preview_and_execute(controller, mode="lyrics")
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

        self._preview_and_execute(controller, mode="audio")
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
        self._preview_and_execute(controller, mode="audio")

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
        self._preview_and_execute(controller, mode="audio")
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
        self._preview_and_execute(controller, mode="audio")
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
        worker = SafeImportPreviewWorker(
            source_root=self.source,
            target_root=self.target,
            mode="audio",
        )
        completed: list[object] = []
        cancelled: list[object] = []
        failed: list[str] = []
        worker.completed.connect(completed.append)
        worker.cancelled.connect(lambda: cancelled.append(True))
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

    def _preview_and_execute(self, controller: SafeImportController, *, mode: str) -> None:
        controller.start_preview(self.source, self.target, mode)
        self._wait_for_controller(controller)
        plan = controller.current_plan
        self.assertIsNotNone(plan)
        controller.start_execute(plan.id)
        self._wait_for_controller(controller)


if __name__ == "__main__":
    unittest.main()
