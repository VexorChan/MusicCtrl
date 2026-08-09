"""Safe Windows .lnk creation and inspection within explicit roots."""

from __future__ import annotations

from dataclasses import dataclass
from contextlib import contextmanager
import os
from pathlib import Path
import stat
import re

from services.file_safety import _is_reparse, _locked_directory_chain, _within_root


class ShortcutError(RuntimeError):
    pass


class ShortcutBoundaryError(ShortcutError):
    pass


class ShortcutConflictError(ShortcutError):
    pass


@dataclass(frozen=True, slots=True)
class ShortcutInfo:
    path: Path
    target_path: Path
    working_directory: Path | None
    arguments: str


_INVALID_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED_NAMES = {
    "con", "prn", "aux", "nul",
    *(f"com{index}" for index in range(1, 10)),
    *(f"lpt{index}" for index in range(1, 10)),
}


def _require_absolute_path(path: Path, *, label: str) -> None:
    if not isinstance(path, Path):
        raise TypeError(f"{label}必须使用 pathlib.Path")
    if not path.is_absolute():
        raise ShortcutBoundaryError(f"{label}必须是绝对路径")


def _validate_directory_root(root: Path, *, label: str) -> None:
    _require_absolute_path(root, label=label)
    try:
        metadata = os.lstat(root)
    except OSError as error:
        raise ShortcutBoundaryError(f"{label}不存在或无法访问：{root}") from error
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode) or _is_reparse(metadata):
        raise ShortcutBoundaryError(f"{label}不能是链接或重解析点：{root}")


def _validate_regular_file(path: Path, *, root: Path, label: str) -> os.stat_result:
    _require_absolute_path(path, label=label)
    _validate_directory_root(root, label=f"{label}允许根")
    if not _within_root(path, root):
        raise ShortcutBoundaryError(f"{label}超出允许根：{path}")
    try:
        with _locked_directory_chain(root, path.parent):
            metadata = os.lstat(path)
    except OSError as error:
        raise ShortcutBoundaryError(f"{label}不存在或无法访问：{path}") from error
    if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode) or _is_reparse(metadata):
        raise ShortcutBoundaryError(f"{label}必须是普通文件：{path}")
    return metadata


def _windows_name_key(name: str) -> str:
    return name.rstrip(" .").casefold()


def _ensure_destination_available(path: Path) -> None:
    wanted = _windows_name_key(path.name)
    try:
        entries = tuple(os.scandir(path.parent))
    except OSError as error:
        raise ShortcutBoundaryError(f"无法读取歌单目录：{path.parent}") from error
    if any(_windows_name_key(entry.name) == wanted for entry in entries):
        raise ShortcutConflictError(f"快捷方式目标已存在，禁止覆盖：{path.name}")


def _validate_playlist_name(name: str) -> str:
    if not isinstance(name, str) or not name.strip():
        raise ShortcutBoundaryError("歌单名称不能为空")
    clean = name.strip()
    base_name = clean.split(".", 1)[0].casefold()
    if (
        _INVALID_NAME.search(clean)
        or clean != name
        or clean.endswith((" ", "."))
        or clean in {".", ".."}
        or base_name in _RESERVED_NAMES
    ):
        raise ShortcutBoundaryError("歌单名称包含 Windows 非法字符或保留名称")
    return clean


def _find_playlist_directory(*, playlist_root: Path, name: str) -> Path:
    _validate_directory_root(playlist_root, label="歌单根")
    clean = _validate_playlist_name(name)
    matches: list[str] = []
    try:
        entries = tuple(os.scandir(playlist_root))
    except OSError as error:
        raise ShortcutBoundaryError(f"无法读取歌单根：{playlist_root}") from error
    for entry in entries:
        if _windows_name_key(entry.name) != _windows_name_key(clean):
            continue
        metadata = entry.stat(follow_symlinks=False)
        if entry.is_dir(follow_symlinks=False) and not entry.is_symlink() and not _is_reparse(metadata):
            matches.append(entry.name)
    if len(matches) != 1:
        raise ShortcutBoundaryError("歌单不存在、名称不唯一或不是安全目录")
    return playlist_root / matches[0]


def _shell():
    if os.name != "nt":
        raise ShortcutError("Windows 快捷方式只支持 Windows")
    try:
        import win32com.client

        return win32com.client.Dispatch("WScript.Shell")
    except Exception as error:
        raise ShortcutError("无法启动 Windows 快捷方式组件") from error


