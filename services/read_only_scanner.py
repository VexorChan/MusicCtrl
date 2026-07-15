"""Safely enumerate audio files below an explicitly allowed directory."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
import os
from pathlib import Path
import stat


SUPPORTED_AUDIO_EXTENSIONS = frozenset({".mp3", ".flac", ".wav", ".m4a", ".ogg", ".aac"})
_FILE_ATTRIBUTE_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x0400)


@dataclass(frozen=True, slots=True)
class AudioFileEntry:
    path: Path
    relative_path: Path
    extension: str
    size_bytes: int
    mtime_ns: int


class ScanError(Exception):
    """Base error for read-only enumeration."""


class ScanBoundaryError(ScanError):
    """Raised when a requested path crosses the allowed scan boundary."""


class ScanRootError(ScanError):
    """Raised when the allowed root or scan root is missing or not a directory."""


class ScanAccessError(ScanError):
    """Raised when directory metadata cannot be read safely."""


class ScanCancelled(ScanError):
    """Raised at a cooperative checkpoint when scanning was cancelled."""


CancelCallback = Callable[[], bool]


def _check_cancelled(cancel_requested: CancelCallback | None) -> None:
    if cancel_requested is not None and cancel_requested():
        raise ScanCancelled("扫描已取消")


def _absolute_path(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _path_key(path: Path) -> str:
    return os.path.normcase(os.path.normpath(os.fspath(path)))


def _is_reparse_point(metadata: os.stat_result) -> bool:
    attributes = int(getattr(metadata, "st_file_attributes", 0))
    return bool(attributes & _FILE_ATTRIBUTE_REPARSE_POINT)


def _assert_within_boundary(root: Path, allowed_root: Path) -> None:
    root_key = _path_key(root)
    allowed_key = _path_key(allowed_root)
    try:
        common = os.path.commonpath((root_key, allowed_key))
    except ValueError as exc:
        raise ScanBoundaryError(f"扫描目录不在允许范围内：{root}") from exc
    if common != allowed_key:
        raise ScanBoundaryError(f"扫描目录不在允许范围内：{root}")


def _directory_chain(root: Path, allowed_root: Path) -> list[Path]:
    allowed_key = _path_key(allowed_root)
    current = root
    chain = [current]
    while _path_key(current) != allowed_key:
        parent = current.parent
        if parent == current:
            raise ScanBoundaryError(f"扫描目录不在允许范围内：{root}")
        current = parent
        chain.append(current)
    chain.reverse()
    return chain


def _validate_directory_chain(
    root: Path,
    allowed_root: Path,
    cancel_requested: CancelCallback | None,
) -> None:
    for directory in _directory_chain(root, allowed_root):
        _check_cancelled(cancel_requested)
        try:
            metadata = os.lstat(directory)
        except FileNotFoundError as exc:
            raise ScanRootError(f"目录不存在：{directory}") from exc
        except OSError as exc:
            raise ScanAccessError(f"无法读取目录信息：{directory}") from exc
        if stat.S_ISLNK(metadata.st_mode) or _is_reparse_point(metadata):
            raise ScanBoundaryError(f"目录不能是符号链接或重解析点：{directory}")
        if not stat.S_ISDIR(metadata.st_mode):
            raise ScanRootError(f"路径不是目录：{directory}")


def _iter_scan_directory(
    directory: Path,
    root: Path,
    cancel_requested: CancelCallback | None,
) -> Iterator[AudioFileEntry]:
    _check_cancelled(cancel_requested)
    try:
        with os.scandir(directory) as iterator:
            entries = []
            while True:
                _check_cancelled(cancel_requested)
                try:
                    entry = next(iterator)
                except StopIteration:
                    break
                _check_cancelled(cancel_requested)
                entries.append(entry)
    except OSError as exc:
        raise ScanAccessError(f"无法读取目录：{directory}") from exc

    entries.sort(key=lambda entry: (entry.name.casefold(), entry.name))
    _check_cancelled(cancel_requested)
    for entry in entries:
        _check_cancelled(cancel_requested)
        entry_path = directory / entry.name
        try:
            metadata = entry.stat(follow_symlinks=False)
            if entry.is_symlink() or _is_reparse_point(metadata):
                continue
            is_directory = entry.is_dir(follow_symlinks=False)
            is_file = entry.is_file(follow_symlinks=False)
        except OSError as exc:
            raise ScanAccessError(f"无法读取路径信息：{entry_path}") from exc

        _check_cancelled(cancel_requested)
        if is_directory:
            _check_cancelled(cancel_requested)
            yield from _iter_scan_directory(entry_path, root, cancel_requested)
            continue
        if not is_file:
            continue

        # Internal recovery artifacts retain an audio extension but are not
        # user library assets and must never be indexed as duplicate songs.
        if entry.name.casefold().startswith(".musicctrl-"):
            continue
        extension = Path(entry.name).suffix.casefold()
        if extension not in SUPPORTED_AUDIO_EXTENSIONS:
            continue
        mtime_ns = int(metadata.st_mtime_ns)
        if mtime_ns < 0:
            raise ScanAccessError(f"文件修改时间无效：{entry_path}")
        result = AudioFileEntry(
            path=entry_path,
            relative_path=entry_path.relative_to(root),
            extension=extension,
            size_bytes=int(metadata.st_size),
            mtime_ns=mtime_ns,
        )
        _check_cancelled(cancel_requested)
        yield result


def iter_audio_files(
    root: Path,
    *,
    allowed_root: Path,
    cancel_requested: CancelCallback | None = None,
) -> Iterator[AudioFileEntry]:
    """Lazily yield supported files with deterministic per-directory ordering."""

    _check_cancelled(cancel_requested)
    absolute_root = _absolute_path(root)
    absolute_allowed_root = _absolute_path(allowed_root)
    _assert_within_boundary(absolute_root, absolute_allowed_root)
    _validate_directory_chain(absolute_root, absolute_allowed_root, cancel_requested)
    _check_cancelled(cancel_requested)
    yield from _iter_scan_directory(absolute_root, absolute_root, cancel_requested)


def enumerate_audio_files(
    root: Path,
    *,
    allowed_root: Path,
    cancel_requested: CancelCallback | None = None,
) -> tuple[AudioFileEntry, ...]:
    """Return supported audio files without following links or reading file content."""

    results = list(
        iter_audio_files(
            root,
            allowed_root=allowed_root,
            cancel_requested=cancel_requested,
        )
    )
    results.sort(
        key=lambda item: (
            item.relative_path.as_posix().casefold(),
            item.relative_path.as_posix(),
        )
    )
    return tuple(results)
