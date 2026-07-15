from __future__ import annotations

import ast
import base64
from dataclasses import replace
import os
from pathlib import Path
import sqlite3
import threading
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEventLoop
from PySide6.QtWidgets import QApplication

from database import DatabaseConfig
from repositories import AssetUpsert, LibraryRepository
from repositories import RepositoryCommitOutcomeUnknown
import services.safe_rename as safe_rename_module
from services.safe_rename import (
    SafeRenameController,
    SafeRenameError,
    SafeRenameInput,
    SafeRenameRunResult,
    SafeRenameWorker,
)
from services.safe_metadata import _read_title_artist
from tests.test_safe_metadata import _AUDIO_FIXTURES


def _app() -> QApplication:
    instance = QCoreApplication.instance()
    if instance is not None and not isinstance(instance, QApplication):
        raise RuntimeError("测试需要 QApplication，不能复用仅 QCoreApplication 实例")
    return instance or QApplication([])


def _wait_until(predicate, timeout_ms: int = 5000) -> bool:
    app = _app()
    deadline = __import__("time").monotonic() + timeout_ms / 1000
    while not predicate() and __import__("time").monotonic() < deadline:
        app.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 25)
    return bool(predicate())


class SafeRenameTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        _app()

    def setUp(self) -> None:
        self._temporary = TemporaryDirectory()
        self.root = Path(self._temporary.name)
        self.db_path = self.root / "library.sqlite3"
        self.root_a = self.root / "A"
        self.root_b = self.root / "B"
        self.root_a.mkdir()
        self.root_b.mkdir()

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def _repository(self) -> LibraryRepository:
        return LibraryRepository(DatabaseConfig(self.db_path))

    def _indexed_input(
        self,
        root: Path,
        source_name: str,
        target_name: str,
        *,
        content: bytes = b"fixture-bytes",
    ) -> SafeRenameInput:
        source = root / source_name
        source.write_bytes(content)
        metadata = source.stat()
        repository = self._repository()
        try:
            asset = repository.upsert_asset(
                AssetUpsert(source, metadata.st_size, metadata.st_mtime_ns)
            )
        finally:
            repository.close()
        return SafeRenameInput(
            asset_id=asset.id,
            source_path=source,
            target_path=root / target_name,
            allowed_root=root,
            expected_size_bytes=metadata.st_size,
            expected_mtime_ns=metadata.st_mtime_ns,
        )

    @staticmethod
    def _snapshot(path: Path) -> tuple[bytes, int, int]:
        metadata = path.stat()
        return path.read_bytes(), metadata.st_size, metadata.st_mtime_ns

    def _run_worker(self, *items: SafeRenameInput):
        worker = SafeRenameWorker(
            items=items,
            repository_factory=self._repository,
        )
        completed: list[SafeRenameRunResult] = []
        cancelled: list[SafeRenameRunResult] = []
        failed: list[str] = []
        worker.completed.connect(completed.append)
        worker.cancelled.connect(cancelled.append)
        worker.failed.connect(failed.append)
        worker.start()
        self.assertTrue(_wait_until(worker.isFinished), "SafeRenameWorker 未在时限内结束")
        _app().processEvents()
        self.assertEqual(len(completed) + len(cancelled) + len(failed), 1)
        return worker, completed, cancelled, failed

    def test_single_file_success_preserves_content_fingerprint_and_commits_all_three_records(self) -> None:
        item = self._indexed_input(self.root_a, "old.mp3", "new.mp3")
        before = self._snapshot(item.source_path)

        _worker, completed, cancelled, failed = self._run_worker(item)

        self.assertEqual((len(completed), cancelled, failed), (1, [], []))
        self.assertFalse(item.source_path.exists())
        self.assertEqual(self._snapshot(item.target_path), before)
        repository = self._repository()
        try:
            asset = repository.get_asset_by_id(item.asset_id)
            self.assertIsNotNone(asset)
            self.assertEqual(asset.canonical_path, item.target_path)  # type: ignore[union-attr]
            operations = completed[0].operations
            self.assertEqual(len(operations), 1)
            operation = repository.get_rename_operation(operations[0].id)
            records = repository.list_rename_operation_items(operations[0].id)
            self.assertEqual((operation.status, operation.success_count, operation.failure_count), ("success", 1, 0))  # type: ignore[union-attr]
            self.assertEqual([record.result for record in records], ["success"])
        finally:
            repository.close()

    def test_supported_metadata_sync_updates_tags_fingerprint_and_audit(self) -> None:
        request = self._indexed_input(
            self.root_a,
            "metadata-old.mp3",
            "新标题-新歌手.mp3",
            content=base64.b64decode(_AUDIO_FIXTURES[".mp3"]),
        )
        request = replace(
            request,
            sync_metadata=True,
            metadata_title="新标题",
            metadata_artist="新歌手",
        )

        _worker, completed, cancelled, failed = self._run_worker(request)

        self.assertEqual((len(completed), cancelled, failed), (1, [], []))
        self.assertEqual(completed[0].items[0].result, "success")
        self.assertEqual(_read_title_artist(request.target_path), ("新标题", "新歌手"))
        self.assertEqual(list(self.root_a.glob(".musicctrl-*")), [])
        metadata = request.target_path.stat()
        repository = self._repository()
        try:
            asset = repository.get_asset_by_id(request.asset_id)
            item = repository.get_rename_operation_item(completed[0].items[0].item_id)
            self.assertEqual((asset.size_bytes, asset.mtime_ns), (metadata.st_size, metadata.st_mtime_ns))  # type: ignore[union-attr]
            self.assertEqual(item.after["metadata_sync"]["original"]["title"], ["原歌名"])  # type: ignore[index,union-attr]
            self.assertEqual(item.after["metadata_sync"]["written"]["artist"], "新歌手")  # type: ignore[index,union-attr]
        finally:
            repository.close()

    def test_metadata_sync_database_failure_restores_exact_original_and_name(self) -> None:
        request = self._indexed_input(
            self.root_a,
            "metadata-rollback.mp3",
            "新标题-新歌手.mp3",
            content=base64.b64decode(_AUDIO_FIXTURES[".mp3"]),
        )
        request = replace(
            request,
            sync_metadata=True,
            metadata_title="新标题",
            metadata_artist="新歌手",
        )
        original = request.source_path.read_bytes()

        class FailCommitRepository(LibraryRepository):
            def commit_rename_item(self, operation_id: str, item_id: str, **_kwargs):
                raise sqlite3.OperationalError("deterministic metadata commit failure")

        worker = SafeRenameWorker(
            items=(request,),
            repository_factory=lambda: FailCommitRepository(DatabaseConfig(self.db_path)),
        )
        completed: list[SafeRenameRunResult] = []
        worker.completed.connect(completed.append)
        worker.start()
        self.assertTrue(_wait_until(lambda: worker.isFinished() and bool(completed)))

        self.assertEqual(completed[0].items[0].result, "rolled_back")
        self.assertEqual(request.source_path.read_bytes(), original)
        self.assertEqual(_read_title_artist(request.source_path), ("原歌名", "原歌手"))
        self.assertFalse(request.target_path.exists())
        self.assertEqual(list(self.root_a.glob(".musicctrl-*")), [])

    def test_two_roots_create_two_operations_without_common_ancestor_authority(self) -> None:
        first = self._indexed_input(self.root_a, "a.mp3", "a-new.mp3", content=b"A")
        second = self._indexed_input(self.root_b, "b.mp3", "b-new.mp3", content=b"B")

        _worker, completed, cancelled, failed = self._run_worker(first, second)

        self.assertEqual((len(completed), cancelled, failed), (1, [], []))
        self.assertEqual(len(completed[0].operations), 2)
        roots = {operation.summary["allowed_root"] for operation in completed[0].operations}
        self.assertEqual(roots, {os.fspath(self.root_a), os.fspath(self.root_b)})
        self.assertNotIn(os.fspath(self.root), roots)

    def test_existing_windows_equivalent_target_is_never_overwritten(self) -> None:
        source = self.root_a / "source.mp3"
        target = self.root_a / "taken.mp3"
        source.write_bytes(b"source")
        target.write_bytes(b"target")
        source_metadata = source.stat()
        repository = self._repository()
        try:
            asset = repository.upsert_asset(
                AssetUpsert(source, source_metadata.st_size, source_metadata.st_mtime_ns)
            )
        finally:
            repository.close()
        request = SafeRenameInput(
            asset.id,
            source,
            self.root_a / "TAKEN.mp3",
            self.root_a,
            source_metadata.st_size,
            source_metadata.st_mtime_ns,
        )

        _worker, completed, cancelled, failed = self._run_worker(request)

        self.assertEqual((len(completed), cancelled, failed), (1, [], []))
        self.assertEqual((completed[0].success_count, completed[0].failure_count), (0, 1))
        self.assertEqual(completed[0].items[0].result, "failed")
        self.assertEqual(source.read_bytes(), b"source")
        self.assertEqual(target.read_bytes(), b"target")

    @unittest.skipUnless(os.name == "nt", "Windows 目录句柄锁专项")
    def test_directory_chain_cannot_be_swapped_during_os_rename(self) -> None:
        nested = self.root_a / "nested"
        nested.mkdir()
        source = nested / "locked.mp3"
        source.write_bytes(b"locked-fixture")
        metadata = source.stat()
        repository = self._repository()
        try:
            asset = repository.upsert_asset(
                AssetUpsert(source, metadata.st_size, metadata.st_mtime_ns)
            )
        finally:
            repository.close()
        request = SafeRenameInput(
            asset.id,
            source,
            nested / "locked-new.mp3",
            self.root_a,
            metadata.st_size,
            metadata.st_mtime_ns,
        )
        parked = self.root_a / "parked"
        original_rename = os.rename
        swap_blocked: list[bool] = []

        def attempt_swap(src, dst):
            if Path(src) == request.source_path:
                try:
                    original_rename(nested, parked)
                except OSError:
                    swap_blocked.append(True)
                else:
                    swap_blocked.append(False)
                    original_rename(parked, nested)
            return original_rename(src, dst)

        with patch.object(safe_rename_module.os, "rename", side_effect=attempt_swap):
            _worker, completed, cancelled, failed = self._run_worker(request)

        self.assertEqual((len(completed), cancelled, failed), (1, [], []))
        self.assertEqual(completed[0].success_count, 1)
        self.assertEqual(swap_blocked, [True])
        self.assertTrue(request.target_path.exists())
        self.assertFalse(parked.exists())

    def test_pre_cancel_changes_no_file_and_records_all_items_cancelled(self) -> None:
        first = self._indexed_input(self.root_a, "one.mp3", "one-new.mp3")
        second = self._indexed_input(self.root_a, "two.mp3", "two-new.mp3")
        worker = SafeRenameWorker(items=(first, second), repository_factory=self._repository)
        completed: list[object] = []
        cancelled: list[SafeRenameRunResult] = []
        failed: list[str] = []
        worker.completed.connect(completed.append)
        worker.cancelled.connect(cancelled.append)
        worker.failed.connect(failed.append)
        worker.request_cancel()
        worker.start()
        self.assertTrue(_wait_until(worker.isFinished))
        self.assertTrue(_wait_until(lambda: bool(completed or cancelled or failed)))
        self.assertEqual((completed, len(cancelled), failed), ([], 1, []))
        self.assertTrue(first.source_path.exists())
        self.assertTrue(second.source_path.exists())
        repository = self._repository()
        try:
            records = tuple(
                record
                for operation in cancelled[0].operations
                for record in repository.list_rename_operation_items(operation.id)
            )
            self.assertTrue(records)
            self.assertEqual({record.result for record in records}, {"cancelled"})
        finally:
            repository.close()

    def test_controller_owns_worker_thread_and_three_rounds_leave_no_thread_running(self) -> None:
        controller = SafeRenameController(repository_factory=self._repository)
        terminals: list[object] = []
        controller.completed.connect(terminals.append)
        controller.cancelled.connect(terminals.append)
        controller.failed.connect(terminals.append)
        for index in range(3):
            request = self._indexed_input(
                self.root_a,
                f"round-{index}.mp3",
                f"round-{index}-new.mp3",
            )
            controller.start((request,))
            self.assertTrue(_wait_until(lambda: not controller.running))
        self.assertEqual(len(terminals), 3)
        self.assertFalse(controller.running)

    def test_fingerprint_drift_and_root_escape_fail_without_renaming(self) -> None:
        drifted = self._indexed_input(self.root_a, "drift.mp3", "drift-new.mp3")
        drifted.source_path.write_bytes(b"changed-after-preview")
        escaped = SafeRenameInput(
            drifted.asset_id,
            drifted.source_path,
            self.root_b / "escape.mp3",
            self.root_a,
            drifted.expected_size_bytes,
            drifted.expected_mtime_ns,
        )
        for request in (drifted, escaped):
            with self.subTest(target=request.target_path):
                _worker, completed, cancelled, failed = self._run_worker(request)
                if request is escaped:
                    self.assertEqual((completed, cancelled, len(failed)), ([], [], 1))
                else:
                    self.assertEqual((len(completed), cancelled, failed), (1, [], []))
                    self.assertEqual(completed[0].items[0].result, "failed")
                self.assertTrue(request.source_path.exists())
                self.assertFalse(request.target_path.exists())

    def test_database_failure_restores_source_and_records_rolled_back(self) -> None:
        request = self._indexed_input(self.root_a, "db-fail.mp3", "db-fail-new.mp3")

        class FailCommitRepository(LibraryRepository):
            def commit_rename_item(self, operation_id: str, item_id: str):
                raise sqlite3.OperationalError("deterministic commit failure")

        factory = lambda: FailCommitRepository(DatabaseConfig(self.db_path))
        worker = SafeRenameWorker(items=(request,), repository_factory=factory)
        completed: list[SafeRenameRunResult] = []
        worker.completed.connect(completed.append)
        worker.start()
        self.assertTrue(_wait_until(lambda: worker.isFinished() and bool(completed)))

        self.assertTrue(request.source_path.exists())
        self.assertFalse(request.target_path.exists())
        self.assertEqual(completed[0].items[0].result, "rolled_back")
        repository = self._repository()
        try:
            item = repository.get_rename_operation_item(completed[0].items[0].item_id)
            asset = repository.get_asset_by_id(request.asset_id)
            self.assertEqual(item.result, "rolled_back")  # type: ignore[union-attr]
            self.assertEqual(asset.canonical_path, request.source_path)  # type: ignore[union-attr]
        finally:
            repository.close()

    def test_database_failure_and_compensation_failure_records_rollback_failed(self) -> None:
        request = self._indexed_input(self.root_a, "rollback-fail.mp3", "rollback-fail-new.mp3")

        class FailCommitRepository(LibraryRepository):
            def commit_rename_item(self, operation_id: str, item_id: str):
                raise sqlite3.OperationalError("commit failed")

        real_rename = os.rename
        calls = 0

        def rename_once_then_fail(source, target):
            nonlocal calls
            calls += 1
            if calls == 1:
                return real_rename(source, target)
            raise PermissionError("rollback blocked")

        worker = SafeRenameWorker(
            items=(request,),
            repository_factory=lambda: FailCommitRepository(DatabaseConfig(self.db_path)),
        )
        completed: list[SafeRenameRunResult] = []
        worker.completed.connect(completed.append)
        with patch.object(safe_rename_module.os, "rename", side_effect=rename_once_then_fail):
            worker.start()
            self.assertTrue(_wait_until(lambda: worker.isFinished() and bool(completed)))

        self.assertFalse(request.source_path.exists())
        self.assertTrue(request.target_path.exists())
        self.assertEqual(completed[0].items[0].result, "rollback_failed")
        repository = self._repository()
        try:
            item = repository.get_rename_operation_item(completed[0].items[0].item_id)
            self.assertEqual(item.result, "rollback_failed")  # type: ignore[union-attr]
            self.assertIn("恢复失败", item.error_message)  # type: ignore[union-attr]
            self.assertEqual(item.after["actual_path"], os.fspath(request.target_path))  # type: ignore[index,union-attr]
        finally:
            repository.close()

    def test_post_rename_validation_failure_is_compensated_not_left_as_plain_failed(self) -> None:
        request = self._indexed_input(self.root_a, "post-check.mp3", "post-check-new.mp3")
        real_lstat = os.lstat
        rejected = False

        def reject_first_post_rename_target(path, *args, **kwargs):
            nonlocal rejected
            candidate = Path(path)
            if (
                candidate == request.target_path
                and not request.source_path.exists()
                and not rejected
            ):
                rejected = True
                raise PermissionError("post-rename target validation blocked")
            return real_lstat(path, *args, **kwargs)

        worker = SafeRenameWorker(items=(request,), repository_factory=self._repository)
        completed: list[SafeRenameRunResult] = []
        worker.completed.connect(completed.append)
        with patch.object(safe_rename_module.os, "lstat", side_effect=reject_first_post_rename_target):
            worker.start()
            self.assertTrue(_wait_until(lambda: worker.isFinished() and bool(completed)))

        self.assertTrue(request.source_path.exists())
        self.assertFalse(request.target_path.exists())
        self.assertEqual(completed[0].items[0].result, "rolled_back")

    def test_post_commit_unknown_readback_success_never_retries_or_rolls_back(self) -> None:
        request = self._indexed_input(self.root_a, "unknown.mp3", "unknown-new.mp3")
        commit_calls = 0
        factory_calls = 0

        class CommitThenRaiseRepository(LibraryRepository):
            def commit_rename_item(self, operation_id: str, item_id: str):
                nonlocal commit_calls
                commit_calls += 1
                connection = self._connection

                class Proxy:
                    @property
                    def in_transaction(self):
                        return connection.in_transaction

                    def execute(self, sql, *args):
                        value = connection.execute(sql, *args)
                        if sql == "COMMIT":
                            raise sqlite3.OperationalError("after real COMMIT")
                        return value

                    def close(self):
                        return connection.close()

                self._connection = Proxy()  # type: ignore[assignment]
                return super().commit_rename_item(operation_id, item_id)

        def factory():
            nonlocal factory_calls
            factory_calls += 1
            repository_type = CommitThenRaiseRepository if factory_calls == 1 else LibraryRepository
            return repository_type(DatabaseConfig(self.db_path))

        worker = SafeRenameWorker(items=(request,), repository_factory=factory)
        completed: list[SafeRenameRunResult] = []
        worker.completed.connect(completed.append)
        worker.start()
        self.assertTrue(_wait_until(lambda: worker.isFinished() and bool(completed)))

        self.assertEqual((factory_calls, commit_calls), (2, 1))
        self.assertFalse(request.source_path.exists())
        self.assertTrue(request.target_path.exists())
        self.assertEqual(completed[0].items[0].result, "success")

    def test_metadata_post_commit_unknown_readback_finalizes_only_after_proven_success(self) -> None:
        request = self._indexed_input(
            self.root_a,
            "metadata-unknown.mp3",
            "新标题-新歌手.mp3",
            content=base64.b64decode(_AUDIO_FIXTURES[".mp3"]),
        )
        request = replace(
            request,
            sync_metadata=True,
            metadata_title="新标题",
            metadata_artist="新歌手",
        )
        factory_calls = 0

        class CommitThenRaiseRepository(LibraryRepository):
            def commit_rename_item(self, operation_id: str, item_id: str, **kwargs):
                connection = self._connection

                class Proxy:
                    @property
                    def in_transaction(self):
                        return connection.in_transaction

                    def execute(self, sql, *args):
                        value = connection.execute(sql, *args)
                        if sql == "COMMIT":
                            raise sqlite3.OperationalError("after real metadata COMMIT")
                        return value

                    def close(self):
                        return connection.close()

                self._connection = Proxy()  # type: ignore[assignment]
                return super().commit_rename_item(operation_id, item_id, **kwargs)

        def factory():
            nonlocal factory_calls
            factory_calls += 1
            repository_type = CommitThenRaiseRepository if factory_calls == 1 else LibraryRepository
            return repository_type(DatabaseConfig(self.db_path))

        worker = SafeRenameWorker(items=(request,), repository_factory=factory)
        completed: list[SafeRenameRunResult] = []
        worker.completed.connect(completed.append)
        worker.start()
        self.assertTrue(_wait_until(lambda: worker.isFinished() and bool(completed)))

        self.assertEqual(factory_calls, 2)
        self.assertEqual(completed[0].items[0].result, "success")
        self.assertEqual(_read_title_artist(request.target_path), ("新标题", "新歌手"))
        self.assertEqual(list(self.root_a.glob(".musicctrl-*")), [])

    def test_unknown_readback_source_running_restores_file_and_records_rolled_back(self) -> None:
        request = self._indexed_input(self.root_a, "unknown-old.mp3", "unknown-target.mp3")
        factory_calls = 0

        class UnknownWithoutCommitRepository(LibraryRepository):
            def commit_rename_item(self, operation_id: str, item_id: str):
                raise RepositoryCommitOutcomeUnknown(
                    "unknown",
                    operation_id=operation_id,
                    item_id=item_id,
                )

        def factory():
            nonlocal factory_calls
            factory_calls += 1
            repository_type = UnknownWithoutCommitRepository if factory_calls == 1 else LibraryRepository
            return repository_type(DatabaseConfig(self.db_path))

        worker = SafeRenameWorker(items=(request,), repository_factory=factory)
        completed: list[SafeRenameRunResult] = []
        worker.completed.connect(completed.append)
        worker.start()
        self.assertTrue(_wait_until(lambda: worker.isFinished() and bool(completed)))

        self.assertTrue(request.source_path.exists())
        self.assertFalse(request.target_path.exists())
        self.assertEqual(completed[0].items[0].result, "rolled_back")

    def test_unknown_mixed_readback_never_finishes_operation_as_success(self) -> None:
        request = self._indexed_input(self.root_a, "mixed-old.mp3", "mixed-target.mp3")
        factory_calls = 0

        class UnknownWithoutCommitRepository(LibraryRepository):
            def commit_rename_item(self, operation_id: str, item_id: str):
                raise RepositoryCommitOutcomeUnknown(
                    "unknown",
                    operation_id=operation_id,
                    item_id=item_id,
                )

        def factory():
            nonlocal factory_calls
            factory_calls += 1
            if factory_calls == 1:
                return UnknownWithoutCommitRepository(DatabaseConfig(self.db_path))
            repository = LibraryRepository(DatabaseConfig(self.db_path))
            repository._connection.execute(
                "UPDATE operation_items SET result='success' WHERE result='running'"
            )
            return repository

        worker = SafeRenameWorker(items=(request,), repository_factory=factory)
        completed: list[SafeRenameRunResult] = []
        failed: list[str] = []
        worker.completed.connect(completed.append)
        worker.failed.connect(failed.append)
        worker.start()
        self.assertTrue(_wait_until(lambda: worker.isFinished() and bool(failed)))

        self.assertEqual(completed, [])
        self.assertIn("recovery_required", failed[0])
        self.assertFalse(request.source_path.exists())
        self.assertTrue(request.target_path.exists())
        connection = sqlite3.connect(self.db_path)
        try:
            operation_status = connection.execute(
                "SELECT status FROM operations"
            ).fetchone()[0]
            item_status = connection.execute(
                "SELECT result FROM operation_items"
            ).fetchone()[0]
            asset_path = connection.execute(
                "SELECT canonical_path FROM assets WHERE id=?",
                (request.asset_id,),
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertEqual((operation_status, item_status), ("running", "success"))
        self.assertEqual(Path(asset_path), request.source_path)

    def test_item_boundary_cancel_finishes_current_and_cancels_remaining(self) -> None:
        first = self._indexed_input(self.root_a, "cancel-one.mp3", "cancel-one-new.mp3")
        second = self._indexed_input(self.root_a, "cancel-two.mp3", "cancel-two-new.mp3")
        worker = SafeRenameWorker(items=(first, second), repository_factory=self._repository)
        cancelled: list[SafeRenameRunResult] = []
        worker.cancelled.connect(cancelled.append)
        real_rename_one = safe_rename_module._rename_one_file
        calls = 0

        def rename_and_cancel(item: SafeRenameInput):
            nonlocal calls
            result = real_rename_one(item)
            calls += 1
            if calls == 1:
                worker.request_cancel()
            return result

        with patch.object(safe_rename_module, "_rename_one_file", side_effect=rename_and_cancel):
            worker.start()
            self.assertTrue(_wait_until(lambda: worker.isFinished() and bool(cancelled)))

        self.assertEqual([item.result for item in cancelled[0].items], ["success", "cancelled"])
        self.assertTrue(first.target_path.exists())
        self.assertTrue(second.source_path.exists())
        self.assertFalse(second.target_path.exists())

    def test_production_module_has_no_overwrite_copy_metadata_hash_or_shortcut_operations(self) -> None:
        module_path = Path(__file__).parents[1] / "services" / "safe_rename.py"
        source = module_path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        imports = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.Import, ast.ImportFrom))
            for alias in node.names
        }
        self.assertFalse({"shutil", "mutagen", "hashlib"} & imports)
        forbidden_calls: list[str] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            function = node.func
            name = function.attr if isinstance(function, ast.Attribute) else function.id if isinstance(function, ast.Name) else ""
            if name in {"replace", "copy", "copy2", "move", "unlink", "remove", "write_bytes", "write_text", "save"}:
                forbidden_calls.append(name)
        self.assertEqual(forbidden_calls, [])
        lowered = source.casefold()
        self.assertNotIn("mutagen", lowered)
        self.assertNotIn(".lnk", lowered)


if __name__ == "__main__":
    unittest.main()
