"""SQLite connection and schema migration helpers for MusicCtrl."""

from .connection import (
    DatabaseConfig,
    DatabaseConfigurationError,
    DatabaseConnectionError,
    open_database,
)
from .migrations import (
    MIGRATIONS,
    Migration,
    MigrationApplyError,
    MigrationDefinitionError,
    MigrationError,
    MigrationHistoryError,
    MigrationResult,
    apply_migrations,
)

__all__ = [
    "DatabaseConfig",
    "DatabaseConfigurationError",
    "DatabaseConnectionError",
    "MIGRATIONS",
    "Migration",
    "MigrationApplyError",
    "MigrationDefinitionError",
    "MigrationError",
    "MigrationHistoryError",
    "MigrationResult",
    "apply_migrations",
    "open_database",
]
