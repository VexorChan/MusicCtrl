from __future__ import annotations

import os
from pathlib import Path
import sqlite3
import threading
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from PySide6.QtCore import QCoreApplication, QEventLoop, QThread, QTimer

from database import DatabaseConfig, open_database
from repositories import LibraryRepository
from services.scan_worker import ReadOnlyScanWorker


class FakeRepository:
    def __init__(
        self,
        *,
        batch_hook=None,
        reconcile_hook=None,
        reconcile_error: Exception | None = None,
        close_error: Exception | None = None,
    ) -> None:
        self.batch_hook = batch_hook
        self.reconcile_hook = reconcile_hook
        self.reconcile_error = reconcile_error
        self.close_error = close_error
        self.thread_ids: list[tuple[str, int]] = []
        self.batches: list[tuple[object, ...]] = []
        self.finished_statuses: list[str] = []
        self.reconcile_count = 0
        self.close_count = 0

    def _record_thread(self, operation: str) -> None:
        self.thread_ids.append((operation, threading.get_ident()))

    def create_scan_session(self, *, mode: str, source_folder: Path):
        self._record_thread("create")
        return SimpleNamespace(id="fake-session")

    def index_scan_batch(self, session_id: str, items):
        self._record_thread("write")
        batch = tuple(items)
        self.batches.append(batch)
        if self.batch_hook is not None:
            self.batch_hook(batch)
        return tuple(object() for _ in batch)

    def finish_scan_session(self, session_id: str, *, status: str):
        self._record_thread(f"finish:{status}")
        self.finished_statuses.append(status)
        return SimpleNamespace(id=session_id, status=status)

    def complete_scan_and_reconcile(self, session_id: str):
        self._record_thread("reconcile")
        self.reconcile_count += 1
        if self.reconcile_hook is not None:
            self.reconcile_hook()
        if self.reconcile_error is not None:
            raise self.reconcile_error
        self.finished_statuses.append("completed")
        return SimpleNamespace(session_id=session_id)

    def close(self) -> None:
        self._record_thread("close")
        self.close_count += 1
        if self.close_error is not None:
            raise self.close_error


class ScanWorkerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = QCoreApplication.instance() or QCoreApplication([])

    def setUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.scan_root = self.root / "scan"
        self.scan_root.mkdir()
        self.database_path = self.root / "library.sqlite3"
        self.config = DatabaseConfig(self.database_path, timeout_seconds=1.0, busy_timeout_ms=1000)

    def touch(self, name: str) -> Path:
        path = self.scan_root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
        return path

    def observe(self, worker: ReadOnlyScanWorker) -> dict[str, list]:
        observed: dict[str, list] = {
            "batches": [],
            "completed": [],
            "cancelled": [],
            "failed": [],
            "finished": [],
        }
        worker.batch_ready.connect(observed["batches"].append)
        worker.completed.connect(observed["completed"].append)
        worker.cancelled.connect(observed["cancelled"].append)
        worker.failed.connect(observed["failed"].append)
        worker.finished.connect(lambda: observed["finished"].append(True))
        return observed

    def run_worker(self, worker: ReadOnlyScanWorker, *, poll=None) -> dict[str, list]:
        observed = self.observe(worker)
        loop = QEventLoop()
        worker.finished.connect(loop.quit)
        timeout = QTimer()
        timeout.setSingleShot(True)
        timed_out = []

        def on_timeout() -> None:
            timed_out.append(True)
            worker.cancel()
            loop.quit()

        timeout.timeout.connect(on_timeout)
        poll_timer = None
        if poll is not None:
            poll_timer = QTimer()
            poll_timer.setInterval(0)
            poll_timer.timeout.connect(poll)
            poll_timer.start()

        worker.start()
        timeout.start(5000)
        loop.exec()
        timeout.stop()
        if poll_timer is not None:
            poll_timer.stop()
        self.assertFalse(timed_out, "worker did not finish before timeout")
        self.assertTrue(worker.wait(5000), "worker thread did not join")
        QCoreApplication.processEvents()
        return observed

    def assert_one_terminal(self, observed: dict[str, list], expected: str) -> None:
        counts = {name: len(observed[name]) for name in ("completed", "cancelled", "failed")}
        self.assertEqual(sum(counts.values()), 1, counts)
        self.assertEqual(counts[expected], 1, counts)
        self.assertEqual(len(observed["finished"]), 1)

    def test_real_database_scan_writes_batches_and_completes_session(self) -> None:
        for name in ("b.mp3", "a.FLAC", "nested/c.ogg"):
            self.touch(name)
        factory_threads: list[int] = []

        def factory() -> LibraryRepository:
            factory_threads.append(threading.get_ident())
            return LibraryRepository(self.config)

        worker = ReadOnlyScanWorker(
            root=self.scan_root,
            allowed_root=self.root,
            repository_factory=factory,
            batch_size=2,
        )
        observed = self.run_worker(worker)

        self.assert_one_terminal(observed, "completed")
        self.assertEqual(observed["completed"], [3])
        self.assertEqual([len(batch) for batch in observed["batches"]], [2, 1])
        self.assertTrue(all(isinstance(batch, tuple) for batch in observed["batches"]))
        self.assertNotEqual(factory_threads, [threading.get_ident()])

        with LibraryRepository(self.config) as repository:
            assets = repository.list_assets()
            self.assertEqual(len(assets), 3)
            self.assertTrue(all(asset.mtime_ns is not None for asset in assets))
        connection = open_database(self.config)
        try:
            session = connection.execute("SELECT id, status FROM scan_sessions").fetchone()
            self.assertEqual(session["status"], "completed")
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM scan_items").fetchone()[0],
                3,
            )
        finally:
            connection.close()

    def test_success_uses_reconcile_once_instead_of_plain_finish(self) -> None:
        self.touch("success.mp3")
        repository = FakeRepository()
        worker = ReadOnlyScanWorker(
            root=self.scan_root,
            allowed_root=self.root,
            repository_factory=lambda: repository,  # type: ignore[arg-type,return-value]
            batch_size=1,
        )

        observed = self.run_worker(worker)

        self.assert_one_terminal(observed, "completed")
        self.assertEqual(repository.reconcile_count, 1)
        self.assertEqual(repository.finished_statuses, ["completed"])

    def test_cancel_before_start_skips_factory_and_all_writes(self) -> None:
        self.touch("never.mp3")
        factory_calls = []
        worker = ReadOnlyScanWorker(
            root=self.scan_root,
            allowed_root=self.root,
            repository_factory=lambda: factory_calls.append(True),  # type: ignore[arg-type,return-value]
            batch_size=1,
        )
        worker.cancel()

        observed = self.run_worker(worker)

        self.assert_one_terminal(observed, "cancelled")
        self.assertEqual(observed["cancelled"], [0])
        self.assertEqual(factory_calls, [])
        self.assertFalse(self.database_path.exists())

    def test_mid_batch_cancel_keeps_committed_batch_but_emits_no_batch(self) -> None:
        self.touch("a.mp3")
        self.touch("b.mp3")
        entered = threading.Event()
        release = threading.Event()

        def batch_hook(batch) -> None:
            entered.set()
            if not release.wait(5):
                raise RuntimeError("test release timeout")

        repository = FakeRepository(batch_hook=batch_hook)
        worker = ReadOnlyScanWorker(
            root=self.scan_root,
            allowed_root=self.root,
            repository_factory=lambda: repository,  # type: ignore[arg-type,return-value]
            batch_size=1,
        )
        cancellation_sent = []

        def cancel_when_write_started() -> None:
            if entered.is_set() and not cancellation_sent:
                cancellation_sent.append(True)
                worker.cancel()
                release.set()

        observed = self.run_worker(worker, poll=cancel_when_write_started)

        self.assert_one_terminal(observed, "cancelled")
        self.assertEqual(observed["cancelled"], [1])
        self.assertEqual(observed["batches"], [])
        self.assertEqual(len(repository.batches), 1)
        self.assertEqual(repository.finished_statuses, ["cancelled"])
        self.assertEqual(repository.reconcile_count, 0)
        self.assertEqual(repository.close_count, 1)

    def test_real_database_cancelled_count_matches_committed_rows_without_batch_signal(self) -> None:
        self.touch("committed.mp3")
        entered = threading.Event()
        release = threading.Event()

        class CommitGateRepository(LibraryRepository):
            def __init__(inner_self) -> None:
                super().__init__(self.config)

            def index_scan_batch(inner_self, session_id, items):
                records = super().index_scan_batch(session_id, items)
                entered.set()
                if not release.wait(5):
                    raise RuntimeError("test release timeout")
                return records

        worker = ReadOnlyScanWorker(
            root=self.scan_root,
            allowed_root=self.root,
            repository_factory=CommitGateRepository,  # type: ignore[arg-type]
            batch_size=1,
        )
        cancellation_sent = []

        def cancel_after_commit() -> None:
            if entered.is_set() and not cancellation_sent:
                cancellation_sent.append(True)
                worker.cancel()
                release.set()

        observed = self.run_worker(worker, poll=cancel_after_commit)

        self.assert_one_terminal(observed, "cancelled")
        self.assertEqual(observed["cancelled"], [1])
        self.assertEqual(observed["batches"], [])
        connection = open_database(self.config)
        try:
            asset_count = connection.execute("SELECT COUNT(*) FROM assets").fetchone()[0]
            item_count = connection.execute("SELECT COUNT(*) FROM scan_items").fetchone()[0]
            status = connection.execute("SELECT status FROM scan_sessions").fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(asset_count, observed["cancelled"][0])
        self.assertEqual(item_count, observed["cancelled"][0])
        self.assertEqual(status, "cancelled")

    def test_factory_construction_error_emits_failed_without_repository_close(self) -> None:
        factory_calls = []

        def failing_factory():
            factory_calls.append(threading.get_ident())
            raise RuntimeError("factory construction failed")

        worker = ReadOnlyScanWorker(
            root=self.scan_root,
            allowed_root=self.root,
            repository_factory=failing_factory,
            batch_size=1,
        )

        observed = self.run_worker(worker)

        self.assert_one_terminal(observed, "failed")
        self.assertIn("factory construction failed", observed["failed"][0])
        self.assertEqual(len(factory_calls), 1)
        self.assertNotEqual(factory_calls[0], threading.get_ident())

    def test_reconcile_error_emits_failed_marks_session_failed_and_closes_once(self) -> None:
        self.touch("finish.mp3")
        repository = FakeRepository(reconcile_error=RuntimeError("reconcile failed"))
        worker = ReadOnlyScanWorker(
            root=self.scan_root,
            allowed_root=self.root,
            repository_factory=lambda: repository,  # type: ignore[arg-type,return-value]
            batch_size=1,
        )

        observed = self.run_worker(worker)

        self.assert_one_terminal(observed, "failed")
        self.assertIn("reconcile failed", observed["failed"][0])
        self.assertEqual(repository.reconcile_count, 1)
        self.assertEqual(repository.finished_statuses, ["failed"])
        self.assertEqual(repository.close_count, 1)

    def test_cancel_arriving_after_reconcile_commit_keeps_completed_terminal(self) -> None:
        self.touch("late-cancel.mp3")
        entered = threading.Event()
        release = threading.Event()

        def reconcile_hook() -> None:
            entered.set()
            if not release.wait(5):
                raise RuntimeError("test release timeout")

        repository = FakeRepository(reconcile_hook=reconcile_hook)
        worker = ReadOnlyScanWorker(
            root=self.scan_root,
            allowed_root=self.root,
            repository_factory=lambda: repository,  # type: ignore[arg-type,return-value]
            batch_size=1,
        )
        cancellation_sent = []

        def cancel_after_reconcile_entered() -> None:
            if entered.is_set() and not cancellation_sent:
                cancellation_sent.append(True)
                worker.cancel()
                release.set()

        observed = self.run_worker(worker, poll=cancel_after_reconcile_entered)

        self.assert_one_terminal(observed, "completed")
        self.assertEqual(observed["completed"], [1])
        self.assertEqual(repository.reconcile_count, 1)
        self.assertEqual(repository.finished_statuses, ["completed"])

    def test_scanner_and_repository_errors_emit_only_failed_and_close(self) -> None:
        scenarios = (
            (self.root / "missing", FakeRepository(), "目录不存在"),
            (
                self.scan_root,
                FakeRepository(batch_hook=lambda batch: (_ for _ in ()).throw(RuntimeError("write failed"))),
                "write failed",
            ),
        )
        self.touch("error.mp3")
        for root, repository, expected in scenarios:
            with self.subTest(expected=expected):
                worker = ReadOnlyScanWorker(
                    root=root,
                    allowed_root=self.root,
                    repository_factory=lambda repository=repository: repository,  # type: ignore[arg-type,return-value]
                    batch_size=1,
                )
                observed = self.run_worker(worker)
                self.assert_one_terminal(observed, "failed")
                self.assertIn(expected, observed["failed"][0])
                self.assertEqual(repository.finished_statuses, ["failed"])
                self.assertEqual(repository.reconcile_count, 0)
                self.assertEqual(repository.close_count, 1)

    def test_factory_write_finish_close_share_worker_thread_and_close_precedes_finished(self) -> None:
        self.touch("thread.mp3")
        events: list[str] = []

        class OrderedRepository(FakeRepository):
            def close(inner_self) -> None:
                events.append("close")
                super().close()

        repository = OrderedRepository()
        factory_threads: list[int] = []

        def factory():
            factory_threads.append(threading.get_ident())
            return repository

        worker = ReadOnlyScanWorker(
            root=self.scan_root,
            allowed_root=self.root,
            repository_factory=factory,  # type: ignore[arg-type]
            batch_size=1,
        )
        worker.finished.connect(lambda: events.append("finished"))
        observed = self.run_worker(worker)

        self.assert_one_terminal(observed, "completed")
        operation_threads = [thread_id for _, thread_id in repository.thread_ids]
        self.assertTrue(operation_threads)
        self.assertTrue(all(thread_id == factory_threads[0] for thread_id in operation_threads))
        self.assertNotEqual(factory_threads[0], threading.get_ident())
        self.assertLess(events.index("close"), events.index("finished"))
        self.assertEqual(repository.close_count, 1)

    def test_main_event_loop_heartbeat_continues_while_worker_is_blocked(self) -> None:
        self.touch("heartbeat.mp3")
        entered = threading.Event()
        release = threading.Event()
        heartbeat = []

        def batch_hook(batch) -> None:
            entered.set()
            if not release.wait(5):
                raise RuntimeError("test release timeout")

        repository = FakeRepository(batch_hook=batch_hook)
        worker = ReadOnlyScanWorker(
            root=self.scan_root,
            allowed_root=self.root,
            repository_factory=lambda: repository,  # type: ignore[arg-type,return-value]
            batch_size=1,
        )

        def tick() -> None:
            if entered.is_set():
                heartbeat.append(True)
                if len(heartbeat) >= 3:
                    release.set()

        observed = self.run_worker(worker, poll=tick)

        self.assert_one_terminal(observed, "completed")
        self.assertGreaterEqual(len(heartbeat), 3)

    def test_real_database_batch_failure_marks_session_failed_and_writes_no_current_batch(self) -> None:
        self.touch("reject.mp3")
        with LibraryRepository(self.config):
            pass
        connection = open_database(self.config)
        try:
            connection.execute(
                """
                CREATE TRIGGER reject_worker_scan_item
                BEFORE INSERT ON scan_items
                BEGIN
                    SELECT RAISE(ABORT, 'worker batch failure');
                END
                """
            )
        finally:
            connection.close()

        worker = ReadOnlyScanWorker(
            root=self.scan_root,
            allowed_root=self.root,
            repository_factory=lambda: LibraryRepository(self.config),
            batch_size=1,
        )
        observed = self.run_worker(worker)

        self.assert_one_terminal(observed, "failed")
        self.assertIn("worker batch failure", observed["failed"][0])
        connection = open_database(self.config)
        try:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM assets").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM scan_items").fetchone()[0], 0)
            self.assertEqual(
                connection.execute("SELECT status FROM scan_sessions").fetchone()[0],
                "failed",
            )
        finally:
            connection.close()

    def test_real_database_two_scans_reconcile_changed_and_missing_assets(self) -> None:
        changed = self.touch("changed.mp3")
        missing = self.touch("missing.mp3")

        first = ReadOnlyScanWorker(
            root=self.scan_root,
            allowed_root=self.root,
            repository_factory=lambda: LibraryRepository(self.config),
            batch_size=2,
        )
        first_observed = self.run_worker(first)
        self.assert_one_terminal(first_observed, "completed")

        first_stat = changed.stat()
        os.utime(
            changed,
            ns=(first_stat.st_atime_ns, first_stat.st_mtime_ns + 1_000_000),
        )
        missing.unlink()

        second = ReadOnlyScanWorker(
            root=self.scan_root,
            allowed_root=self.root,
            repository_factory=lambda: LibraryRepository(self.config),
            batch_size=2,
        )
        second_observed = self.run_worker(second)
        self.assert_one_terminal(second_observed, "completed")

        with LibraryRepository(self.config) as repository:
            changed_record = repository.get_asset_by_path(changed)
            missing_record = repository.get_asset_by_path(missing)
            self.assertEqual(changed_record.file_state, "external_changed")  # type: ignore[union-attr]
            self.assertEqual(missing_record.file_state, "missing")  # type: ignore[union-attr]
        self.assertEqual(changed.stat().st_size, 0)

    def test_close_error_is_reported_without_hiding_primary_failure(self) -> None:
        self.touch("close.mp3")

        def fail_write(batch) -> None:
            raise RuntimeError("primary write failure")

        repository = FakeRepository(
            batch_hook=fail_write,
            close_error=RuntimeError("close failure"),
        )
        worker = ReadOnlyScanWorker(
            root=self.scan_root,
            allowed_root=self.root,
            repository_factory=lambda: repository,  # type: ignore[arg-type,return-value]
            batch_size=1,
        )

        observed = self.run_worker(worker)

        self.assert_one_terminal(observed, "failed")
        self.assertIn("primary write failure", observed["failed"][0])
        self.assertIn("close failure", observed["failed"][0])
        self.assertEqual(repository.close_count, 1)

    def test_worker_is_one_shot_and_second_start_never_calls_factory(self) -> None:
        self.touch("once.mp3")
        repository = FakeRepository()
        factory_calls = []

        def factory():
            factory_calls.append(threading.get_ident())
            return repository

        worker = ReadOnlyScanWorker(
            root=self.scan_root,
            allowed_root=self.root,
            repository_factory=factory,  # type: ignore[arg-type]
            batch_size=1,
        )
        observed = self.run_worker(worker)
        self.assert_one_terminal(observed, "completed")

        with self.assertRaisesRegex(RuntimeError, "one-shot"):
            worker.start()

        self.assertEqual(len(factory_calls), 1)
        self.assertEqual(len(repository.batches), 1)


if __name__ == "__main__":
    unittest.main()
