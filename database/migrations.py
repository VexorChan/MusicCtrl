"""Versioned SQLite schema migrations for the P1 read-only index."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import os
from pathlib import Path
import sqlite3
from uuid import uuid4


@dataclass(frozen=True, slots=True)
class Migration:
    version: int
    description: str
    statements: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class MigrationResult:
    applied_versions: tuple[int, ...]
    backup_path: Path | None


class MigrationError(RuntimeError):
    """Base error for migration validation and execution."""


class MigrationDefinitionError(MigrationError):
    """Raised when migration definitions are not continuous and ordered."""


class MigrationHistoryError(MigrationError):
    """Raised when the database migration history is inconsistent."""


class MigrationApplyError(MigrationError):
    """Raised after a migration fails and its transaction is rolled back."""


MIGRATIONS = (
    Migration(
        version=1,
        description="P1 read-only scan index",
        statements=(
            """
            CREATE TABLE schema_migrations (
                version INTEGER PRIMARY KEY,
                description TEXT,
                applied_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE assets (
                id TEXT PRIMARY KEY NOT NULL,
                kind TEXT NOT NULL CHECK (kind IN ('audio', 'lyric')),
                canonical_path TEXT NOT NULL,
                normalized_path TEXT NOT NULL UNIQUE,
                file_name TEXT NOT NULL,
                extension TEXT NOT NULL,
                size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0),
                mtime_ns INTEGER CHECK (mtime_ns IS NULL OR mtime_ns >= 0),
                file_state TEXT NOT NULL
                    CHECK (file_state IN ('active', 'missing', 'external_changed')),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
            "CREATE INDEX idx_assets_kind_state ON assets(kind, file_state)",
            """
            CREATE TABLE settings (
                key TEXT PRIMARY KEY NOT NULL,
                value_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE scan_sessions (
                id TEXT PRIMARY KEY NOT NULL,
                mode TEXT NOT NULL CHECK (mode IN ('audio', 'lyric')),
                source_folder TEXT NOT NULL,
                status TEXT NOT NULL
                    CHECK (status IN ('running', 'cancelled', 'completed', 'failed')),
                started_at TEXT NOT NULL,
                completed_at TEXT
            )
            """,
            """
            CREATE TABLE scan_items (
                id TEXT PRIMARY KEY NOT NULL,
                session_id TEXT NOT NULL
                    REFERENCES scan_sessions(id) ON DELETE CASCADE,
                source_path TEXT NOT NULL,
                size_bytes INTEGER CHECK (size_bytes IS NULL OR size_bytes >= 0),
                status TEXT NOT NULL
                    CHECK (status IN ('waiting', 'indexed', 'skipped', 'failed')),
                reason TEXT,
                UNIQUE(session_id, source_path)
            )
            """,
            "CREATE INDEX idx_scan_items_session ON scan_items(session_id)",
        ),
    ),
    Migration(
        version=2,
        description="P2 safe rename operations",
        statements=(
            """
            CREATE TABLE operations (
                id TEXT PRIMARY KEY NOT NULL,
                operation_type TEXT NOT NULL
                    CHECK (operation_type IN ('rename')),
                status TEXT NOT NULL
                    CHECK (status IN (
                        'planned', 'running', 'success', 'partial', 'failed', 'cancelled'
                    )),
                success_count INTEGER NOT NULL DEFAULT 0 CHECK (success_count >= 0),
                failure_count INTEGER NOT NULL DEFAULT 0 CHECK (failure_count >= 0),
                summary_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                started_at TEXT,
                completed_at TEXT
            )
            """,
            """
            CREATE TABLE operation_items (
                id TEXT PRIMARY KEY NOT NULL,
                operation_id TEXT NOT NULL
                    REFERENCES operations(id) ON DELETE CASCADE,
                asset_id TEXT NOT NULL
                    REFERENCES assets(id) ON DELETE RESTRICT,
                source_path TEXT NOT NULL,
                normalized_source_path TEXT NOT NULL,
                target_path TEXT NOT NULL,
                normalized_target_path TEXT NOT NULL,
                expected_size_bytes INTEGER NOT NULL CHECK (expected_size_bytes >= 0),
                expected_mtime_ns INTEGER
                    CHECK (expected_mtime_ns IS NULL OR expected_mtime_ns >= 0),
                result TEXT NOT NULL
                    CHECK (result IN (
                        'planned', 'running', 'success', 'failed',
                        'rolled_back', 'rollback_failed', 'cancelled'
                    )),
                error_code TEXT,
                error_message TEXT,
                before_json TEXT NOT NULL,
                after_json TEXT,
                created_at TEXT NOT NULL,
                completed_at TEXT,
                UNIQUE(operation_id, asset_id),
                UNIQUE(operation_id, normalized_source_path),
                UNIQUE(operation_id, normalized_target_path)
            )
            """,
            "CREATE INDEX idx_operation_items_operation ON operation_items(operation_id, result)",
            """
            CREATE UNIQUE INDEX uq_operation_items_active_asset
            ON operation_items(asset_id)
            WHERE result IN ('planned', 'running')
            """,
            """
            CREATE UNIQUE INDEX uq_operation_items_active_target
            ON operation_items(normalized_target_path)
            WHERE result IN ('planned', 'running')
            """,
        ),
    ),
    Migration(
        version=3,
        description="P4 lyrics match history",
        statements=(
            """
            CREATE TABLE lyrics_matches (
                id TEXT PRIMARY KEY NOT NULL,
                audio_asset_id TEXT NOT NULL
                    REFERENCES assets(id) ON DELETE RESTRICT,
                lyric_asset_id TEXT
                    REFERENCES assets(id) ON DELETE RESTRICT,
                source_kind TEXT NOT NULL
                    CHECK (source_kind IN ('embedded', 'external')),
                confidence INTEGER NOT NULL CHECK (confidence BETWEEN 0 AND 100),
                method TEXT NOT NULL CHECK (method IN ('automatic', 'manual')),
                state TEXT NOT NULL CHECK (state IN ('matched', 'cancelled')),
                is_current INTEGER NOT NULL CHECK (is_current IN (0, 1)),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                CHECK (is_current = 0 OR state = 'matched'),
                CHECK (
                    (source_kind = 'embedded' AND lyric_asset_id IS NULL)
                    OR (source_kind = 'external' AND lyric_asset_id IS NOT NULL)
                )
            )
            """,
            """
            CREATE UNIQUE INDEX uq_current_lyrics_by_audio
            ON lyrics_matches(audio_asset_id)
            WHERE is_current = 1
            """,
            """
            CREATE UNIQUE INDEX uq_current_audio_by_external_lyric
            ON lyrics_matches(lyric_asset_id)
            WHERE is_current = 1 AND lyric_asset_id IS NOT NULL
            """,
            "CREATE INDEX idx_lyrics_matches_audio_history ON lyrics_matches(audio_asset_id, created_at)",
        ),
    ),
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _path_key(path: Path) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def _validate_database_path(connection: sqlite3.Connection, database_path: Path) -> Path:
    if not isinstance(database_path, Path):
        raise MigrationHistoryError("database_path 必须使用 pathlib.Path")
    if not database_path.is_absolute():
        raise MigrationHistoryError("database_path 必须是绝对路径")
    if not database_path.parent.is_dir():
        raise MigrationHistoryError(f"数据库父目录不可用：{database_path.parent}")

    rows = connection.execute("PRAGMA database_list").fetchall()
    main_paths = [row[2] for row in rows if row[1] == "main"]
    if len(main_paths) != 1 or not main_paths[0]:
        raise MigrationHistoryError("迁移只支持文件型 SQLite 主数据库")
    if _path_key(Path(main_paths[0])) != _path_key(database_path):
        raise MigrationHistoryError("database_path 与当前 SQLite 连接不一致")
    return database_path


