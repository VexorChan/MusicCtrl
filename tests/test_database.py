from __future__ import annotations

from pathlib import Path
import sqlite3
import threading
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from database.connection import (
    DatabaseConfig,
    DatabaseConfigurationError,
    DatabaseConnectionError,
    open_database,
)
from database.migrations import (
    MIGRATIONS,
    Migration,
    MigrationApplyError,
    MigrationDefinitionError,
    MigrationError,
    MigrationHistoryError,
    apply_migrations,
)


class DatabaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.database_path = self.root / "library.sqlite3"

    def open(self, **overrides) -> sqlite3.Connection:
        config = DatabaseConfig(
            path=self.database_path,
            timeout_seconds=overrides.get("timeout_seconds", 1.25),
            busy_timeout_ms=overrides.get("busy_timeout_ms", 2345),
        )
        connection = open_database(config)
        self.addCleanup(connection.close)
        return connection

    def migrate_v1(self) -> sqlite3.Connection:
        connection = self.open()
        result = apply_migrations(
            connection,
            self.database_path,
            migrations=(MIGRATIONS[0],),
        )
        self.assertEqual(result.applied_versions, (1,))
        self.assertIsNone(result.backup_path)
        return connection

    def test_config_requires_absolute_path_and_existing_directory_parent(self) -> None:
        with self.assertRaisesRegex(DatabaseConfigurationError, "pathlib.Path"):
            DatabaseConfig(path=str(self.database_path))  # type: ignore[arg-type]
        with self.assertRaisesRegex(DatabaseConfigurationError, "绝对路径"):
            DatabaseConfig(path=Path("relative.sqlite3"))
        with self.assertRaisesRegex(DatabaseConfigurationError, "父目录不存在"):
            DatabaseConfig(path=self.root / "missing" / "library.sqlite3")

        parent_file = self.root / "parent-file"
        parent_file.touch()
        with self.assertRaisesRegex(DatabaseConfigurationError, "不是目录"):
            DatabaseConfig(path=parent_file / "library.sqlite3")

    def test_connection_uses_explicit_timeouts_foreign_keys_rows_and_no_wal(self) -> None:
        connection = self.open(timeout_seconds=0.75, busy_timeout_ms=3210)

        self.assertIs(connection.row_factory, sqlite3.Row)
        self.assertIsNone(connection.isolation_level)
        self.assertEqual(connection.execute("PRAGMA busy_timeout").fetchone()[0], 3210)
        self.assertEqual(connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        self.assertNotEqual(connection.execute("PRAGMA journal_mode").fetchone()[0].lower(), "wal")

    def test_connection_remains_owned_by_creating_thread(self) -> None:
        connection = self.open()
        errors: list[BaseException] = []

        def use_from_other_thread() -> None:
            try:
                connection.execute("SELECT 1").fetchone()
            except BaseException as exc:  # captured for assertion in the owner thread
                errors.append(exc)

        worker = threading.Thread(target=use_from_other_thread)
        worker.start()
        worker.join(timeout=5)

        self.assertFalse(worker.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], sqlite3.ProgrammingError)

    def test_v1_creates_exactly_the_five_tables_and_expected_index(self) -> None:
        connection = self.migrate_v1()
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        indexes = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index' AND name NOT LIKE 'sqlite_%'"
            )
        }

        self.assertEqual(
            tables,
            {"schema_migrations", "assets", "settings", "scan_sessions", "scan_items"},
        )
        self.assertIn("idx_assets_kind_state", indexes)
        self.assertIn("idx_scan_items_session", indexes)

    def test_v2_creates_exact_rename_audit_schema_indexes_and_constraints(self) -> None:
        self.assertGreaterEqual(len(MIGRATIONS), 2)
        connection = self.open()

        result = apply_migrations(connection, self.database_path)

        self.assertEqual(result.applied_versions, (1, 2))
        self.assertIsNone(result.backup_path)
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        self.assertEqual(
            tables,
            {
                "schema_migrations",
                "assets",
                "settings",
                "scan_sessions",
                "scan_items",
                "operations",
                "operation_items",
            },
        )
        operation_columns = [row[1] for row in connection.execute("PRAGMA table_info(operations)")]
        item_columns = [row[1] for row in connection.execute("PRAGMA table_info(operation_items)")]
        self.assertEqual(
            operation_columns,
            [
                "id",
                "operation_type",
                "status",
                "success_count",
                "failure_count",
                "summary_json",
                "created_at",
                "started_at",
                "completed_at",
            ],
        )
        self.assertEqual(
            item_columns,
            [
                "id",
                "operation_id",
                "asset_id",
                "source_path",
                "normalized_source_path",
                "target_path",
                "normalized_target_path",
                "expected_size_bytes",
                "expected_mtime_ns",
                "result",
                "error_code",
                "error_message",
                "before_json",
                "after_json",
                "created_at",
                "completed_at",
            ],
        )

        foreign_keys = {
            row[3]: (row[2], row[6])
            for row in connection.execute("PRAGMA foreign_key_list(operation_items)")
        }
        self.assertEqual(foreign_keys["operation_id"], ("operations", "CASCADE"))
        self.assertEqual(foreign_keys["asset_id"], ("assets", "RESTRICT"))

        index_rows = connection.execute("PRAGMA index_list(operation_items)").fetchall()
        index_contracts: list[tuple[tuple[str, ...], bool, bool, str]] = []
        for row in index_rows:
            name = row[1]
            columns = tuple(
                info[2]
                for info in connection.execute(f'PRAGMA index_info("{name}")')
            )
            sql_row = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='index' AND name=?",
                (name,),
            ).fetchone()
            index_contracts.append(
                (columns, bool(row[2]), bool(row[4]), "" if sql_row is None or sql_row[0] is None else sql_row[0])
            )
        self.assertIn((("operation_id", "result"), False, False), {
            (columns, unique, partial) for columns, unique, partial, _sql in index_contracts
        })
        self.assertIn((("operation_id", "asset_id"), True, False), {
            (columns, unique, partial) for columns, unique, partial, _sql in index_contracts
        })
        self.assertIn((("operation_id", "normalized_source_path"), True, False), {
            (columns, unique, partial) for columns, unique, partial, _sql in index_contracts
        })
        self.assertIn((("operation_id", "normalized_target_path"), True, False), {
            (columns, unique, partial) for columns, unique, partial, _sql in index_contracts
        })
        for active_column in ("asset_id", "normalized_target_path"):
            matches = [
                sql
                for columns, unique, partial, sql in index_contracts
                if columns == (active_column,) and unique and partial
            ]
            self.assertEqual(len(matches), 1)
            self.assertIn("planned", matches[0])
            self.assertIn("running", matches[0])

        now = "2026-01-01T00:00:00Z"
        connection.execute(
            """
            INSERT INTO assets(
                id, kind, canonical_path, normalized_path, file_name, extension,
                size_bytes, mtime_ns, file_state, created_at, updated_at
            ) VALUES ('asset-v2', 'audio', 'C:/fixture/source.mp3', 'c:/fixture/source.mp3',
                      'source.mp3', '.mp3', 10, 20, 'active', ?, ?)
            """,
            (now, now),
        )
        connection.execute(
            "INSERT INTO operations(id, operation_type, status, summary_json, created_at) VALUES ('op-1', 'rename', 'planned', '{}', ?)",
            (now,),
        )
        connection.execute(
            """
            INSERT INTO operation_items(
                id, operation_id, asset_id, source_path, normalized_source_path,
                target_path, normalized_target_path, expected_size_bytes,
                expected_mtime_ns, result, before_json, created_at
            ) VALUES ('item-1', 'op-1', 'asset-v2', 'C:/fixture/source.mp3',
                      'c:/fixture/source.mp3', 'C:/fixture/target.mp3',
                      'c:/fixture/target.mp3', 10, 20, 'planned', '{}', ?)
            """,
            (now,),
        )
        with self.assertRaises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO operations(id, operation_type, status, summary_json, created_at) VALUES ('bad-type', 'scan', 'planned', '{}', ?)",
                (now,),
            )
        with self.assertRaises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO operations(id, operation_type, status, success_count, summary_json, created_at) VALUES ('bad-count', 'rename', 'planned', -1, '{}', ?)",
                (now,),
            )
        with self.assertRaises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO operation_items(
                    id, operation_id, asset_id, source_path, normalized_source_path,
                    target_path, normalized_target_path, expected_size_bytes,
                    result, before_json, created_at
                ) VALUES ('bad-size', 'op-1', 'asset-v2', 'x', 'x', 'y', 'y', -1,
                          'planned', '{}', ?)
                """,
                (now,),
            )

        again = apply_migrations(connection, self.database_path)
        self.assertEqual(again.applied_versions, ())
        self.assertIsNone(again.backup_path)

    def test_formal_v2_upgrade_backs_up_v1_preserves_data_and_is_idempotent(self) -> None:
        connection = self.migrate_v1()
        connection.execute(
            "INSERT INTO settings(key, value_json, updated_at) VALUES ('v1-sentinel', '1', 'now')"
        )

        result = apply_migrations(connection, self.database_path)

        self.assertEqual(result.applied_versions, (2,))
        self.assertIsNotNone(result.backup_path)
        assert result.backup_path is not None
        self.assertEqual(connection.execute("SELECT value_json FROM settings WHERE key='v1-sentinel'").fetchone()[0], "1")
        backup = sqlite3.connect(result.backup_path)
        try:
            self.assertEqual(backup.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(backup.execute("SELECT version FROM schema_migrations").fetchall(), [(1,)])
            self.assertIsNone(
                backup.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='operations'"
                ).fetchone()
            )
        finally:
            backup.close()
        self.assertEqual(apply_migrations(connection, self.database_path).applied_versions, ())
        self.assertEqual(len(list(self.root.glob("*.backup.sqlite3"))), 1)

    def test_v2_database_is_rejected_by_older_v1_migration_set(self) -> None:
        connection = self.open()
        self.assertEqual(
            apply_migrations(connection, self.database_path).applied_versions,
            (1, 2),
        )

        with self.assertRaises(MigrationHistoryError):
            apply_migrations(
                connection,
                self.database_path,
                migrations=(MIGRATIONS[0],),
            )

        self.assertEqual(
            [row[0] for row in connection.execute("SELECT version FROM schema_migrations")],
            [1, 2],
        )
        self.assertIsNotNone(
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='operations'"
            ).fetchone()
        )

    def test_asset_constraints_and_normalized_path_uniqueness(self) -> None:
        connection = self.migrate_v1()
        valid = (
            "asset-1",
            "audio",
            "C:/demo/song.mp3",
            "c:/demo/song.mp3",
            "song.mp3",
            ".mp3",
            10,
            None,
            "active",
            "2026-01-01T00:00:00Z",
            "2026-01-01T00:00:00Z",
        )
        connection.execute(
            """
            INSERT INTO assets(
                id, kind, canonical_path, normalized_path, file_name, extension,
                size_bytes, mtime_ns, file_state, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            valid,
        )

        duplicate = ("asset-2",) + valid[1:]
        with self.assertRaises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO assets(
                    id, kind, canonical_path, normalized_path, file_name, extension,
                    size_bytes, mtime_ns, file_state, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                duplicate,
            )
        with self.assertRaises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO assets(
                    id, kind, canonical_path, normalized_path, file_name, extension,
                    size_bytes, mtime_ns, file_state, created_at, updated_at
                ) VALUES ('bad-size', 'audio', 'C:/bad', 'c:/bad', 'bad', '.mp3',
                          -1, NULL, 'active', 'now', 'now')
                """
            )
        with self.assertRaises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO assets(
                    id, kind, canonical_path, normalized_path, file_name, extension,
                    size_bytes, mtime_ns, file_state, created_at, updated_at
                ) VALUES ('bad-state', 'audio', 'C:/state', 'c:/state', 'state', '.mp3',
                          1, NULL, 'deleted', 'now', 'now')
                """
            )

    def test_scan_constraints_unique_items_foreign_key_and_cascade(self) -> None:
        connection = self.migrate_v1()
        connection.execute(
            """
            INSERT INTO scan_sessions(id, mode, source_folder, status, started_at)
            VALUES ('session-1', 'audio', 'C:/fixture', 'running', 'now')
            """
        )
        connection.execute(
            """
            INSERT INTO scan_items(id, session_id, source_path, size_bytes, status)
            VALUES ('item-1', 'session-1', 'C:/fixture/a.mp3', 1, 'waiting')
            """
        )
        with self.assertRaises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO scan_items(id, session_id, source_path, size_bytes, status)
                VALUES ('item-2', 'session-1', 'C:/fixture/a.mp3', 1, 'waiting')
                """
            )
        with self.assertRaises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO scan_items(id, session_id, source_path, size_bytes, status)
                VALUES ('orphan', 'missing', 'C:/fixture/b.mp3', 1, 'waiting')
                """
            )

        connection.execute("DELETE FROM scan_sessions WHERE id = 'session-1'")
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM scan_items").fetchone()[0], 0)

    def test_v1_is_idempotent_and_does_not_create_backup(self) -> None:
        connection = self.open()
        first = apply_migrations(connection, self.database_path, migrations=(MIGRATIONS[0],))
        second = apply_migrations(connection, self.database_path, migrations=(MIGRATIONS[0],))

        self.assertEqual(first.applied_versions, (1,))
        self.assertIsNone(first.backup_path)
        self.assertEqual(second.applied_versions, ())
        self.assertIsNone(second.backup_path)
        self.assertEqual(
            connection.execute("SELECT version FROM schema_migrations").fetchall()[0][0],
            1,
        )
        self.assertEqual(list(self.root.glob("*.backup.sqlite3")), [])

    def test_existing_v1_gets_openable_backup_before_injected_v2(self) -> None:
        connection = self.migrate_v1()
        connection.execute(
            "INSERT INTO settings(key, value_json, updated_at) VALUES ('sentinel', '1', 'now')"
        )
        migrations = (MIGRATIONS[0],) + (
            Migration(2, "test v2", ("CREATE TABLE v2_marker (id INTEGER PRIMARY KEY)",)),
        )

        result = apply_migrations(connection, self.database_path, migrations=migrations)

        self.assertEqual(result.applied_versions, (2,))
        self.assertIsNotNone(result.backup_path)
        assert result.backup_path is not None
        self.assertEqual(result.backup_path.parent, self.root)
        self.assertTrue(result.backup_path.is_file())
        backup = sqlite3.connect(result.backup_path)
        try:
            self.assertEqual(backup.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(
                backup.execute("SELECT version FROM schema_migrations").fetchall(),
                [(1,)],
            )
            self.assertEqual(
                backup.execute(
                    "SELECT value_json FROM settings WHERE key='sentinel'"
                ).fetchone()[0],
                "1",
            )
            self.assertIsNone(
                backup.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='v2_marker'"
                ).fetchone()
            )
        finally:
            backup.close()

        again = apply_migrations(connection, self.database_path, migrations=migrations)
        self.assertEqual(again.applied_versions, ())
        self.assertIsNone(again.backup_path)
        self.assertEqual(len(list(self.root.glob("*.backup.sqlite3"))), 1)

    def test_failed_v2_keeps_backup_and_rolls_back_ddl_and_version(self) -> None:
        connection = self.migrate_v1()
        connection.execute(
            "INSERT INTO settings(key, value_json, updated_at) VALUES ('sentinel', '1', 'now')"
        )
        migrations = (MIGRATIONS[0],) + (
            Migration(
                2,
                "failing test v2",
                (
                    "CREATE TABLE partial_v2 (id INTEGER PRIMARY KEY)",
                    "INSERT INTO table_that_does_not_exist VALUES (1)",
                ),
            ),
        )

        with self.assertRaisesRegex(MigrationApplyError, "v2.*回滚"):
            apply_migrations(connection, self.database_path, migrations=migrations)

        self.assertIsNone(
            connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='partial_v2'"
            ).fetchone()
        )
        self.assertEqual(
            [row[0] for row in connection.execute("SELECT version FROM schema_migrations")],
            [1],
        )
        self.assertEqual(
            connection.execute(
                "SELECT value_json FROM settings WHERE key='sentinel'"
            ).fetchone()[0],
            "1",
        )
        backups = list(self.root.glob("*.backup.sqlite3"))
        self.assertEqual(len(backups), 1)
        backup = sqlite3.connect(backups[0])
        try:
            self.assertEqual(backup.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(
                backup.execute(
                    "SELECT value_json FROM settings WHERE key='sentinel'"
                ).fetchone()[0],
                "1",
            )
        finally:
            backup.close()

    def test_formal_v2_version_insert_failure_rolls_back_ddl_and_keeps_v1(self) -> None:
        connection = self.migrate_v1()
        connection.execute(
            """
            CREATE TRIGGER reject_formal_v2_version
            BEFORE INSERT ON schema_migrations
            WHEN NEW.version = 2
            BEGIN
                SELECT RAISE(ABORT, 'reject formal v2 version');
            END
            """
        )

        with self.assertRaisesRegex(MigrationApplyError, "v2.*回滚"):
            apply_migrations(connection, self.database_path)

        self.assertEqual(
            [row[0] for row in connection.execute("SELECT version FROM schema_migrations")],
            [1],
        )
        for table in ("operations", "operation_items"):
            self.assertIsNone(
                connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                    (table,),
                ).fetchone()
            )
        self.assertEqual(len(list(self.root.glob("*.backup.sqlite3"))), 1)

    def test_formal_v2_commit_failure_rolls_back_ddl_version_and_keeps_connection_usable(self) -> None:
        connection = self.migrate_v1()

        class FailV2CommitProxy:
            @property
            def in_transaction(self):
                return connection.in_transaction

            def execute(self, sql, *args):
                if sql == "COMMIT":
                    raise sqlite3.OperationalError("formal v2 commit failure")
                return connection.execute(sql, *args)

            def backup(self, destination):
                return connection.backup(destination)

        proxy = FailV2CommitProxy()
        with self.assertRaisesRegex(MigrationApplyError, "v2.*回滚"):
            apply_migrations(proxy, self.database_path)  # type: ignore[arg-type]

        self.assertFalse(connection.in_transaction)
        self.assertEqual(
            [row[0] for row in connection.execute("SELECT version FROM schema_migrations")],
            [1],
        )
        self.assertIsNone(
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='operations'"
            ).fetchone()
        )
        self.assertEqual(connection.execute("SELECT 1").fetchone()[0], 1)
        self.assertEqual(len(list(self.root.glob("*.backup.sqlite3"))), 1)

    def test_backup_failure_prevents_v2_migration(self) -> None:
        connection = self.migrate_v1()
        migrations = (MIGRATIONS[0],) + (
            Migration(2, "test v2", ("CREATE TABLE must_not_exist(id INTEGER)",)),
        )
        original_connect = sqlite3.connect

        def fail_backup(path, *args, **kwargs):
            if str(path).endswith(".backup.sqlite3"):
                raise sqlite3.OperationalError("simulated backup failure")
            return original_connect(path, *args, **kwargs)

        with patch("database.migrations.sqlite3.connect", side_effect=fail_backup):
            with self.assertRaisesRegex(MigrationError, "备份"):
                apply_migrations(connection, self.database_path, migrations=migrations)

        self.assertEqual(
            [row[0] for row in connection.execute("SELECT version FROM schema_migrations")],
            [1],
        )
        self.assertIsNone(
            connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='must_not_exist'"
            ).fetchone()
        )

    def test_migration_versions_must_be_continuous_unique_and_ordered(self) -> None:
        connection = self.open()
        invalid = (
            MIGRATIONS[0],
            Migration(3, "skipped v2", ("SELECT 1",)),
        )
        with self.assertRaisesRegex(MigrationDefinitionError, "连续"):
            apply_migrations(connection, self.database_path, migrations=invalid)

    def test_unmanaged_nonempty_database_is_preserved_and_rejected(self) -> None:
        connection = self.open()
        connection.execute("CREATE TABLE foreign_data(value TEXT)")
        connection.execute("INSERT INTO foreign_data VALUES ('sentinel')")

        with self.assertRaisesRegex(MigrationHistoryError, "未受 MusicCtrl 管理"):
            apply_migrations(connection, self.database_path)

        self.assertEqual(
            connection.execute("SELECT value FROM foreign_data").fetchone()[0],
            "sentinel",
        )
        self.assertFalse(
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='assets'"
            ).fetchone()
        )

    def test_future_and_gapped_migration_histories_are_rejected(self) -> None:
        for versions in ((1, 3), (1, 2, 3)):
            with self.subTest(versions=versions):
                path = self.root / f"history-{'-'.join(map(str, versions))}.sqlite3"
                connection = open_database(DatabaseConfig(path))
                self.addCleanup(connection.close)
                connection.execute(
                    """CREATE TABLE schema_migrations(
                        version INTEGER PRIMARY KEY,
                        description TEXT,
                        applied_at TEXT NOT NULL
                    )"""
                )
                connection.executemany(
                    "INSERT INTO schema_migrations VALUES (?, 'test', 'now')",
                    ((version,) for version in versions),
                )
                with self.assertRaises(MigrationHistoryError):
                    apply_migrations(connection, path)

    def test_corrupt_database_reports_connection_or_history_error(self) -> None:
        self.database_path.write_bytes(b"not a sqlite database")
        try:
            connection = self.open()
        except DatabaseConnectionError:
            return
        with self.assertRaises(MigrationHistoryError):
            apply_migrations(connection, self.database_path)

    def test_timeout_values_must_be_positive_and_finite(self) -> None:
        for timeout in (0, -1, float("inf")):
            with self.subTest(timeout=timeout):
                with self.assertRaises(DatabaseConfigurationError):
                    DatabaseConfig(self.database_path, timeout_seconds=timeout)
        with self.assertRaises(DatabaseConfigurationError):
            DatabaseConfig(self.database_path, busy_timeout_ms=0)

    def test_database_path_mismatch_is_rejected(self) -> None:
        connection = self.open()
        with self.assertRaisesRegex(Exception, "不一致"):
            apply_migrations(connection, self.root / "other.sqlite3")

    def test_package_never_creates_database_or_backup_in_project_root(self) -> None:
        project_root = Path(__file__).resolve().parents[1]
        before = {
            path.name
            for pattern in ("*.db", "*.sqlite", "*.sqlite3", "*.backup.sqlite3")
            for path in project_root.glob(pattern)
        }

        connection = self.migrate_v1()
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0], 1)

        after = {
            path.name
            for pattern in ("*.db", "*.sqlite", "*.sqlite3", "*.backup.sqlite3")
            for path in project_root.glob(pattern)
        }
        self.assertEqual(after, before)
        self.assertTrue(self.database_path.is_relative_to(self.root))


if __name__ == "__main__":
    unittest.main()