@contextmanager
def _com_scope():
    if os.name != "nt":
        raise ShortcutError("Windows 快捷方式只支持 Windows")
    try:
        import pythoncom

        pythoncom.CoInitialize()
    except Exception as error:
        raise ShortcutError("无法初始化 Windows COM 组件") from error
    try:
        yield
    finally:
        pythoncom.CoUninitialize()


def _read_shortcut_unlocked(path: Path) -> ShortcutInfo:
    try:
        with _com_scope():
            shell = _shell()
            shortcut = shell.CreateShortcut(os.fspath(path))
            target_text = str(shortcut.TargetPath).strip()
            working_text = str(shortcut.WorkingDirectory).strip()
            arguments = str(shortcut.Arguments)
            del shortcut
            del shell
    except Exception as error:
        raise ShortcutError(f"快捷方式损坏或无法读取：{path.name}") from error
    if not target_text:
        raise ShortcutError(f"快捷方式缺少目标：{path.name}")
    target = Path(target_text)
    if not target.is_absolute():
        raise ShortcutError(f"快捷方式目标不是绝对路径：{path.name}")
    working = Path(working_text) if working_text else None
    if working is not None and not working.is_absolute():
        raise ShortcutError(f"快捷方式工作目录不是绝对路径：{path.name}")
    return ShortcutInfo(path, target, working, arguments)


def read_shortcut(path: Path, *, playlist_root: Path) -> ShortcutInfo:
    if path.suffix.casefold() != ".lnk":
        raise ShortcutBoundaryError("只允许读取 .lnk 快捷方式")
    _validate_regular_file(path, root=playlist_root, label="快捷方式")
    return _read_shortcut_unlocked(path)


def create_shortcut(
    *,
    target_path: Path,
    audio_root: Path,
    shortcut_path: Path,
    playlist_root: Path,
) -> ShortcutInfo:
    """Create one verified .lnk without overwriting an existing path."""

    if shortcut_path.suffix.casefold() != ".lnk":
        raise ShortcutBoundaryError("快捷方式文件必须使用 .lnk 扩展名")
    _validate_regular_file(target_path, root=audio_root, label="音频目标")
    _validate_directory_root(playlist_root, label="歌单根")
    _require_absolute_path(shortcut_path, label="快捷方式路径")
    if not _within_root(shortcut_path, playlist_root):
        raise ShortcutBoundaryError("快捷方式路径超出歌单根")
    with _locked_directory_chain(playlist_root, shortcut_path.parent):
        _ensure_destination_available(shortcut_path)
        created = False
        try:
            with _com_scope():
                shell = _shell()
                shortcut = shell.CreateShortcut(os.fspath(shortcut_path))
                shortcut.TargetPath = os.fspath(target_path)
                shortcut.WorkingDirectory = os.fspath(target_path.parent)
                shortcut.Arguments = ""
                shortcut.Save()
                del shortcut
                del shell
            created = True
            metadata = os.lstat(shortcut_path)
            if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode) or _is_reparse(metadata):
                raise ShortcutError("快捷方式写入后不是普通文件")
            info = _read_shortcut_unlocked(shortcut_path)
            if os.path.normcase(os.path.normpath(os.fspath(info.target_path))) != os.path.normcase(
                os.path.normpath(os.fspath(target_path))
            ):
                raise ShortcutError("快捷方式回读目标与请求目标不一致")
            return info
        except Exception:
            if created:
                try:
                    metadata = os.lstat(shortcut_path)
                    if stat.S_ISREG(metadata.st_mode) and not stat.S_ISLNK(metadata.st_mode) and not _is_reparse(metadata):
                        os.unlink(shortcut_path)
                except OSError:
                    pass
            raise


def create_playlist_directory(*, playlist_root: Path, name: str) -> Path:
    _validate_directory_root(playlist_root, label="歌单根")
    clean = _validate_playlist_name(name)
    existing = {
        _windows_name_key(entry.name): entry
        for entry in os.scandir(playlist_root)
    }
    key = _windows_name_key(clean)
    if key in existing:
        entry = existing[key]
        metadata = entry.stat(follow_symlinks=False)
        if entry.is_dir(follow_symlinks=False) and not entry.is_symlink() and not _is_reparse(metadata):
            return playlist_root / entry.name
        raise ShortcutConflictError("同名歌单路径已存在且不是安全目录")
    path = playlist_root / clean
    try:
        path.mkdir()
    except OSError as error:
        raise ShortcutError(f"无法创建歌单目录：{clean}") from error
    return path


