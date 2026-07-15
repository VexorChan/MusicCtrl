from __future__ import annotations

import os
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import unittest
import wave

from mutagen.id3 import USLT
from mutagen.wave import WAVE

from services.lyrics_scanner import (
    AudioLyricsInput,
    LyricsScanCancelled,
    LyricsScanError,
    build_lyrics_candidates,
    detect_embedded_lyrics,
    enumerate_lrc_files,
    iter_lrc_files,
)


class LyricsScannerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _lrc(self, name: str, text: str, encoding: str = "utf-8") -> Path:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode(encoding))
        return path

    def test_only_lrc_is_read_in_stable_order_and_bytes_are_unchanged(self) -> None:
        first = self._lrc("b/晴天-周杰伦.lrc", "[ti:晴天]\n[ar:周杰伦]\n[00:01]歌词")
        second = self._lrc("A/富士山下-陈奕迅.LRC", "[ti:富士山下]\n[ar:陈奕迅]", "utf-8-sig")
        ignored = self._lrc("ignore.txt", "[ti:不应读取]")
        snapshots = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in (first, second, ignored)}

        result = enumerate_lrc_files(self.root, allowed_root=self.root)

        self.assertEqual([item.relative_path.as_posix() for item in result], ["A/富士山下-陈奕迅.LRC", "b/晴天-周杰伦.lrc"])
        self.assertEqual([(item.title, item.artist) for item in result], [("富士山下", "陈奕迅"), ("晴天", "周杰伦")])
        for path, snapshot in snapshots.items():
            self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), snapshot)

    def test_embedded_lyrics_use_verified_read_only_handle_and_preserve_audio(self) -> None:
        path = self.root / "embedded.wav"
        with wave.open(os.fspath(path), "wb") as stream:
            stream.setnchannels(1)
            stream.setsampwidth(1)
            stream.setframerate(8000)
            stream.writeframes(b"\x80" * 32)
        media = WAVE(os.fspath(path))
        media.add_tags()
        media.tags.add(USLT(encoding=3, lang="zho", desc="", text="内嵌歌词"))
        media.save()
        snapshot = (path.read_bytes(), path.stat().st_size, path.stat().st_mtime_ns)

        self.assertTrue(detect_embedded_lyrics(path, allowed_root=self.root))
        self.assertEqual(
            (path.read_bytes(), path.stat().st_size, path.stat().st_mtime_ns),
            snapshot,
        )
        with self.assertRaises(LyricsScanError):
            detect_embedded_lyrics(path, allowed_root=self.root / "other")

    def test_utf8_bom_gb18030_and_big5_are_decoded_without_writing(self) -> None:
        paths = (
            self._lrc("utf8.lrc", "[ti:晴天]\n[ar:周杰伦]", "utf-8-sig"),
            self._lrc("gbk.lrc", "[ti:月半小夜曲]\n[ar:李克勤]", "gb18030"),
            self._lrc("big5.lrc", "[ti:海闊天空]\n[ar:Beyond]", "big5"),
        )
        before = {path: path.read_bytes() for path in paths}

        result = enumerate_lrc_files(self.root, allowed_root=self.root)

        self.assertEqual(len(result), 3)
        self.assertEqual({item.title for item in result}, {"晴天", "月半小夜曲", "海闊天空"})
        self.assertEqual({path: path.read_bytes() for path in paths}, before)

    def test_filename_fallback_uses_last_hyphen(self) -> None:
        self._lrc("歌-名-歌手.lrc", "[00:01]只有正文")
        item = enumerate_lrc_files(self.root, allowed_root=self.root)[0]
        self.assertEqual((item.title, item.artist), ("歌-名", "歌手"))

    def test_root_escape_and_directory_symlink_are_rejected_or_skipped(self) -> None:
        outside = self.root.parent / "outside-lyrics"
        with self.assertRaises(LyricsScanError):
            enumerate_lrc_files(outside, allowed_root=self.root)
        target = self.root / "target"
        target.mkdir()
        self._lrc("target/escape.lrc", "[ti:escape]")
        link = self.root / "link"
        try:
            link.symlink_to(target, target_is_directory=True)
        except OSError:
            return
        result = enumerate_lrc_files(self.root, allowed_root=self.root)
        self.assertEqual([item.relative_path.as_posix() for item in result], ["target/escape.lrc"])

    def test_cancel_stops_before_publishing_remaining_items(self) -> None:
        self._lrc("a.lrc", "[ti:a]")
        self._lrc("b.lrc", "[ti:b]")
        cancelled = threading.Event()
        iterator = iter_lrc_files(self.root, allowed_root=self.root, cancel_requested=cancelled.is_set)
        next(iterator)
        cancelled.set()
        with self.assertRaises(LyricsScanCancelled):
            next(iterator)

    def test_candidates_prioritize_embedded_and_mark_low_confidence_and_conflicts(self) -> None:
        lyric = self._lrc("晴天-周杰伦.lrc", "[ti:晴天]\n[ar:周杰伦]")
        low = self._lrc("晴天-其他歌手.lrc", "[ti:晴天]\n[ar:其他歌手]")
        entries = enumerate_lrc_files(self.root, allowed_root=self.root)
        candidates = build_lyrics_candidates(
            (
                AudioLyricsInput("embedded", "内嵌歌", "歌手", True),
                AudioLyricsInput("exact", "晴天", "周杰伦"),
                AudioLyricsInput("duplicate", "晴天", "周杰伦"),
            ),
            entries,
        )
        embedded = next(item for item in candidates if item.audio_asset_id == "embedded")
        self.assertEqual((embedded.source_kind, embedded.status), ("embedded", "已有内嵌歌词"))
        exact = [item for item in candidates if item.audio_asset_id == "exact"]
        self.assertTrue(any(item.lyric_path == low and item.requires_confirmation for item in exact))
        self.assertTrue(any(item.lyric_path == lyric and item.status == "冲突" for item in exact))
        self.assertTrue(all(item.requires_confirmation for item in candidates if item.lyric_path == lyric))


if __name__ == "__main__":
    unittest.main()