def _validate_migrations(migrations: tuple[Migration, ...]) -> None:
    if not migrations:
        raise MigrationDefinitionError("迁移列表不能为空")
    versions = tuple(migration.version for migration in migrations)
    expected = tuple(range(1, len(migrations) + 1))
    if versions != expected:
        raise MigrationDefinitionError("迁移版本必须从 1 开始、连续、唯一且升序")
    for migration in migrations:
        if not migration.description.strip():
            raise MigrationDefinitionError(f"迁移 v{migration.version} 缺少描述")
        if not migration.statements:
            raise MigrationDefinitionError(f"迁移 v{migration.version} 没有 SQL 语句")
        if any(not statement.strip() for statement in migration.statements):
            raise MigrationDefinitionError(f"迁移 v{migration.version} 包含空 SQL 语句")


def _schema_migrations_exists(connection: sqlite3.Connection) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_migrations'"
    ).fetchone()
    return row is not None


def _read_applied_versions(connection: sqlite3.Connection) -> tuple[int, ...]:
    if not _schema_migrations_exists(connection):
        unmanaged = connection.execute(
            """SELECT name FROM sqlite_master
               WHERE type IN ('table', 'index', 'view', 'trigger')
                 AND name NOT LIKE 'sqlite_%'
               ORDER BY name"""
        ).fetchall()
        if unmanaged:
            raise MigrationHistoryError("数据库包含未受 MusicCtrl 管理的结构，已拒绝自动迁移")
        return ()
    rows = connection.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall()
    versions = tuple(int(row[0]) for row in rows)
    if not versions:
        raise MigrationHistoryError("数据库存在空的 schema_migrations，无法确认结构来源")
    return versions


