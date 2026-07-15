"""Two-phase, same-directory metadata replacement for P2-C.

This module deliberately does not touch SQLite or Qt.  A caller prepares and
validates a candidate, applies it while retaining the original as a rollback
file, then either finalizes after its database commit or rolls the file back.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import stat
from typing import Any
from uuid import uuid4

from mutagen import File as MutagenFile
from mutagen.flac import FLAC
from mutagen.id3 import ID3, ID3NoHeaderError, TIT2, TPE1
from mutagen.mp4 import MP4

from services.safe_rename import (
    _identity,
    _is_reparse,
    _locked_directory_chain,
    _path_key,
    _within_root,
)


SUPPORTED_WRITE_EXTENSIONS = frozenset({".mp3", ".flac", ".m4a"})


class SafeMetadataError(RuntimeError):
    """Raised when a metadata write cannot be proven safe."""


@dataclass(frozen=True, slots=True)
class MetadataWriteInput:
    asset_id: str
    source_path: Path
    allowed_root: Path
    expected_size_bytes: int
    expected_mtime_ns: int | None
    title: str
    artist: str


@dataclass(frozen=True, slots=True)
class PreparedMetadataWrite:
    request: MetadataWriteInput
    candidate_path: Path
    original_identity: tuple[int, int, int, int]
    candidate_identity: tuple[int, int, int, int]
    original_metadata_json: str


@dataclass(frozen=True, slots=True)
class AppliedMetadataWrite:
    prepared: PreparedMetadataWrite
    backup_path: Path
    applied_identity: tuple[int, int, int, int]


def _validate_request(request: MetadataWriteInput) -> None:
    if not isinstance(request, MetadataWriteInput):
        raise SafeMetadataError("元数据写入必须使用 MetadataWriteInput")
    if not request.asset_id.strip():
        raise SafeMetadataError("asset_id 不能为空")
    if not isinstance(request.source_path, Path) or not request.source_path.is_absolute():
        raise SafeMetadataError("source_path 必须是绝对 Path")
    if not isinstance(request.allowed_root, Path) or not request.allowed_root.is_absolute():
        raise SafeMetadataError("allowed_root 必须是绝对 Path")
    if not _within_root(request.source_path, request.allowed_root):
        raise SafeMetadataError("源文件超出已授权扫描根")
    if request.source_path.suffix.casefold() not in SUPPORTED_WRITE_EXTENSIONS:
        raise SafeMetadataError("元数据写入只支持 MP3、FLAC 和 M4A")
    if request.expected_size_bytes < 0 or (
        request.expected_mtime_ns is not None and request.expected_mtime_ns < 0
    ):
        raise SafeMetadataError("文件指纹不能为负数")
    if not request.title.strip() or not request.artist.strip():
        raise SafeMetadataError("Title 和 Artist 必须同时为非空文本")


def _ordinary_file_metadata(path: Path) -> os.stat_result:
    metadata = os.lstat(path)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or _is_reparse(metadata)
    ):
        raise SafeMetadataError(f"路径不是普通文件：{path}")
    return metadata


def _strict_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True)


def _jsonable_tag_value(value: Any) -> object:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, (list, tuple)):
        return [_jsonable_tag_value(item) for item in value]
    text = getattr(value, "text", None)
    if text is not None:
        return _jsonable_tag_value(text)
    return str(value)


def _metadata_snapshot(stream: Any) -> str:
    media = MutagenFile(stream, easy=True)
    tags = getattr(media, "tags", None) if media is not None else None
    snapshot: dict[str, object] = {}
    if tags is not None:
        for key in sorted(tags.keys(), key=lambda item: str(item).casefold()):
            snapshot[str(key)] = _jsonable_tag_value(tags[key])
    return _strict_json(snapshot)


def _write_tags(path: Path, title: str, artist: str) -> None:
    extension = path.suffix.casefold()
    if extension == ".mp3":
        try:
            tags = ID3(os.fspath(path))
        except ID3NoHeaderError:
            tags = ID3()
        tags.delall("TIT2")
        tags.delall("TPE1")
        tags.add(TIT2(encoding=3, text=[title]))
        tags.add(TPE1(encoding=3, text=[artist]))
        tags.save(os.fspath(path), v2_version=3)
        return
    if extension == ".flac":
        media = FLAC(os.fspath(path))
        media["title"] = [title]
        media["artist"] = [artist]
        media.save()
        return
    if extension == ".m4a":
        media = MP4(os.fspath(path))
        if media.tags is None:
            media.add_tags()
        assert media.tags is not None
        media.tags["\xa9nam"] = [title]
        media.tags["\xa9ART"] = [artist]
        media.save()
        return
    raise SafeMetadataError("元数据写入只支持 MP3、FLAC 和 M4A")


def _read_title_artist(path: Path) -> tuple[str | None, str | None]:
    media = MutagenFile(os.fspath(path), easy=True)
    tags = getattr(media, "tags", None) if media is not None else None
    if tags is None:
        return None, None

    def first(key: str) -> str | None:
        value = tags.get(key)
        if isinstance(value, str):
            return value.strip() or None
        if value:
            return str(value[0]).strip() or None
        return None

    return first("title"), first("artist")


def _copy_candidate(
    source: Path,
    candidate: Path,
    expected_identity: tuple[int, int, int, int],
) -> str:
    with source.open("rb") as reader, candidate.open("xb") as writer:
        if _identity(os.fstat(reader.fileno())) != expected_identity:
            raise SafeMetadataError("安全检查与只读打开之间源文件发生变化")
        original_snapshot = _metadata_snapshot(reader)
        reader.seek(0)
        while True:
            block = reader.read(1024 * 1024)
            if not block:
                break
            writer.write(block)
        writer.flush()
        os.fsync(writer.fileno())
        if _identity(os.fstat(reader.fileno())) != expected_identity:
            raise SafeMetadataError("复制候选副本期间源文件发生变化")
    return original_snapshot


def _temp_path(source: Path, purpose: str) -> Path:
    return source.with_name(f".musicctrl-{purpose}-{uuid4().hex}{source.suffix.lower()}")


def _safe_unlink_created(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError as error:
        raise SafeMetadataError(f"无法清理应用创建的临时文件：{path}") from error


def prepare_metadata_write(request: MetadataWriteInput) -> PreparedMetadataWrite:
    """Create and validate a tagged candidate without changing the source."""

    _validate_request(request)
    candidate = _temp_path(request.source_path, "metadata-candidate")
    with _locked_directory_chain(request.allowed_root, request.source_path.parent):
        source_meta = _ordinary_file_metadata(request.source_path)
        source_identity = _identity(source_meta)
        if source_meta.st_size != request.expected_size_bytes or (
            request.expected_mtime_ns is not None
            and source_meta.st_mtime_ns != request.expected_mtime_ns
        ):
            raise SafeMetadataError("源文件已变化，请重新分析后再同步标签")
        try:
            original_snapshot = _copy_candidate(
                request.source_path,
                candidate,
                source_identity,
            )
            if _identity(_ordinary_file_metadata(request.source_path)) != source_identity:
                raise SafeMetadataError("复制候选副本期间源文件发生变化")
            _write_tags(candidate, request.title.strip(), request.artist.strip())
            with candidate.open("r+b") as candidate_handle:
                candidate_handle.flush()
                os.fsync(candidate_handle.fileno())
            if _read_title_artist(candidate) != (
                request.title.strip(),
                request.artist.strip(),
            ):
                raise SafeMetadataError("候选副本标签回读验证失败")
            candidate_identity = _identity(_ordinary_file_metadata(candidate))
            return PreparedMetadataWrite(
                request=request,
                candidate_path=candidate,
                original_identity=source_identity,
                candidate_identity=candidate_identity,
                original_metadata_json=original_snapshot,
            )
        except Exception:
            _safe_unlink_created(candidate)
            raise


def discard_prepared_metadata(prepared: PreparedMetadataWrite) -> None:
    """Remove an unused candidate while refusing to delete an unknown file."""

    request = prepared.request
    with _locked_directory_chain(request.allowed_root, request.source_path.parent):
        if _identity(_ordinary_file_metadata(prepared.candidate_path)) != prepared.candidate_identity:
            raise SafeMetadataError("候选副本已变化，拒绝自动清理")
        _safe_unlink_created(prepared.candidate_path)


def apply_prepared_metadata(prepared: PreparedMetadataWrite) -> AppliedMetadataWrite:
    """Install a candidate and retain the original for later commit/rollback."""

    request = prepared.request
    backup = _temp_path(request.source_path, "metadata-rollback")
    with _locked_directory_chain(request.allowed_root, request.source_path.parent):
        if _identity(_ordinary_file_metadata(request.source_path)) != prepared.original_identity:
            raise SafeMetadataError("应用候选副本前源文件发生变化")
        if _identity(_ordinary_file_metadata(prepared.candidate_path)) != prepared.candidate_identity:
            raise SafeMetadataError("候选副本在应用前发生变化")
        if backup.exists():
            raise SafeMetadataError("回滚临时路径已存在")
        os.rename(request.source_path, backup)
        try:
            os.rename(prepared.candidate_path, request.source_path)
        except Exception as error:
            os.rename(backup, request.source_path)
            raise SafeMetadataError("候选副本落位失败，已恢复原文件") from error
        try:
            applied_identity = _identity(_ordinary_file_metadata(request.source_path))
            if applied_identity != prepared.candidate_identity:
                raise SafeMetadataError("候选副本落位后身份不一致")
            if _identity(_ordinary_file_metadata(backup)) != prepared.original_identity:
                raise SafeMetadataError("回滚副本身份不一致")
            if _read_title_artist(request.source_path) != (
                request.title.strip(),
                request.artist.strip(),
            ):
                raise SafeMetadataError("落位后的标签回读验证失败")
            return AppliedMetadataWrite(prepared, backup, applied_identity)
        except Exception:
            failed_candidate = prepared.candidate_path
            os.rename(request.source_path, failed_candidate)
            os.rename(backup, request.source_path)
            _safe_unlink_created(failed_candidate)
            raise


def rollback_metadata_write(applied: AppliedMetadataWrite) -> None:
    """Restore the exact original while retaining no candidate artifact."""

    request = applied.prepared.request
    candidate = applied.prepared.candidate_path
    with _locked_directory_chain(request.allowed_root, request.source_path.parent):
        if _identity(_ordinary_file_metadata(request.source_path)) != applied.applied_identity:
            raise SafeMetadataError("回滚前当前文件已变化，拒绝覆盖")
        if _identity(_ordinary_file_metadata(applied.backup_path)) != applied.prepared.original_identity:
            raise SafeMetadataError("回滚副本已变化，拒绝恢复")
        if candidate.exists():
            raise SafeMetadataError("候选临时路径被占用，无法安全回滚")
        os.rename(request.source_path, candidate)
        try:
            os.rename(applied.backup_path, request.source_path)
        except Exception:
            os.rename(candidate, request.source_path)
            raise
        if _identity(_ordinary_file_metadata(request.source_path)) != applied.prepared.original_identity:
            raise SafeMetadataError("恢复后的原文件身份不一致")
        _safe_unlink_created(candidate)


def finalize_metadata_write(applied: AppliedMetadataWrite) -> None:
    """Discard the retained original only after the caller committed its DB state."""

    request = applied.prepared.request
    with _locked_directory_chain(request.allowed_root, request.source_path.parent):
        if _identity(_ordinary_file_metadata(request.source_path)) != applied.applied_identity:
            raise SafeMetadataError("提交清理前当前文件已变化")
        if _identity(_ordinary_file_metadata(applied.backup_path)) != applied.prepared.original_identity:
            raise SafeMetadataError("提交清理前回滚副本已变化")
        _safe_unlink_created(applied.backup_path)
