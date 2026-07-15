"""Create explicitly configured, thread-owned SQLite connections."""

from __future__ import annotations

from dataclasses import dataclass
import math
from pathlib import Path
import sqlite3


class DatabaseConfigurationError(ValueError):
    """Raised when a database connection configuration is unsafe or invalid."""


class DatabaseConnectionError(RuntimeError):
    """Raised when SQLite cannot safely open or initialize the configured file."""


@dataclass(frozen=True, slots=True)
class DatabaseConfig:
    """Configuration for one SQLite database file.

    The caller owns the path choice.  This layer never selects a default path or
    creates a missing parent directory.
    """

    path: Path
    timeout_seconds: float = 5.0
    busy_timeout_ms: int = 5000

    def __post_init__(self) -> None:
        if not isinstance(self.path, Path):
            raise DatabaseConfigurationError("数据库路径必须使用 pathlib.Path")
        if not self.path.is_absolute():
            raise DatabaseConfigurationError("数据库路径必须是绝对路径")
        if not self.path.parent.exists():
            raise DatabaseConfigurationError(f"数据库父目录不存在：{self.path.parent}")
        if not self.path.parent.is_dir():
            raise DatabaseConfigurationError(f"数据库父路径不是目录：{self.path.parent}")
        if self.path.exists() and self.path.is_dir():
            raise DatabaseConfigurationError(f"数据库路径不能是目录：{self.path}")
        if (
            isinstance(self.timeout_seconds, bool)
            or not math.isfinite(self.timeout_seconds)
            or self.timeout_seconds <= 0
        ):
            raise DatabaseConfigurationError("timeout_seconds 必须是大于 0 的有限数")
        if (
            isinstance(self.busy_timeout_ms, bool)
            or not isinstance(self.busy_timeout_ms, int)
            or self.busy_timeout_ms <= 0
        ):
            raise DatabaseConfigurationError("busy_timeout_ms 必须是大于 0 的整数")


def open_database(config: DatabaseConfig) -> sqlite3.Connection:
    """Open a connection owned by the current thread.

    Autocommit mode lets the migration layer define every transaction boundary
    explicitly.  WAL is intentionally not enabled in this package.
    """

    if not isinstance(config, DatabaseConfig):
        raise DatabaseConfigurationError("必须显式提供 DatabaseConfig")

    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(
            config.path,
            timeout=float(config.timeout_seconds),
            isolation_level=None,
            check_same_thread=True,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {int(config.busy_timeout_ms)}")
        if connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
            raise DatabaseConnectionError("无法启用 SQLite foreign_keys")
        if connection.execute("PRAGMA busy_timeout").fetchone()[0] != config.busy_timeout_ms:
            raise DatabaseConnectionError("无法应用 SQLite busy_timeout")
        return connection
    except DatabaseConnectionError:
        if connection is not None:
            connection.close()
        raise
    except (OSError, sqlite3.Error) as exc:
        if connection is not None:
            connection.close()
        raise DatabaseConnectionError(f"无法打开 SQLite 数据库：{config.path}") from exc