def _validate_history(applied: tuple[int, ...], migrations: tuple[Migration, ...]) -> None:
    expected = tuple(range(1, len(applied) + 1))
    if applied != expected:
        raise MigrationHistoryError("数据库迁移历史不连续")
    if applied and applied[-1] > migrations[-1].version:
        raise MigrationHistoryError("数据库版本高于当前程序支持版本")


def _create_backup(connection: sqlite3.Connection, database_path: Path, version: int) -> Path:
    backup_path = database_path.with_name(
        f"{database_path.stem}.v{version}.{uuid4().hex}.backup.sqlite3"
    )
    try:
        destination = sqlite3.connect(backup_path, isolation_level=None)
        try:
            connection.backup(destination)
        finally:
            destination.close()

        verification = sqlite3.connect(backup_path)
        try:
            result = verification.execute("PRAGMA integrity_check").fetchone()
            if result is None or result[0] != "ok":
                raise MigrationError(f"迁移备份完整性检查失败：{backup_path}")
        finally:
            verification.close()
        return backup_path
    except (OSError, sqlite3.Error, MigrationError) as exc:
        try:
            backup_path.unlink(missing_ok=True)
        except OSError:
            pass
        if isinstance(exc, MigrationError):
            raise
        raise MigrationError("无法创建升级前 SQLite 一致性备份") from exc


def apply_migrations(
    connection: sqlite3.Connection,
    database_path: Path,
    *,
    migrations: tuple[Migration, ...] = MIGRATIONS,
) -> MigrationResult:
    """Apply pending migrations and return newly applied versions and backup path."""

    database_path = _validate_database_path(connection, database_path)
    _validate_migrations(migrations)
    if connection.in_transaction:
        raise MigrationHistoryError("迁移开始前连接不得处于其他事务中")
    try:
        applied = _read_applied_versions(connection)
    except MigrationError:
        raise
    except sqlite3.Error as exc:
        raise MigrationHistoryError("无法读取数据库迁移历史") from exc
    _validate_history(applied, migrations)

    current_version = applied[-1] if applied else 0
    pending = tuple(migration for migration in migrations if migration.version > current_version)
    if not pending:
        return MigrationResult(applied_versions=(), backup_path=None)

    backup_path = None
    if current_version >= 1:
        backup_path = _create_backup(connection, database_path, current_version)

    newly_applied: list[int] = []
    for migration in pending:
        try:
            connection.execute("BEGIN IMMEDIATE")
            for statement in migration.statements:
                connection.execute(statement)
            connection.execute(
                """
                INSERT INTO schema_migrations(version, description, applied_at)
                VALUES (?, ?, ?)
                """,
                (migration.version, migration.description, _utc_now()),
            )
            connection.execute("COMMIT")
        except Exception as exc:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise MigrationApplyError(f"迁移 v{migration.version} 失败并已回滚") from exc
        newly_applied.append(migration.version)

    return MigrationResult(applied_versions=tuple(newly_applied), backup_path=backup_path)
