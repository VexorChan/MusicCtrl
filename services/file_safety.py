"""Shared path identity and Windows directory-chain locking helpers."""

from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import stat


_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x0400)


def _path_key(path: Path) -> str:
    return os.path.normcase(os.path.normpath(os.path.abspath(os.fspath(path))))


def _is_reparse(metadata: os.stat_result) -> bool:
    return bool(int(getattr(metadata, "st_file_attributes", 0)) & _REPARSE_POINT)


def _identity(metadata: os.stat_result) -> tuple[int, int, int, int]:
    return (
        int(metadata.st_dev),
        int(metadata.st_ino),
        int(metadata.st_size),
        int(metadata.st_mtime_ns),
    )


def _within_root(path: Path, root: Path) -> bool:
    try:
        return os.path.commonpath((_path_key(path), _path_key(root))) == _path_key(root)
    except ValueError:
        return False


def _validate_directory_chain(root: Path, parent: Path) -> None:
    if not _within_root(parent, root):
        raise RuntimeError(f"路径超出已授权扫描根：{parent}")
    root_meta = os.lstat(root)
    if not stat.S_ISDIR(root_meta.st_mode) or stat.S_ISLNK(root_meta.st_mode) or _is_reparse(root_meta):
        raise RuntimeError(f"授权根不是普通目录：{root}")
    relative = Path(os.path.relpath(parent, root))
    current = root
    if relative == Path("."):
        return
    for part in relative.parts:
        if part in {"", ".", ".."}:
            raise RuntimeError(f"目录链无效：{parent}")
        current = current / part
        metadata = os.lstat(current)
        if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode) or _is_reparse(metadata):
            raise RuntimeError(f"目录链包含链接或重解析点：{current}")


@contextmanager
def _locked_directory_chain(root: Path, parent: Path):
    """Lock checked Windows directory components against rename/reparse swap."""

    _validate_directory_chain(root, parent)
    if os.name != "nt":
        yield
        return

    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL

    invalid_handle = ctypes.c_void_p(-1).value
    relative = Path(os.path.relpath(parent, root))
    paths = [root]
    current = root
    if relative != Path("."):
        for part in relative.parts:
            current = current / part
            paths.append(current)

    handles: list[int] = []
    try:
        for path in paths:
            handle = create_file(
                os.fspath(path),
                0x0080 | 0x00010000,  # FILE_READ_ATTRIBUTES | DELETE
                0x00000001 | 0x00000002,  # share read/write, not delete
                None,
                3,  # OPEN_EXISTING
                0x02000000 | 0x00200000,  # BACKUP_SEMANTICS | OPEN_REPARSE_POINT
                None,
            )
            if handle == invalid_handle:
                raise RuntimeError(
                    f"无法锁定目录链：{path}（Windows 错误 {ctypes.get_last_error()}）"
                )
            handles.append(handle)
            metadata = os.lstat(path)
            if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode) or _is_reparse(metadata):
                raise RuntimeError(f"目录链包含链接或重解析点：{path}")
        yield
    finally:
        for handle in reversed(handles):
            close_handle(handle)
