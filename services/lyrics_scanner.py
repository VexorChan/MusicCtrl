"""Read-only LRC discovery, decoding and deterministic candidate matching."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, replace
import os
from pathlib import Path
import re
import stat

from services.file_safety import (
    _identity,
    _is_reparse,
    _locked_directory_chain,
    _path_key,
    _within_root,
)


class LyricsScanError(RuntimeError):
    """Base error for an unsafe or unreadable lyrics scan."""


class LyricsScanCancelled(LyricsScanError):
    pass


CancelCallback = Callable[[], bool]


@dataclass(frozen=True, slots=True)
class LyricsFileEntry:
    path: Path
    relative_path: Path
    size_bytes: int
    mtime_ns: int
    encoding: str
    text: str
    title: str | None
    artist: str | None


@dataclass(frozen=True, slots=True)
class AudioLyricsInput:
    asset_id: str
    title: str
    artist: str
    has_embedded_lyrics: bool = False


@dataclass(frozen=True, slots=True)
class LyricsMatchCandidate:
    audio_asset_id: str
    lyric_path: Path | None
    lyric_title: str | None
    lyric_artist: str | None
    source_kind: str
    confidence: int
    status: str
    requires_confirmation: bool
    message: str


_TAG_PATTERN = re.compile(r"^\[(ti|ar):\s*(.*?)\s*\]$", re.IGNORECASE)


def _check_cancelled(cancel_requested: CancelCallback | None) -> None:
    if cancel_requested is not None and cancel_requested():
        raise LyricsScanCancelled("歌词扫描已取消")


def _validate_root(root: Path, allowed_root: Path) -> tuple[Path, Path]:
    if not isinstance(root, Path) or not isinstance(allowed_root, Path):
        raise LyricsScanError("扫描路径必须使用 pathlib.Path")
    if not root.is_absolute() or not allowed_root.is_absolute():
        raise LyricsScanError("扫描路径和允许根必须是绝对路径")
    if not _within_root(root, allowed_root):
        raise LyricsScanError("歌词扫描路径超出允许根")
    current = allowed_root
    relative = Path(os.path.relpath(root, allowed_root))
    paths = [allowed_root]
    if relative != Path("."):
        for part in relative.parts:
            if part in {"", ".", ".."}:
                raise LyricsScanError("歌词扫描目录链无效")
            current /= part
            paths.append(current)
    for path in paths:
        try:
            metadata = os.lstat(path)
        except OSError as error:
            raise LyricsScanError(f"无法读取歌词目录：{path}") from error
        if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode) or _is_reparse(metadata):
            raise LyricsScanError(f"歌词目录不能是链接或重解析点：{path}")
    return root, allowed_root


def _decode_lrc(data: bytes) -> tuple[str, str]:
    if data.startswith(b"\xef\xbb\xbf"):
        return data.decode("utf-8-sig"), "utf-8-sig"
    attempts: list[tuple[int, str, str]] = []
    for encoding in ("utf-8", "gb18030", "big5"):
        try:
            text = data.decode(encoding)
        except UnicodeDecodeError:
            continue
        # Prefer text that exposes LRC metadata/timestamps and contains fewer
        # control characters.  Stable encoding order resolves equal scores.
        lowered = text.casefold()
        score = 20 * int("[ti:" in lowered) + 20 * int("[ar:" in lowered)
        score += min(20, lowered.count("["))
        score -= sum(ord(char) < 32 and char not in "\r\n\t" for char in text) * 10
        score -= sum(0xE000 <= ord(char) <= 0xF8FF for char in text) * 25
        attempts.append((score, encoding, text))
    if not attempts:
        raise LyricsScanError("歌词编码无法识别（支持 UTF-8、GBK/GB18030、Big5）")
    attempts.sort(key=lambda item: item[0], reverse=True)
    _score, encoding, text = attempts[0]
    return text, encoding


def _parse_identity(path: Path, text: str) -> tuple[str | None, str | None]:
    title: str | None = None
    artist: str | None = None
    for line in text.splitlines()[:100]:
        match = _TAG_PATTERN.match(line.strip())
        if match is None:
            continue
        value = match.group(2).strip()
        if not value:
            continue
        if match.group(1).casefold() == "ti" and title is None:
            title = value
        elif match.group(1).casefold() == "ar" and artist is None:
            artist = value
        if title is not None and artist is not None:
            break
    if title is not None and artist is not None:
        return title, artist
    stem = path.stem
    if "-" not in stem:
        return title, artist
    fallback_title, fallback_artist = (part.strip() for part in stem.rsplit("-", 1))
    return title or fallback_title or None, artist or fallback_artist or None


def _read_lrc(path: Path, root: Path) -> LyricsFileEntry:
    with _locked_directory_chain(root, path.parent):
        before = os.lstat(path)
        if not stat.S_ISREG(before.st_mode) or stat.S_ISLNK(before.st_mode) or _is_reparse(before):
            raise LyricsScanError(f"歌词路径不是普通文件：{path}")
        with path.open("rb") as stream:
            if _identity(os.fstat(stream.fileno())) != _identity(before):
                raise LyricsScanError(f"安全检查与打开之间歌词发生变化：{path}")
            data = stream.read()
            if _identity(os.fstat(stream.fileno())) != _identity(before):
                raise LyricsScanError(f"读取期间歌词发生变化：{path}")
        after = os.lstat(path)
        if _identity(after) != _identity(before):
            raise LyricsScanError(f"读取后歌词路径发生变化：{path}")
    text, encoding = _decode_lrc(data)
    title, artist = _parse_identity(path, text)
    return LyricsFileEntry(
        path=path,
        relative_path=path.relative_to(root),
        size_bytes=int(before.st_size),
        mtime_ns=int(before.st_mtime_ns),
        encoding=encoding,
        text=text,
        title=title,
        artist=artist,
    )


def iter_lrc_files(
    root: Path,
    *,
    allowed_root: Path,
    cancel_requested: CancelCallback | None = None,
) -> Iterator[LyricsFileEntry]:
    root, _allowed_root = _validate_root(root, allowed_root)
    stack = [root]
    while stack:
        _check_cancelled(cancel_requested)
        directory = stack.pop()
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
        except OSError as error:
            raise LyricsScanError(f"无法读取歌词目录：{directory}") from error
        entries.sort(key=lambda entry: (entry.name.casefold(), entry.name), reverse=True)
        for entry in entries:
            _check_cancelled(cancel_requested)
            path = directory / entry.name
            try:
                metadata = entry.stat(follow_symlinks=False)
                if entry.is_symlink() or _is_reparse(metadata):
                    continue
                if entry.is_dir(follow_symlinks=False):
                    stack.append(path)
                    continue
                if not entry.is_file(follow_symlinks=False):
                    continue
            except OSError as error:
                raise LyricsScanError(f"无法读取歌词路径：{path}") from error
            if entry.name.casefold().startswith(".musicctrl-") or path.suffix.casefold() != ".lrc":
                continue
            _check_cancelled(cancel_requested)
            yield _read_lrc(path, root)


def enumerate_lrc_files(
    root: Path,
    *,
    allowed_root: Path,
    cancel_requested: CancelCallback | None = None,
) -> tuple[LyricsFileEntry, ...]:
    return tuple(
        sorted(
            iter_lrc_files(root, allowed_root=allowed_root, cancel_requested=cancel_requested),
            key=lambda item: (item.relative_path.as_posix().casefold(), item.relative_path.as_posix()),
        )
    )


def _normalized(value: str | None) -> str:
    return "" if value is None else re.sub(r"\s+", "", value).casefold()


def build_lyrics_candidates(
    audio_items: Sequence[AudioLyricsInput],
    lyric_items: Sequence[LyricsFileEntry],
) -> tuple[LyricsMatchCandidate, ...]:
    candidates: list[LyricsMatchCandidate] = []
    for audio in audio_items:
        if not isinstance(audio, AudioLyricsInput) or not audio.asset_id.strip():
            raise LyricsScanError("音频候选输入无效")
        if audio.has_embedded_lyrics:
            candidates.append(
                LyricsMatchCandidate(
                    audio.asset_id, None, audio.title, audio.artist, "embedded", 100,
                    "已有内嵌歌词", False, "内嵌歌词优先，不自动绑定外部 LRC",
                )
            )
            continue
        audio_title = _normalized(audio.title)
        audio_artist = _normalized(audio.artist)
        ranked: list[tuple[int, LyricsFileEntry]] = []
        for lyric in lyric_items:
            lyric_title = _normalized(lyric.title)
            lyric_artist = _normalized(lyric.artist)
            confidence = 0
            if audio_title and lyric_title == audio_title:
                confidence = 70
                if audio_artist and lyric_artist == audio_artist:
                    confidence = 100
                elif not audio_artist or not lyric_artist:
                    confidence = 82
                elif audio_artist in lyric_artist or lyric_artist in audio_artist:
                    confidence = 90
            elif audio_title and lyric_title and (audio_title in lyric_title or lyric_title in audio_title):
                confidence = 65
            if confidence:
                ranked.append((confidence, lyric))
        ranked.sort(key=lambda pair: (-pair[0], pair[1].relative_path.as_posix().casefold(), pair[1].relative_path.as_posix()))
        for confidence, lyric in ranked:
            candidates.append(
                LyricsMatchCandidate(
                    audio.asset_id,
                    lyric.path,
                    lyric.title,
                    lyric.artist,
                    "external",
                    confidence,
                    "待提交" if confidence >= 95 else "待人工确认",
                    confidence < 95,
                    "高置信度候选" if confidence >= 95 else "置信度不足，禁止自动提交",
                )
            )

    claims: dict[str, set[str]] = {}
    for candidate in candidates:
        if candidate.lyric_path is not None and candidate.confidence >= 95:
            claims.setdefault(_path_key(candidate.lyric_path), set()).add(candidate.audio_asset_id)
    conflicts = {key for key, asset_ids in claims.items() if len(asset_ids) > 1}
    return tuple(
        replace(
            candidate,
            status="冲突",
            requires_confirmation=True,
            message="同一外部 LRC 被多个音频高置信度候选占用，必须人工处理",
        )
        if candidate.lyric_path is not None and _path_key(candidate.lyric_path) in conflicts
        else candidate
        for candidate in candidates
    )