def rename_playlist_directory(
    *,
    playlist_root: Path,
    current_name: str,
    new_name: str,
) -> Path:
    """Rename one ordinary playlist directory without overwriting another path."""

    clean = _validate_playlist_name(new_name)
    source = _find_playlist_directory(playlist_root=playlist_root, name=current_name)
    destination = playlist_root / clean
    source_key = _windows_name_key(source.name)
    destination_key = _windows_name_key(clean)
    try:
        entries = tuple(os.scandir(playlist_root))
    except OSError as error:
        raise ShortcutBoundaryError(f"无法读取歌单根：{playlist_root}") from error
    if any(
        _windows_name_key(entry.name) == destination_key
        and _windows_name_key(entry.name) != source_key
        for entry in entries
    ):
        raise ShortcutConflictError("目标歌单名称已存在，禁止覆盖")
    if source.name == clean:
        return source
    # Lock the managed root, but not the directory being renamed: on Windows an
    # open handle to the leaf directory itself prevents os.rename from succeeding.
    with _locked_directory_chain(playlist_root, source.parent):
        metadata = os.lstat(source)
        if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode) or _is_reparse(metadata):
            raise ShortcutBoundaryError("歌单目录已变化，拒绝重命名")
        try:
            os.rename(source, destination)
        except OSError as error:
            raise ShortcutError(f"无法重命名歌单：{source.name}") from error
    return destination


def inspect_playlist_shortcuts(
    *,
    playlist_root: Path,
    name: str,
    allowed_targets: tuple[Path, ...],
) -> tuple[ShortcutInfo, ...]:
    """Preflight a playlist deletion and return only verified managed shortcuts."""

    folder = _find_playlist_directory(playlist_root=playlist_root, name=name)
    allowed = {
        os.path.normcase(os.path.normpath(os.fspath(path)))
        for path in allowed_targets
    }
    infos: list[ShortcutInfo] = []
    try:
        entries = tuple(os.scandir(folder))
    except OSError as error:
        raise ShortcutBoundaryError(f"无法读取歌单目录：{folder}") from error
    for entry in entries:
        metadata = entry.stat(follow_symlinks=False)
        if (
            not entry.is_file(follow_symlinks=False)
            or entry.is_symlink()
            or _is_reparse(metadata)
            or not entry.name.casefold().endswith(".lnk")
        ):
            raise ShortcutBoundaryError(f"歌单包含未知或不安全项目：{entry.name}")
        # read_shortcut obtains its own verified directory-chain lock and
        # rechecks the file immediately before COM reads it.
        info = read_shortcut(folder / entry.name, playlist_root=playlist_root)
        target_key = os.path.normcase(os.path.normpath(os.fspath(info.target_path)))
        if target_key not in allowed:
            raise ShortcutBoundaryError(
                f"快捷方式目标不是 active 音频，拒绝删除歌单：{entry.name}"
            )
        infos.append(info)
    return tuple(sorted(infos, key=lambda item: (item.path.name.casefold(), item.path.name)))


def remove_empty_playlist_directory(*, playlist_root: Path, name: str) -> None:
    folder = _find_playlist_directory(playlist_root=playlist_root, name=name)
    # As with rename, do not hold an open handle to the leaf directory while
    # asking Windows to remove that directory.
    with _locked_directory_chain(playlist_root, folder.parent):
        try:
            with os.scandir(folder) as entries:
                if next(entries, None) is not None:
                    raise ShortcutConflictError("歌单目录仍包含项目，拒绝删除目录")
            os.rmdir(folder)
        except ShortcutError:
            raise
        except OSError as error:
            raise ShortcutError(f"无法删除空歌单目录：{folder.name}") from error


def remove_shortcut(
    *,
    shortcut_path: Path,
    playlist_root: Path,
    expected_target: Path,
) -> None:
    info = read_shortcut(shortcut_path, playlist_root=playlist_root)
    if os.path.normcase(os.path.normpath(os.fspath(info.target_path))) != os.path.normcase(
        os.path.normpath(os.fspath(expected_target))
    ):
        raise ShortcutBoundaryError("快捷方式目标已变化，拒绝移除")
    with _locked_directory_chain(playlist_root, shortcut_path.parent):
        metadata = os.lstat(shortcut_path)
        if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode) or _is_reparse(metadata):
            raise ShortcutBoundaryError("快捷方式不再是普通文件，拒绝移除")
        try:
            os.unlink(shortcut_path)
        except OSError as error:
            raise ShortcutError(f"无法移除快捷方式：{shortcut_path.name}") from error
