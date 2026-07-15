from __future__ import annotations

import math
import os
from pathlib import Path
import sqlite3
import threading
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from database import DatabaseConfig, open_database
from repositories.library_repository import (
    AssetUpsert,
    IndexBatchItem,
    LibraryRepository,
    RecordNotFoundError,
    RepositoryClosedError,
    RepositoryDataError,
    RepositoryPathError,
    RepositoryThreadError,
    ScanItemInput,
)


class LibraryRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.database_path = self.root / "library.sqlite3"
        self.config = DatabaseConfig(self.database_path, timeout_seconds=1.0, busy_timeout_ms=1000)

    def create_repository(self) -> LibraryRepository:
        repository = LibraryRepository(self.config)

        def close_if_open() -> None:
            try:
                repository.close()
            except RepositoryClosedError:
                pass

        self.addCleanup(close_if_open)
        return repository

    def audio(self, name: str, *, size: int = 1, **overrides) -> AssetUpsert:
        return AssetUpsert(
            canonical_path=self.root / "music" / name,
            size_bytes=size,
            mtime_ns=overrides.get("mtime_ns"),
            kind=overrides.get("kind", "audio"),
            file_state=overrides.get("file_state", "active"),
        )

    def index_completed_session(
        self,
        repository: LibraryRepository,
        scan_root: Path,
        entries: tuple[tuple[str, int, int | None], ...],
    ):
        session = repository.create_scan_session(mode="audio", source_folder=scan_root)
        if entries:
            repository.index_scan_batch(
                session.id,
                tuple(
                    IndexBatchItem(scan_root / name, size, mtime_ns)
                    for name, size, mtime_ns in entries
                ),
            )
        result = repository.complete_scan_and_reconcile(session.id)
        return session, result

    def test_constructor_migrates_only_the_injected_temporary_database(self) -> None:
        project_root = Path(__file__).resolve().parents[1]
        patterns = ("*.db", "*.sqlite", "*.sqlite3", "*.backup.sqlite3")
        before = {path for pattern in patterns for path in project_root.glob(pattern)}

        repository = self.create_repository()

        self.assertEqual(repository.list_assets(), ())
        self.assertTrue(self.database_path.is_file())
        after = {path for pattern in patterns for path in project_root.glob(pattern)}
        self.assertEqual(after, before)

    def test_asset_rejects_relative_path_without_resolving_paths(self) -> None:
        repository = self.create_repository()
        with self.assertRaisesRegex(RepositoryPathError, "绝对路径"):
            repository.upsert_asset(AssetUpsert(Path("relative.mp3"), 1))

        absolute = self.root / "music" / "song.MP3"
        with patch.object(Path, "resolve", side_effect=AssertionError("resolve must not run")):
            record = repository.upsert_asset(AssetUpsert(absolute, 12, mtime_ns=34))

        expected = Path(os.path.normpath(os.path.abspath(os.fspath(absolute))))
        self.assertEqual(record.canonical_path, expected)
        self.assertEqual(record.file_name, "song.MP3")
        self.assertEqual(record.extension, ".mp3")
        self.assertEqual(record.normalized_path, os.path.normcase(str(expected)).replace("\\", "/"))

    @unittest.skipUnless(os.name == "nt", "Windows 路径大小写等价规则")
    def test_equivalent_windows_paths_upsert_the_same_id_and_keep_created_at(self) -> None:
        repository = self.create_repository()
        original = self.root / "Music" / "Track.MP3"
        equivalent = Path(os.fspath(original).swapcase().replace("\\", "/"))

        first = repository.upsert_asset(AssetUpsert(original, 10, mtime_ns=100))
        second = repository.upsert_asset(
            AssetUpsert(equivalent, 20, mtime_ns=200, file_state="external_changed")
        )

        self.assertEqual(second.id, first.id)
        self.assertEqual(second.created_at, first.created_at)
        self.assertEqual(second.size_bytes, 20)
        self.assertEqual(second.mtime_ns, 200)
        self.assertEqual(second.file_state, "external_changed")
        self.assertEqual(len(repository.list_assets()), 1)
        self.assertEqual(repository.get_asset_by_path(original), second)

    def test_asset_listing_is_stable_and_supports_kind_and_state_filters(self) -> None:
        repository = self.create_repository()
        repository.upsert_assets(
            (
                self.audio("z.mp3", file_state="missing"),
                self.audio("A.FLAC"),
                self.audio("middle.lrc", kind="lyric"),
            )
        )

        all_records = repository.list_assets()
        self.assertEqual(
            [record.normalized_path for record in all_records],
            sorted(record.normalized_path for record in all_records),
        )
        self.assertEqual([record.kind for record in repository.list_assets(kind="lyric")], ["lyric"])
        self.assertEqual(
            [record.file_state for record in repository.list_assets(file_state="missing")],
            ["missing"],
        )

    def test_asset_batch_validation_failure_writes_nothing(self) -> None:
        repository = self.create_repository()
        with self.assertRaises(RepositoryDataError):
            repository.upsert_assets((self.audio("valid.mp3"), self.audio("bad.mp3", size=-1)))
        self.assertEqual(repository.list_assets(), ())

    def test_asset_batch_database_failure_rolls_back_prior_rows(self) -> None:
        repository = self.create_repository()
        connection = open_database(self.config)
        try:
            connection.execute(
                """
                CREATE TRIGGER reject_test_asset
                BEFORE INSERT ON assets
                WHEN NEW.file_name = 'reject.mp3'
                BEGIN
                    SELECT RAISE(ABORT, 'simulated asset failure');
                END
                """
            )
        finally:
            connection.close()

        with self.assertRaises(sqlite3.IntegrityError):
            repository.upsert_assets((self.audio("first.mp3"), self.audio("reject.mp3")))

        self.assertEqual(repository.list_assets(), ())

    def test_commit_failure_rolls_back_and_leaves_connection_usable(self) -> None:
        repository = self.create_repository()
        connection = repository._connection  # 定向验证事务恢复，不扩大生产 API

        class FailCommitProxy:
            @property
            def in_transaction(self):
                return connection.in_transaction

            def execute(self, sql, *args):
                if sql == "COMMIT":
                    raise sqlite3.OperationalError("simulated commit failure")
                return connection.execute(sql, *args)

            def close(self):
                return connection.close()

        repository._connection = FailCommitProxy()  # type: ignore[assignment]
        with self.assertRaisesRegex(sqlite3.OperationalError, "commit failure"):
            repository.set_setting("must.rollback", {"value": 1})

        self.assertFalse(connection.in_transaction)
        self.assertIsNone(repository.get_setting("must.rollback"))

    def test_settings_round_trip_unicode_nested_values_and_distinguish_null(self) -> None:
        repository = self.create_repository()
        value = {"目录": ["音乐", True, 3, {"比例": 1.5}]}

        saved = repository.set_setting("scan.options", value)

        self.assertEqual(saved.value, value)
        self.assertEqual(repository.get_setting("scan.options").value, value)  # type: ignore[union-attr]
        self.assertIsNone(repository.get_setting("missing"))
        null_record = repository.set_setting("nullable", None)
        self.assertIsNone(null_record.value)
        self.assertIsNone(repository.get_setting("nullable").value)  # type: ignore[union-attr]

    def test_settings_reject_non_json_values_nan_and_infinity_without_writing(self) -> None:
        repository = self.create_repository()
        for key, value in (
            ("set", {1, 2}),
            ("nan", math.nan),
            ("infinity", {"value": math.inf}),
        ):
            with self.subTest(key=key):
                with self.assertRaises(RepositoryDataError):
                    repository.set_setting(key, value)
                self.assertIsNone(repository.get_setting(key))

    def test_corrupt_or_non_standard_json_raises_repository_data_error(self) -> None:
        repository = self.create_repository()
        repository.set_setting("corrupt", {"valid": True})
        repository.close()

        connection = open_database(self.config)
        try:
            connection.execute(
                "UPDATE settings SET value_json = 'NaN' WHERE key = 'corrupt'"
            )
        finally:
            connection.close()

        reopened = self.create_repository()
        with self.assertRaisesRegex(RepositoryDataError, "JSON"):
            reopened.get_setting("corrupt")

    def test_scan_session_create_get_finish_and_state_validation(self) -> None:
        repository = self.create_repository()
        source = self.root / "scan"

        session = repository.create_scan_session(mode="audio", source_folder=source)

        self.assertEqual(session.status, "running")
        self.assertEqual(repository.get_scan_session(session.id), session)
        finished = repository.finish_scan_session(session.id, status="completed")
        self.assertEqual(finished.status, "completed")
        self.assertIsNotNone(finished.completed_at)
        with self.assertRaises(RepositoryDataError):
            repository.create_scan_session(mode="video", source_folder=source)
        with self.assertRaises(RepositoryDataError):
            repository.finish_scan_session(session.id, status="running")
        with self.assertRaises(RepositoryDataError):
            repository.finish_scan_session(session.id, status="failed")
        with self.assertRaises(RecordNotFoundError):
            repository.finish_scan_session("missing", status="failed")

    def test_scan_items_are_atomic_and_returned_in_stable_order(self) -> None:
        repository = self.create_repository()
        session = repository.create_scan_session(mode="audio", source_folder=self.root / "scan")
        beta = self.root / "scan" / "beta.mp3"
        alpha = self.root / "scan" / "Alpha.mp3"

        records = repository.add_scan_items(
            session.id,
            (
                ScanItemInput(beta, 2, status="indexed"),
                ScanItemInput(alpha, None, status="skipped", reason="unsupported metadata"),
            ),
        )

        self.assertEqual(len(records), 2)
        listed = repository.list_scan_items(session.id)
        self.assertEqual(
            [item.source_path.name.casefold() for item in listed],
            ["alpha.mp3", "beta.mp3"],
        )
        self.assertEqual(listed[0].reason, "unsupported metadata")
        with self.assertRaises(RepositoryDataError):
            repository.add_scan_items(
                session.id,
                (ScanItemInput(self.root / "scan" / "bad.mp3", 1, status="duplicate"),),
            )

    def test_index_scan_batch_writes_matching_assets_and_items_together(self) -> None:
        repository = self.create_repository()
        session = repository.create_scan_session(mode="audio", source_folder=self.root / "scan")
        source = self.root / "scan" / "song.MP3"

        records = repository.index_scan_batch(
            session.id,
            (IndexBatchItem(source, 12, 34),),
        )

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].asset.id, repository.get_asset_by_path(source).id)  # type: ignore[union-attr]
        self.assertEqual(records[0].asset.mtime_ns, 34)
        self.assertEqual(records[0].scan_item.status, "indexed")
        self.assertEqual(records[0].scan_item.source_path.as_posix(), records[0].asset.normalized_path)

    def test_index_scan_batch_derives_file_state_from_previous_fingerprint(self) -> None:
        repository = self.create_repository()
        scan_root = self.root / "scan"
        existing = {
            "same.mp3": (10, 100, "active"),
            "size.mp3": (10, 100, "active"),
            "mtime.mp3": (10, 100, "active"),
            "both.mp3": (10, 100, "active"),
            "returns.mp3": (10, 100, "missing"),
        }
        repository.upsert_assets(
            tuple(
                AssetUpsert(scan_root / name, size, mtime, file_state=state)
                for name, (size, mtime, state) in existing.items()
            )
        )
        session = repository.create_scan_session(mode="audio", source_folder=scan_root)

        records = repository.index_scan_batch(
            session.id,
            (
                IndexBatchItem(scan_root / "new.mp3", 1, None),
                IndexBatchItem(scan_root / "same.mp3", 10, 100),
                IndexBatchItem(scan_root / "size.mp3", 11, 100),
                IndexBatchItem(scan_root / "mtime.mp3", 10, 101),
                IndexBatchItem(scan_root / "both.mp3", 11, 101),
                IndexBatchItem(scan_root / "returns.mp3", 10, 100),
            ),
        )

        states = {record.asset.file_name: record.asset.file_state for record in records}
        self.assertEqual(states["new.mp3"], "active")
        self.assertEqual(states["same.mp3"], "active")
        self.assertEqual(states["returns.mp3"], "active")
        self.assertEqual(states["size.mp3"], "external_changed")
        self.assertEqual(states["mtime.mp3"], "external_changed")
        self.assertEqual(states["both.mp3"], "external_changed")

    def test_generic_asset_upsert_keeps_explicit_state_behavior(self) -> None:
        repository = self.create_repository()
        source = self.root / "music" / "explicit.mp3"

        first = repository.upsert_asset(AssetUpsert(source, 1, 1, file_state="missing"))
        second = repository.upsert_asset(
            AssetUpsert(source, 2, 2, file_state="external_changed")
        )

        self.assertEqual(first.file_state, "missing")
        self.assertEqual(second.file_state, "external_changed")

    def test_index_scan_batch_rejects_root_escape_and_rolls_back_whole_batch(self) -> None:
        repository = self.create_repository()
        scan_root = self.root / "Root"
        session = repository.create_scan_session(mode="audio", source_folder=scan_root)

        with self.assertRaisesRegex(RepositoryPathError, "扫描根目录"):
            repository.index_scan_batch(
                session.id,
                (
                    IndexBatchItem(scan_root / "inside.mp3", 1, 1),
                    IndexBatchItem(self.root / "Root2" / "outside.mp3", 1, 1),
                ),
            )

        self.assertEqual(repository.list_assets(), ())
        self.assertEqual(repository.list_scan_items(session.id), ())

    def test_reconcile_marks_only_missing_active_asset_and_preserves_identity(self) -> None:
        repository = self.create_repository()
        scan_root = self.root / "scan"
        self.index_completed_session(
            repository,
            scan_root,
            (("kept.mp3", 1, 1), ("gone.mp3", 2, 2)),
        )
        gone_before = repository.get_asset_by_path(scan_root / "gone.mp3")
        self.assertIsNotNone(gone_before)

        _, result = self.index_completed_session(
            repository,
            scan_root,
            (("kept.mp3", 1, 1),),
        )

        gone_after = repository.get_asset_by_path(scan_root / "gone.mp3")
        self.assertIsNotNone(gone_after)
        self.assertEqual(gone_after.id, gone_before.id)  # type: ignore[union-attr]
        self.assertEqual(gone_after.file_state, "missing")  # type: ignore[union-attr]
        self.assertEqual(result.seen_count, 1)
        self.assertEqual(result.active_count, 1)
        self.assertEqual(result.external_changed_count, 0)
        self.assertEqual(result.missing_count, 1)
        self.assertEqual(len(repository.list_assets()), 2)

    def test_reconcile_does_not_replace_external_changed_with_missing(self) -> None:
        repository = self.create_repository()
        scan_root = self.root / "scan"
        self.index_completed_session(repository, scan_root, (("changed.mp3", 1, 1),))
        repository.upsert_asset(
            AssetUpsert(
                scan_root / "changed.mp3",
                2,
                2,
                file_state="external_changed",
            )
        )

        _, result = self.index_completed_session(repository, scan_root, ())

        self.assertEqual(result.missing_count, 0)
        self.assertEqual(
            repository.get_asset_by_path(scan_root / "changed.mp3").file_state,  # type: ignore[union-attr]
            "external_changed",
        )

    def test_reconcile_requires_running_audio_session(self) -> None:
        repository = self.create_repository()
        lyric = repository.create_scan_session(mode="lyric", source_folder=self.root / "lyrics")
        with self.assertRaisesRegex(RepositoryDataError, "音频扫描"):
            repository.complete_scan_and_reconcile(lyric.id)
        self.assertEqual(repository.get_scan_session(lyric.id).status, "running")  # type: ignore[union-attr]

        audio = repository.create_scan_session(mode="audio", source_folder=self.root / "audio")
        repository.finish_scan_session(audio.id, status="cancelled")
        with self.assertRaisesRegex(RepositoryDataError, "已经结束"):
            repository.complete_scan_and_reconcile(audio.id)

    def test_reconcile_uses_latest_successful_exact_root_only(self) -> None:
        repository = self.create_repository()
        root = self.root / "Root"
        root2 = self.root / "Root2"
        nested = root / "Nested"
        self.index_completed_session(repository, root, (("old.mp3", 1, 1),))
        self.index_completed_session(repository, root, (("latest.mp3", 1, 1),))
        self.index_completed_session(repository, root2, (("other.mp3", 1, 1),))
        self.index_completed_session(repository, nested, (("nested.mp3", 1, 1),))
        repository.upsert_asset(AssetUpsert(root / "old.mp3", 1, 1, file_state="active"))

        self.index_completed_session(repository, root, ())

        self.assertEqual(
            repository.get_asset_by_path(root / "latest.mp3").file_state,  # type: ignore[union-attr]
            "missing",
        )
        self.assertEqual(
            repository.get_asset_by_path(root / "old.mp3").file_state,  # type: ignore[union-attr]
            "active",
        )
        self.assertEqual(
            repository.get_asset_by_path(root2 / "other.mp3").file_state,  # type: ignore[union-attr]
            "active",
        )
        self.assertEqual(
            repository.get_asset_by_path(nested / "nested.mp3").file_state,  # type: ignore[union-attr]
            "active",
        )

    @unittest.skipUnless(os.name == "nt", "Windows 根目录规范化规则")
    def test_reconcile_matches_equivalent_windows_root_and_rejects_other_drive(self) -> None:
        repository = self.create_repository()
        scan_root = self.root / "CaseRoot"
        equivalent_root = Path(os.fspath(scan_root).swapcase().replace("\\", "/"))
        self.index_completed_session(repository, scan_root, (("song.mp3", 1, 1),))

        current = repository.create_scan_session(mode="audio", source_folder=equivalent_root)
        result = repository.complete_scan_and_reconcile(current.id)

        self.assertEqual(result.missing_count, 1)
        self.assertEqual(
            repository.get_asset_by_path(scan_root / "song.mp3").file_state,  # type: ignore[union-attr]
            "missing",
        )

        current_drive = scan_root.drive.casefold()
        other_drive = next(
            drive for drive in ("Z:", "Y:", "X:") if drive.casefold() != current_drive
        )
        escaped = Path(other_drive + "\\escape.mp3")
        cross_drive = repository.create_scan_session(mode="audio", source_folder=scan_root)
        with self.assertRaisesRegex(RepositoryPathError, "扫描根目录"):
            repository.index_scan_batch(
                cross_drive.id,
                (IndexBatchItem(escaped, 1, 1),),
            )
        self.assertEqual(repository.list_scan_items(cross_drive.id), ())

    def test_cancelled_and_failed_sessions_are_not_reconcile_baselines(self) -> None:
        repository = self.create_repository()
        scan_root = self.root / "scan"
        self.index_completed_session(repository, scan_root, (("successful.mp3", 1, 1),))
        for status, name in (("cancelled", "cancelled.mp3"), ("failed", "failed.mp3")):
            session = repository.create_scan_session(mode="audio", source_folder=scan_root)
            repository.index_scan_batch(
                session.id,
                (IndexBatchItem(scan_root / name, 1, 1),),
            )
            repository.finish_scan_session(session.id, status=status)

        self.index_completed_session(repository, scan_root, ())

        self.assertEqual(
            repository.get_asset_by_path(scan_root / "successful.mp3").file_state,  # type: ignore[union-attr]
            "missing",
        )
        for name in ("cancelled.mp3", "failed.mp3"):
            self.assertEqual(
                repository.get_asset_by_path(scan_root / name).file_state,  # type: ignore[union-attr]
                "active",
            )

    def test_reconcile_fails_closed_for_out_of_root_current_or_history_item(self) -> None:
        for corrupt_current in (False, True):
            with self.subTest(corrupt_current=corrupt_current):
                repository = self.create_repository()
                scan_root = self.root / ("current" if corrupt_current else "history")
                baseline, _ = self.index_completed_session(
                    repository,
                    scan_root,
                    (("song.mp3", 1, 1),),
                )
                current = repository.create_scan_session(mode="audio", source_folder=scan_root)
                if corrupt_current:
                    repository.index_scan_batch(
                        current.id,
                        (IndexBatchItem(scan_root / "current.mp3", 1, 1),),
                    )
                    target_session = current.id
                else:
                    target_session = baseline.id
                repository._connection.execute(
                    "UPDATE scan_items SET source_path = ? WHERE session_id = ?",
                    (str(self.root / "outside" / "escape.mp3"), target_session),
                )

                with self.assertRaisesRegex(RepositoryPathError, "扫描根目录"):
                    repository.complete_scan_and_reconcile(current.id)

                self.assertEqual(repository.get_scan_session(current.id).status, "running")  # type: ignore[union-attr]
                repository.close()

    def test_reconcile_sql_failure_rolls_back_missing_and_completed_state(self) -> None:
        repository = self.create_repository()
        scan_root = self.root / "scan"
        self.index_completed_session(repository, scan_root, (("song.mp3", 1, 1),))
        current = repository.create_scan_session(mode="audio", source_folder=scan_root)
        repository._connection.execute(
            """
            CREATE TRIGGER fail_reconcile_update
            BEFORE UPDATE OF file_state ON assets
            WHEN NEW.file_state = 'missing'
            BEGIN
                SELECT RAISE(ABORT, 'reconcile update failure');
            END
            """
        )

        with self.assertRaisesRegex(sqlite3.IntegrityError, "reconcile update failure"):
            repository.complete_scan_and_reconcile(current.id)

        self.assertEqual(repository.get_scan_session(current.id).status, "running")  # type: ignore[union-attr]
        self.assertEqual(
            repository.get_asset_by_path(scan_root / "song.mp3").file_state,  # type: ignore[union-attr]
            "active",
        )

    def test_reconcile_commit_failure_rolls_back_missing_and_completed_state(self) -> None:
        repository = self.create_repository()
        scan_root = self.root / "scan"
        self.index_completed_session(repository, scan_root, (("song.mp3", 1, 1),))
        current = repository.create_scan_session(mode="audio", source_folder=scan_root)
        connection = repository._connection

        class FailCommitProxy:
            @property
            def in_transaction(self):
                return connection.in_transaction

            def execute(self, sql, *args):
                if sql == "COMMIT":
                    raise sqlite3.OperationalError("reconcile commit failure")
                return connection.execute(sql, *args)

            def executemany(self, sql, *args):
                return connection.executemany(sql, *args)

            def close(self):
                return connection.close()

        repository._connection = FailCommitProxy()  # type: ignore[assignment]
        with self.assertRaisesRegex(sqlite3.OperationalError, "reconcile commit failure"):
            repository.complete_scan_and_reconcile(current.id)

        self.assertEqual(repository.get_scan_session(current.id).status, "running")  # type: ignore[union-attr]
        self.assertEqual(
            repository.get_asset_by_path(scan_root / "song.mp3").file_state,  # type: ignore[union-attr]
            "active",
        )

    def test_index_scan_batch_scan_item_failure_rolls_back_assets_and_items(self) -> None:
        repository = self.create_repository()
        session = repository.create_scan_session(mode="audio", source_folder=self.root / "scan")
        connection = open_database(self.config)
        try:
            connection.execute(
                """
                CREATE TRIGGER reject_second_scan_item
                BEFORE INSERT ON scan_items
                WHEN NEW.source_path LIKE '%reject.mp3'
                BEGIN
                    SELECT RAISE(ABORT, 'simulated scan item failure');
                END
                """
            )
        finally:
            connection.close()

        with self.assertRaises(sqlite3.IntegrityError):
            repository.index_scan_batch(
                session.id,
                (
                    IndexBatchItem(self.root / "scan" / "first.mp3", 1, 1),
                    IndexBatchItem(self.root / "scan" / "reject.mp3", 2, 2),
                ),
            )

        self.assertEqual(repository.list_assets(), ())
        self.assertEqual(repository.list_scan_items(session.id), ())

    @unittest.skipUnless(os.name == "nt", "Windows 组合索引路径等价规则")
    def test_index_scan_batch_equivalent_paths_roll_back_without_half_rows(self) -> None:
        repository = self.create_repository()
        session = repository.create_scan_session(mode="audio", source_folder=self.root / "scan")
        original = self.root / "Scan" / "Song.MP3"
        equivalent = Path(os.fspath(original).swapcase().replace("\\", "/"))

        with self.assertRaises(sqlite3.IntegrityError):
            repository.index_scan_batch(
                session.id,
                (IndexBatchItem(original, 1, 1), IndexBatchItem(equivalent, 1, 1)),
            )

        self.assertEqual(repository.list_assets(), ())
        self.assertEqual(repository.list_scan_items(session.id), ())

    def test_index_scan_batch_commit_failure_rolls_back_assets_and_items(self) -> None:
        repository = self.create_repository()
        session = repository.create_scan_session(mode="audio", source_folder=self.root / "scan")
        connection = open_database(self.config)
        try:
            connection.execute("CREATE TABLE test_commit_parent(id TEXT PRIMARY KEY)")
            connection.execute(
                """
                CREATE TABLE test_commit_guard(
                    parent_id TEXT REFERENCES test_commit_parent(id)
                        DEFERRABLE INITIALLY DEFERRED
                )
                """
            )
            connection.execute(
                """
                CREATE TRIGGER fail_index_commit
                AFTER INSERT ON scan_items
                BEGIN
                    INSERT INTO test_commit_guard(parent_id) VALUES ('missing');
                END
                """
            )
        finally:
            connection.close()

        with self.assertRaises(sqlite3.IntegrityError):
            repository.index_scan_batch(
                session.id,
                (IndexBatchItem(self.root / "scan" / "commit.mp3", 1, 1),),
            )

        self.assertEqual(repository.list_assets(), ())
        self.assertEqual(repository.list_scan_items(session.id), ())
        connection = open_database(self.config)
        try:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM test_commit_guard").fetchone()[0], 0)
        finally:
            connection.close()

    def test_duplicate_scan_item_rolls_back_the_whole_batch(self) -> None:
        repository = self.create_repository()
        session = repository.create_scan_session(mode="audio", source_folder=self.root / "scan")
        source = self.root / "scan" / "same.mp3"

        with self.assertRaises(sqlite3.IntegrityError):
            repository.add_scan_items(
                session.id,
                (ScanItemInput(source, 1), ScanItemInput(source, 1)),
            )

        self.assertEqual(repository.list_scan_items(session.id), ())
        with self.assertRaises(RecordNotFoundError):
            repository.add_scan_items("missing", (ScanItemInput(source, 1),))

        repository.finish_scan_session(session.id, status="completed")
        with self.assertRaises(RepositoryDataError):
            repository.add_scan_items(
                session.id,
                (ScanItemInput(self.root / "scan" / "late.mp3", 1),),
            )

    @unittest.skipUnless(os.name == "nt", "Windows 扫描条目路径等价规则")
    def test_equivalent_scan_item_paths_cannot_bypass_uniqueness(self) -> None:
        repository = self.create_repository()
        session = repository.create_scan_session(mode="audio", source_folder=self.root / "scan")
        original = self.root / "Scan" / "Song.MP3"
        equivalent = Path(os.fspath(original).swapcase().replace("\\", "/"))

        with self.assertRaises(sqlite3.IntegrityError):
            repository.add_scan_items(
                session.id,
                (ScanItemInput(original, 1), ScanItemInput(equivalent, 1)),
            )

        self.assertEqual(repository.list_scan_items(session.id), ())

    def test_repository_rejects_cross_thread_use_and_close(self) -> None:
        repository = self.create_repository()
        errors: list[BaseException] = []

        def use_and_close() -> None:
            for operation in (repository.list_assets, repository.close):
                try:
                    operation()
                except BaseException as exc:
                    errors.append(exc)

        worker = threading.Thread(target=use_and_close)
        worker.start()
        worker.join(timeout=5)

        self.assertFalse(worker.is_alive())
        self.assertEqual(len(errors), 2)
        self.assertTrue(all(isinstance(error, RepositoryThreadError) for error in errors))
        self.assertEqual(repository.list_assets(), ())

    def test_latest_completed_audio_roots_choose_latest_stable_candidate_per_asset(self) -> None:
        repository = self.create_repository()
        root_a = self.root / "A"
        nested_a = root_a / "Nested"
        root_b = self.root / "B"
        nested_asset = repository.upsert_asset(AssetUpsert(nested_a / "song.mp3", 1, 1))
        a_asset = repository.upsert_asset(AssetUpsert(root_a / "a.mp3", 1, 1))
        b_asset = repository.upsert_asset(AssetUpsert(root_b / "b.mp3", 1, 1))
        no_history = repository.upsert_asset(AssetUpsert(root_b / "no-history.mp3", 1, 1))

        old_a, _ = self.index_completed_session(
            repository,
            root_a,
            (("Nested/song.mp3", 1, 1), ("a.mp3", 1, 1)),
        )
        b_session, _ = self.index_completed_session(repository, root_b, (("b.mp3", 1, 1),))
        latest_nested, _ = self.index_completed_session(repository, nested_a, (("song.mp3", 1, 1),))
        for session_id, day in ((old_a.id, 1), (b_session.id, 2), (latest_nested.id, 3)):
            repository._connection.execute(
                "UPDATE scan_sessions SET started_at = ?, completed_at = ? WHERE id = ?",
                (f"2026-01-0{day}T00:00:00Z", f"2026-01-0{day}T00:00:01Z", session_id),
            )

        roots = repository.latest_completed_audio_roots(
            (nested_asset.id, a_asset.id, b_asset.id, nested_asset.id, no_history.id)
        )

        self.assertEqual(roots[nested_asset.id], nested_a)
        self.assertEqual(roots[a_asset.id], root_a)
        self.assertEqual(roots[b_asset.id], root_b)
        self.assertNotIn(no_history.id, roots)
        self.assertEqual(set(roots), {nested_asset.id, a_asset.id, b_asset.id})

    def test_latest_completed_audio_roots_order_by_completed_started_then_id(self) -> None:
        repository = self.create_repository()
        broad_root = self.root / "order"
        narrow_root = broad_root / "nested"
        asset = repository.upsert_asset(AssetUpsert(narrow_root / "song.mp3", 1, 1))
        broad, _ = self.index_completed_session(
            repository,
            broad_root,
            (("nested/song.mp3", 1, 1),),
        )
        narrow, _ = self.index_completed_session(repository, narrow_root, (("song.mp3", 1, 1),))

        repository._connection.execute(
            "UPDATE scan_sessions SET started_at = ?, completed_at = ? WHERE id = ?",
            ("2026-01-01T00:00:00Z", "2026-01-03T00:00:00Z", broad.id),
        )
        repository._connection.execute(
            "UPDATE scan_sessions SET started_at = ?, completed_at = ? WHERE id = ?",
            ("2026-01-02T00:00:00Z", "2026-01-02T00:00:00Z", narrow.id),
        )
        self.assertEqual(repository.latest_completed_audio_roots((asset.id,))[asset.id], broad_root)

        repository._connection.execute(
            "UPDATE scan_sessions SET completed_at = ? WHERE id IN (?, ?)",
            ("2026-01-03T00:00:00Z", broad.id, narrow.id),
        )
        self.assertEqual(repository.latest_completed_audio_roots((asset.id,))[asset.id], narrow_root)

        repository._connection.execute(
            "UPDATE scan_sessions SET started_at = ? WHERE id IN (?, ?)",
            ("2026-01-02T00:00:00Z", broad.id, narrow.id),
        )
        expected_root = narrow_root if narrow.id > broad.id else broad_root
        self.assertEqual(repository.latest_completed_audio_roots((asset.id,))[asset.id], expected_root)

    def test_latest_completed_audio_roots_filter_mode_terminal_item_status_and_kind(self) -> None:
        repository = self.create_repository()
        root = self.root / "filters"
        paths = {
            name: root / f"{name}.mp3"
            for name in ("cancelled", "failed", "running", "lyric", "waiting", "valid")
        }
        assets = {
            name: repository.upsert_asset(AssetUpsert(path, 1, 1))
            for name, path in paths.items()
        }

        for status in ("cancelled", "failed"):
            session = repository.create_scan_session(mode="audio", source_folder=root)
            repository.index_scan_batch(session.id, (IndexBatchItem(paths[status], 1, 1),))
            repository.finish_scan_session(session.id, status=status)
        running = repository.create_scan_session(mode="audio", source_folder=root)
        repository.index_scan_batch(running.id, (IndexBatchItem(paths["running"], 1, 1),))
        lyric = repository.create_scan_session(mode="lyric", source_folder=root)
        repository.add_scan_items(lyric.id, (ScanItemInput(paths["lyric"], 1, status="indexed"),))
        repository.finish_scan_session(lyric.id, status="completed")
        waiting = repository.create_scan_session(mode="audio", source_folder=root)
        repository.add_scan_items(waiting.id, (ScanItemInput(paths["waiting"], 1, status="waiting"),))
        repository.finish_scan_session(waiting.id, status="completed")
        self.index_completed_session(repository, root, (("valid.mp3", 1, 1),))
        lyric_asset = repository.upsert_asset(AssetUpsert(root / "lyrics.lrc", 1, 1, kind="lyric"))

        roots = repository.latest_completed_audio_roots(
            tuple(asset.id for asset in assets.values()) + (lyric_asset.id, "missing-asset")
        )

        self.assertEqual(roots, {assets["valid"].id: root})
        with self.assertRaises(RepositoryDataError):
            repository.latest_completed_audio_roots(("",))
        with self.assertRaises(RepositoryDataError):
            repository.latest_completed_audio_roots((123,))  # type: ignore[arg-type]

    @unittest.skipUnless(os.name == "nt", "Windows 路径规范化与跨盘规则")
    def test_latest_completed_audio_roots_accept_equivalent_windows_paths_and_reject_cross_drive(self) -> None:
        repository = self.create_repository()
        root = self.root / "CaseRoot"
        path = root / "Song.MP3"
        asset = repository.upsert_asset(AssetUpsert(path, 1, 1))
        session, _ = self.index_completed_session(repository, root, (("Song.MP3", 1, 1),))
        repository._connection.execute(
            "UPDATE scan_sessions SET source_folder = ? WHERE id = ?",
            (os.fspath(root).swapcase().replace("\\", "/"), session.id),
        )
        repository._connection.execute(
            "UPDATE scan_items SET source_path = ? WHERE session_id = ?",
            (os.fspath(path).swapcase().replace("\\", "/"), session.id),
        )
        roots = repository.latest_completed_audio_roots((asset.id,))
        self.assertEqual(os.path.normcase(os.fspath(roots[asset.id])), os.path.normcase(os.fspath(root)))

        current_drive = root.drive.casefold()
        other_drive = next(drive for drive in ("Z:", "Y:", "X:") if drive.casefold() != current_drive)
        escaped_path = Path(other_drive + "\\escape.mp3")
        escaped_asset = repository.upsert_asset(AssetUpsert(escaped_path, 1, 1))
        corrupt = repository.create_scan_session(mode="audio", source_folder=root)
        repository.finish_scan_session(corrupt.id, status="completed")
        repository._connection.execute(
            "INSERT INTO scan_items(id, session_id, source_path, size_bytes, status) VALUES (?, ?, ?, ?, ?)",
            ("cross-drive-item", corrupt.id, escaped_asset.normalized_path, 1, "indexed"),
        )
        with self.assertRaises(RepositoryDataError):
            repository.latest_completed_audio_roots((escaped_asset.id,))

    def test_latest_completed_audio_roots_fail_closed_for_corrupt_root_or_root2_escape(self) -> None:
        repository = self.create_repository()
        root = self.root / "Root"
        root2 = self.root / "Root2"
        path = root2 / "escape.mp3"
        asset = repository.upsert_asset(AssetUpsert(path, 1, 1))
        session, _ = self.index_completed_session(repository, root2, (("escape.mp3", 1, 1),))

        repository._connection.execute(
            "UPDATE scan_sessions SET source_folder = ? WHERE id = ?",
            (str(root), session.id),
        )
        with self.assertRaises(RepositoryDataError):
            repository.latest_completed_audio_roots((asset.id,))

        repository._connection.execute(
            "UPDATE scan_sessions SET source_folder = ? WHERE id = ?",
            ("relative/root", session.id),
        )
        with self.assertRaises(RepositoryDataError):
            repository.latest_completed_audio_roots((asset.id,))

    def test_latest_completed_audio_roots_is_read_only_single_connection_and_thread_owned(self) -> None:
        repository = self.create_repository()
        root = self.root / "readonly"
        asset = repository.upsert_asset(AssetUpsert(root / "song.mp3", 1, 1))
        self.index_completed_session(repository, root, (("song.mp3", 1, 1),))
        changes_before = repository._connection.total_changes
        statements: list[str] = []
        repository._connection.set_trace_callback(statements.append)
        try:
            with patch(
                "repositories.library_repository.open_database",
                side_effect=AssertionError("不得创建第二连接"),
            ):
                self.assertEqual(
                    repository.latest_completed_audio_roots((asset.id, asset.id)),
                    {asset.id: root},
                )
        finally:
            repository._connection.set_trace_callback(None)
        self.assertEqual(repository._connection.total_changes, changes_before)
        self.assertTrue(statements)
        self.assertTrue(all(statement.lstrip().upper().startswith("SELECT") for statement in statements))

        errors: list[BaseException] = []

        def cross_thread() -> None:
            try:
                repository.latest_completed_audio_roots((asset.id,))
            except BaseException as error:
                errors.append(error)

        thread = threading.Thread(target=cross_thread)
        thread.start()
        thread.join()
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], RepositoryThreadError)

    def test_closed_repository_rejects_all_further_use(self) -> None:
        repository = self.create_repository()
        repository.close()

        with self.assertRaises(RepositoryClosedError):
            repository.list_assets()
        with self.assertRaises(RepositoryClosedError):
            repository.close()


if __name__ == "__main__":
    unittest.main()
