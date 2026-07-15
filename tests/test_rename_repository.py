from __future__ import annotations

from dataclasses import FrozenInstanceError
import os
from pathlib import Path
import sqlite3
import threading
from tempfile import TemporaryDirectory
import unittest

from database import DatabaseConfig, open_database
from repositories.library_repository import (
    AssetUpsert,
    LibraryRepository,
    RecordNotFoundError,
    RenamePlanItem,
    RepositoryClosedError,
    RepositoryCommitOutcomeUnknown,
    RepositoryDataError,
    RepositoryPathError,
    RepositoryThreadError,
)


class RenameRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.music_root = self.root / "music"
        self.music_root.mkdir()
        self.config = DatabaseConfig(
            self.root / "library.sqlite3",
            timeout_seconds=1.0,
            busy_timeout_ms=1000,
        )

    def create_repository(self) -> LibraryRepository:
        repository = LibraryRepository(self.config)

        def close_if_open() -> None:
            try:
                repository.close()
            except RepositoryClosedError:
                pass

        self.addCleanup(close_if_open)
        return repository

    def add_audio(
        self,
        repository: LibraryRepository,
        name: str,
        *,
        content: bytes = b"fixture-audio",
        file_state: str = "active",
    ):
        path = self.music_root / name
        path.write_bytes(content)
        stat = path.stat()
        record = repository.upsert_asset(
            AssetUpsert(
                canonical_path=path,
                size_bytes=stat.st_size,
                mtime_ns=stat.st_mtime_ns,
                file_state=file_state,
            )
        )
        return path, record

    @staticmethod
    def plan(asset, source: Path, target: Path, **overrides) -> RenamePlanItem:
        return RenamePlanItem(
            asset_id=overrides.get("asset_id", asset.id),
            source_path=overrides.get("source_path", source),
            target_path=overrides.get("target_path", target),
            expected_size_bytes=overrides.get("expected_size_bytes", asset.size_bytes),
            expected_mtime_ns=overrides.get("expected_mtime_ns", asset.mtime_ns),
        )

    @staticmethod
    def media_snapshot(root: Path) -> dict[str, tuple[bytes, int, int]]:
        return {
            path.relative_to(root).as_posix(): (
                path.read_bytes(),
                path.stat().st_size,
                path.stat().st_mtime_ns,
            )
            for path in sorted(root.rglob("*"))
            if path.is_file() and path.suffix.casefold() != ".sqlite3"
        }

    def create_operation(self, repository: LibraryRepository, *plans: RenamePlanItem):
        return repository.create_rename_operation(
            allowed_root=self.music_root,
            items=plans,
        )

    def test_create_plan_returns_frozen_stable_records_and_does_not_touch_media(self) -> None:
        repository = self.create_repository()
        alpha_path, alpha = self.add_audio(repository, "Alpha.mp3")
        beta_path, beta = self.add_audio(repository, "Beta.flac", content=b"beta")
        before = self.media_snapshot(self.music_root)

        operation, items = self.create_operation(
            repository,
            self.plan(beta, beta_path, self.music_root / "Beta-New.flac"),
            self.plan(alpha, alpha_path, self.music_root / "Alpha-New.mp3"),
        )

        self.assertEqual(operation.operation_type, "rename")
        self.assertEqual(operation.status, "planned")
        self.assertEqual(operation.success_count, 0)
        self.assertEqual(operation.failure_count, 0)
        self.assertEqual(len(items), 2)
        self.assertEqual(repository.get_rename_operation(operation.id), operation)
        self.assertEqual(repository.list_rename_operation_items(operation.id), items)
        self.assertEqual({item.asset_id for item in items}, {alpha.id, beta.id})
        self.assertTrue(all(item.result == "planned" for item in items))
        self.assertTrue(all(item.before["canonical_path"] for item in items))
        with self.assertRaises(FrozenInstanceError):
            operation.status = "running"  # type: ignore[misc]
        self.assertEqual(self.media_snapshot(self.music_root), before)

    def test_empty_and_duplicate_plan_inputs_write_nothing(self) -> None:
        repository = self.create_repository()
        first_path, first = self.add_audio(repository, "duplicate-first.mp3")
        second_path, second = self.add_audio(repository, "duplicate-second.mp3")
        first_plan = self.plan(first, first_path, self.music_root / "target-first.mp3")
        duplicate_asset = self.plan(first, first_path, self.music_root / "target-other.mp3")
        duplicate_source = self.plan(
            second,
            second_path,
            self.music_root / "target-second.mp3",
            source_path=first_path,
        )
        duplicate_target = self.plan(second, second_path, self.music_root / "target-first.mp3")

        for plans in ((), (first_plan, duplicate_asset), (first_plan, duplicate_source), (first_plan, duplicate_target)):
            with self.subTest(plans=len(plans)):
                with self.assertRaises(RepositoryDataError):
                    self.create_operation(repository, *plans)
                self.assertEqual(repository._connection.execute("SELECT COUNT(*) FROM operations").fetchone()[0], 0)
                self.assertEqual(repository._connection.execute("SELECT COUNT(*) FROM operation_items").fetchone()[0], 0)

    def test_plan_validation_failure_is_all_or_nothing(self) -> None:
        invalid_cases = (
            "relative-source",
            "relative-target",
            "missing-asset",
            "source-mismatch",
            "fingerprint-mismatch",
            "non-active",
            "different-parent",
            "same-target",
            "extension-change",
            "target-occupied",
            "invalid-character",
            "control-character",
            "reserved-device",
            "trailing-dot",
            "trailing-space",
            "empty-stem",
        )
        for case in invalid_cases:
            with self.subTest(case=case):
                with TemporaryDirectory() as directory:
                    root = Path(directory)
                    music = root / "music"
                    music.mkdir()
                    config = DatabaseConfig(root / "library.sqlite3")
                    repository = LibraryRepository(config)
                    try:
                        first_path = music / "first.mp3"
                        first_path.write_bytes(b"first")
                        first_stat = first_path.stat()
                        first = repository.upsert_asset(
                            AssetUpsert(first_path, first_stat.st_size, first_stat.st_mtime_ns)
                        )
                        second_path = music / "second.mp3"
                        second_path.write_bytes(b"second")
                        second_stat = second_path.stat()
                        second = repository.upsert_asset(
                            AssetUpsert(
                                second_path,
                                second_stat.st_size,
                                second_stat.st_mtime_ns,
                                file_state="missing" if case == "non-active" else "active",
                            )
                        )
                        valid = self.plan(first, first_path, music / "first-new.mp3")
                        invalid = self.plan(second, second_path, music / "second-new.mp3")
                        if case == "relative-source":
                            invalid = self.plan(second, second_path, music / "second-new.mp3", source_path=Path("second.mp3"))
                        elif case == "relative-target":
                            invalid = self.plan(second, second_path, music / "second-new.mp3", target_path=Path("new.mp3"))
                        elif case == "missing-asset":
                            invalid = self.plan(second, second_path, music / "second-new.mp3", asset_id="missing")
                        elif case == "source-mismatch":
                            invalid = self.plan(second, second_path, music / "second-new.mp3", source_path=music / "wrong.mp3")
                        elif case == "fingerprint-mismatch":
                            invalid = self.plan(second, second_path, music / "second-new.mp3", expected_size_bytes=999)
                        elif case == "different-parent":
                            invalid = self.plan(second, second_path, music / "nested" / "second-new.mp3")
                        elif case == "same-target":
                            invalid = self.plan(second, second_path, second_path)
                        elif case == "extension-change":
                            invalid = self.plan(second, second_path, music / "second-new.flac")
                        elif case == "target-occupied":
                            invalid = self.plan(second, second_path, first_path)
                        elif case == "invalid-character":
                            invalid = self.plan(second, second_path, music / "bad?.mp3")
                        elif case == "control-character":
                            invalid = self.plan(second, second_path, music / "bad\x01.mp3")
                        elif case == "reserved-device":
                            invalid = self.plan(second, second_path, music / "CON.mp3")
                        elif case == "trailing-dot":
                            invalid = self.plan(second, second_path, music / "new.mp3.")
                        elif case == "trailing-space":
                            invalid = self.plan(second, second_path, music / "new.mp3 ")
                        elif case == "empty-stem":
                            invalid = self.plan(second, second_path, music / ".mp3")
                        with self.assertRaises(
                            (RecordNotFoundError, RepositoryDataError, RepositoryPathError)
                        ):
                            repository.create_rename_operation(
                                allowed_root=music,
                                items=(valid, invalid),
                            )
                        connection = repository._connection
                        self.assertEqual(connection.execute("SELECT COUNT(*) FROM operations").fetchone()[0], 0)
                        self.assertEqual(connection.execute("SELECT COUNT(*) FROM operation_items").fetchone()[0], 0)
                    finally:
                        repository.close()

    @unittest.skipUnless(os.name == "nt", "Windows 等价路径规则")
    def test_windows_equivalent_duplicate_targets_and_cross_operation_active_target_are_rejected(self) -> None:
        repository = self.create_repository()
        first_path, first = self.add_audio(repository, "First.mp3")
        second_path, second = self.add_audio(repository, "Second.mp3")
        equivalent = Path(os.fspath(self.music_root / "DUPLICATE.MP3").swapcase().replace("\\", "/"))

        with self.assertRaises(RepositoryDataError):
            self.create_operation(
                repository,
                self.plan(first, first_path, self.music_root / "duplicate.mp3"),
                self.plan(second, second_path, equivalent),
            )
        self.assertEqual(repository._connection.execute("SELECT COUNT(*) FROM operations").fetchone()[0], 0)

        operation, _items = self.create_operation(
            repository,
            self.plan(first, first_path, self.music_root / "duplicate.mp3"),
        )
        self.assertEqual(operation.status, "planned")
        with self.assertRaises(sqlite3.IntegrityError):
            self.create_operation(
                repository,
                self.plan(second, second_path, equivalent),
            )
        self.assertEqual(repository._connection.execute("SELECT COUNT(*) FROM operations").fetchone()[0], 1)

    def test_active_partial_uniques_block_asset_and_target_then_release_after_terminal(self) -> None:
        repository = self.create_repository()
        first_path, first = self.add_audio(repository, "partial-first.mp3")
        second_path, second = self.add_audio(repository, "partial-second.mp3")
        target = self.music_root / "shared-target.mp3"
        first_operation, first_items = self.create_operation(
            repository,
            self.plan(first, first_path, target),
        )

        with self.assertRaises(sqlite3.IntegrityError):
            self.create_operation(
                repository,
                self.plan(first, first_path, self.music_root / "other-target.mp3"),
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.create_operation(
                repository,
                self.plan(second, second_path, target),
            )
        self.assertEqual(repository._connection.execute("SELECT COUNT(*) FROM operations").fetchone()[0], 1)

        repository.start_rename_operation(first_operation.id)
        repository.record_rename_item_outcome(
            first_operation.id,
            first_items[0].id,
            result="cancelled",
            actual_path=None,
            error_code=None,
            error_message=None,
        )
        self.assertEqual(repository.finish_rename_operation(first_operation.id).status, "cancelled")

        replacement, replacement_items = self.create_operation(
            repository,
            self.plan(first, first_path, target),
        )
        self.assertEqual(replacement.status, "planned")
        self.assertEqual(replacement_items[0].result, "planned")

    def test_extension_comparison_is_case_insensitive_for_same_format(self) -> None:
        repository = self.create_repository()
        source, asset = self.add_audio(repository, "same-format.MP3")

        operation, items = self.create_operation(
            repository,
            self.plan(asset, source, self.music_root / "renamed.mp3"),
        )

        self.assertEqual(operation.status, "planned")
        self.assertEqual(items[0].target_path.suffix, ".mp3")

    def test_state_machine_and_finish_aggregation_are_strict(self) -> None:
        repository = self.create_repository()
        paths_and_assets = [self.add_audio(repository, f"{index}.mp3") for index in range(3)]
        operation, items = self.create_operation(
            repository,
            *(
                self.plan(asset, path, self.music_root / f"{index}-new.mp3")
                for index, (path, asset) in enumerate(paths_and_assets)
            ),
        )
        with self.assertRaises(RepositoryDataError):
            repository.start_rename_item(operation.id, items[0].id)
        running = repository.start_rename_operation(operation.id)
        self.assertEqual(running.status, "running")
        with self.assertRaises(RepositoryDataError):
            repository.start_rename_operation(operation.id)

        repository.start_rename_item(operation.id, items[0].id)
        repository.commit_rename_item(operation.id, items[0].id)
        repository.start_rename_item(operation.id, items[1].id)
        failed = repository.record_rename_item_outcome(
            operation.id,
            items[1].id,
            result="failed",
            actual_path=paths_and_assets[1][0],
            error_code="rename_failed",
            error_message="fixture failure",
        )
        self.assertEqual(failed.result, "failed")
        cancelled = repository.record_rename_item_outcome(
            operation.id,
            items[2].id,
            result="cancelled",
            actual_path=None,
            error_code=None,
            error_message=None,
        )
        self.assertEqual(cancelled.result, "cancelled")
        final = repository.finish_rename_operation(operation.id)
        self.assertEqual(final.status, "partial")
        self.assertEqual((final.success_count, final.failure_count), (1, 1))
        self.assertIsNotNone(final.completed_at)
        with self.assertRaises(RepositoryDataError):
            repository.finish_rename_operation(operation.id)
        with self.assertRaises(RepositoryDataError):
            repository.record_rename_item_outcome(
                operation.id,
                items[1].id,
                result="failed",
                actual_path=None,
                error_code="again",
                error_message="again",
            )

    def test_finish_refuses_running_or_planned_items(self) -> None:
        repository = self.create_repository()
        path, asset = self.add_audio(repository, "unfinished.mp3")
        operation, items = self.create_operation(
            repository,
            self.plan(asset, path, self.music_root / "finished.mp3"),
        )
        repository.start_rename_operation(operation.id)
        with self.assertRaises(RepositoryDataError):
            repository.finish_rename_operation(operation.id)
        repository.start_rename_item(operation.id, items[0].id)
        with self.assertRaises(RepositoryDataError):
            repository.finish_rename_operation(operation.id)
        self.assertEqual(repository.get_rename_operation(operation.id).status, "running")  # type: ignore[union-attr]

    def test_finish_aggregation_covers_success_failed_cancelled_and_mixed(self) -> None:
        repository = self.create_repository()

        def new_operation(prefix: str, count: int = 1):
            pairs = [self.add_audio(repository, f"{prefix}-{index}.mp3") for index in range(count)]
            operation, items = self.create_operation(
                repository,
                *(
                    self.plan(asset, path, self.music_root / f"{prefix}-{index}-new.mp3")
                    for index, (path, asset) in enumerate(pairs)
                ),
            )
            repository.start_rename_operation(operation.id)
            return operation, items

        success_operation, success_items = new_operation("success")
        repository.start_rename_item(success_operation.id, success_items[0].id)
        repository.commit_rename_item(success_operation.id, success_items[0].id)
        success = repository.finish_rename_operation(success_operation.id)
        self.assertEqual((success.status, success.success_count, success.failure_count), ("success", 1, 0))

        failed_operation, failed_items = new_operation("failed")
        repository.start_rename_item(failed_operation.id, failed_items[0].id)
        repository.record_rename_item_outcome(
            failed_operation.id,
            failed_items[0].id,
            result="rollback_failed",
            actual_path=None,
            error_code="rollback_failed",
            error_message="fixture",
        )
        failed = repository.finish_rename_operation(failed_operation.id)
        self.assertEqual((failed.status, failed.success_count, failed.failure_count), ("failed", 0, 1))

        cancelled_operation, cancelled_items = new_operation("cancelled")
        repository.record_rename_item_outcome(
            cancelled_operation.id,
            cancelled_items[0].id,
            result="cancelled",
            actual_path=None,
            error_code=None,
            error_message=None,
        )
        cancelled = repository.finish_rename_operation(cancelled_operation.id)
        self.assertEqual((cancelled.status, cancelled.success_count, cancelled.failure_count), ("cancelled", 0, 0))

        mixed_operation, mixed_items = new_operation("mixed", count=2)
        repository.start_rename_item(mixed_operation.id, mixed_items[0].id)
        repository.commit_rename_item(mixed_operation.id, mixed_items[0].id)
        repository.record_rename_item_outcome(
            mixed_operation.id,
            mixed_items[1].id,
            result="cancelled",
            actual_path=None,
            error_code=None,
            error_message=None,
        )
        mixed = repository.finish_rename_operation(mixed_operation.id)
        self.assertEqual((mixed.status, mixed.success_count, mixed.failure_count), ("partial", 1, 0))

    def test_non_cancelled_outcome_requires_a_running_item(self) -> None:
        repository = self.create_repository()
        source, asset = self.add_audio(repository, "planned.mp3")
        operation, items = self.create_operation(
            repository,
            self.plan(asset, source, self.music_root / "planned-new.mp3"),
        )
        repository.start_rename_operation(operation.id)

        with self.assertRaises(RepositoryDataError):
            repository.record_rename_item_outcome(
                operation.id,
                items[0].id,
                result="failed",
                actual_path=source,
                error_code="not_started",
                error_message="item was not started",
            )

        self.assertEqual(repository.get_rename_operation_item(items[0].id).result, "planned")  # type: ignore[union-attr]
        self.assertEqual(repository.get_rename_operation(operation.id).failure_count, 0)  # type: ignore[union-attr]

    def test_success_commit_updates_asset_item_and_counter_atomically(self) -> None:
        repository = self.create_repository()
        source, asset = self.add_audio(repository, "old.mp3")
        target = self.music_root / "new.mp3"
        operation, items = self.create_operation(repository, self.plan(asset, source, target))
        repository.start_rename_operation(operation.id)
        repository.start_rename_item(operation.id, items[0].id)

        updated_asset, updated_item = repository.commit_rename_item(operation.id, items[0].id)

        self.assertEqual(updated_asset.id, asset.id)
        self.assertEqual(updated_asset.canonical_path, target)
        self.assertEqual(updated_asset.file_name, "new.mp3")
        self.assertEqual(updated_item.result, "success")
        self.assertEqual(updated_item.after["canonical_path"], os.fspath(target))
        self.assertEqual(repository.get_rename_operation(operation.id).success_count, 1)  # type: ignore[union-attr]
        self.assertTrue(source.is_file(), "repository 不得实际重命名媒体文件")
        self.assertFalse(target.exists())

    def test_pre_commit_failure_rolls_back_asset_item_and_counter(self) -> None:
        repository = self.create_repository()
        source, asset = self.add_audio(repository, "rollback.mp3")
        target = self.music_root / "target.mp3"
        operation, items = self.create_operation(repository, self.plan(asset, source, target))
        repository.start_rename_operation(operation.id)
        repository.start_rename_item(operation.id, items[0].id)
        repository._connection.execute(
            """
            CREATE TRIGGER reject_item_success
            BEFORE UPDATE OF result ON operation_items
            WHEN NEW.result = 'success'
            BEGIN
                SELECT RAISE(ABORT, 'simulated pre-commit failure');
            END
            """
        )

        with self.assertRaises(sqlite3.IntegrityError):
            repository.commit_rename_item(operation.id, items[0].id)

        self.assertEqual(repository.get_asset_by_id(asset.id).canonical_path, source)  # type: ignore[union-attr]
        self.assertEqual(repository.get_rename_operation_item(items[0].id).result, "running")  # type: ignore[union-attr]
        self.assertEqual(repository.get_rename_operation(operation.id).success_count, 0)  # type: ignore[union-attr]

    def test_post_commit_proxy_raises_unknown_and_readback_proves_commit(self) -> None:
        repository = self.create_repository()
        source, asset = self.add_audio(repository, "unknown.mp3")
        target = self.music_root / "committed.mp3"
        operation, items = self.create_operation(repository, self.plan(asset, source, target))
        repository.start_rename_operation(operation.id)
        repository.start_rename_item(operation.id, items[0].id)
        connection = repository._connection

        class CommitThenRaiseProxy:
            @property
            def in_transaction(self):
                return connection.in_transaction

            def execute(self, sql, *args):
                result = connection.execute(sql, *args)
                if sql == "COMMIT":
                    raise sqlite3.OperationalError("simulated transport error after COMMIT")
                return result

            def close(self):
                return connection.close()

        repository._connection = CommitThenRaiseProxy()  # type: ignore[assignment]
        with self.assertRaises(RepositoryCommitOutcomeUnknown) as raised:
            repository.commit_rename_item(operation.id, items[0].id)

        self.assertEqual(raised.exception.operation_id, operation.id)
        self.assertEqual(raised.exception.item_id, items[0].id)
        self.assertEqual(repository.get_asset_by_id(asset.id).canonical_path, target)  # type: ignore[union-attr]
        self.assertEqual(repository.get_rename_operation_item(items[0].id).result, "success")  # type: ignore[union-attr]
        self.assertEqual(repository.get_rename_operation(operation.id).success_count, 1)  # type: ignore[union-attr]

    def test_new_methods_enforce_thread_ownership_and_closed_state(self) -> None:
        repository = self.create_repository()
        source, asset = self.add_audio(repository, "thread.mp3")
        operation, items = self.create_operation(
            repository,
            self.plan(asset, source, self.music_root / "thread-new.mp3"),
        )
        errors: list[type[BaseException]] = []

        def misuse() -> None:
            calls = (
                lambda: self.create_operation(
                    repository,
                    self.plan(asset, source, self.music_root / "thread-other.mp3"),
                ),
                lambda: repository.get_asset_by_id(asset.id),
                lambda: repository.get_rename_operation(operation.id),
                lambda: repository.get_rename_operation_item(items[0].id),
                lambda: repository.list_rename_operation_items(operation.id),
                lambda: repository.start_rename_operation(operation.id),
                lambda: repository.start_rename_item(operation.id, items[0].id),
                lambda: repository.commit_rename_item(operation.id, items[0].id),
                lambda: repository.record_rename_item_outcome(
                    operation.id,
                    items[0].id,
                    result="cancelled",
                    actual_path=None,
                    error_code=None,
                    error_message=None,
                ),
                lambda: repository.finish_rename_operation(operation.id),
                repository.close,
            )
            for call in calls:
                try:
                    call()
                except BaseException as exc:
                    errors.append(type(exc))

        thread = threading.Thread(target=misuse)
        thread.start()
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [RepositoryThreadError] * 11)

        repository.close()
        for call in (
            lambda: repository.get_asset_by_id(asset.id),
            lambda: repository.get_rename_operation(operation.id),
            lambda: repository.list_rename_operation_items(operation.id),
        ):
            with self.assertRaises(RepositoryClosedError):
                call()

    def test_repository_never_modifies_temporary_media_files(self) -> None:
        repository = self.create_repository()
        source, asset = self.add_audio(repository, "untouched.mp3", content=b"untouched bytes")
        before = self.media_snapshot(self.music_root)
        operation, items = self.create_operation(
            repository,
            self.plan(asset, source, self.music_root / "planned.mp3"),
        )
        repository.start_rename_operation(operation.id)
        repository.start_rename_item(operation.id, items[0].id)
        repository.record_rename_item_outcome(
            operation.id,
            items[0].id,
            result="rolled_back",
            actual_path=source,
            error_code="fixture",
            error_message="fixture",
        )
        repository.finish_rename_operation(operation.id)
        self.assertEqual(self.media_snapshot(self.music_root), before)


if __name__ == "__main__":
    unittest.main()
