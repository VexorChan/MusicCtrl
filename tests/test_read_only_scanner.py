from __future__ import annotations

import builtins
import os
from pathlib import Path
import stat
import subprocess
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from services.read_only_scanner import (
    SUPPORTED_AUDIO_EXTENSIONS,
    ScanAccessError,
    ScanBoundaryError,
    ScanRootError,
    _is_reparse_point,
    enumerate_audio_files,
)


class ReadOnlyScannerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.allowed_root = Path(self.temporary_directory.name)
        self.scan_root = self.allowed_root / "scan"
        self.scan_root.mkdir()

    def touch(self, relative_path: str) -> Path:
        path = self.scan_root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
        return path

    def scan(self):
        return enumerate_audio_files(self.scan_root, allowed_root=self.allowed_root)

    def create_directory_link(self, link: Path, target: Path) -> None:
        try:
            link.symlink_to(target, target_is_directory=True)
            return
        except OSError as symlink_error:
            if os.name != "nt":
                self.skipTest(f"当前环境无法创建目录符号链接：{symlink_error}")
        completed = subprocess.run(
            ["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if completed.returncode != 0:
            self.skipTest(f"当前环境无法创建目录 junction：{completed.stderr.strip()}")

    def test_recognizes_six_supported_extensions_case_insensitively(self) -> None:
        names = ("a.MP3", "b.flac", "c.WaV", "d.M4A", "e.ogg", "f.AAC")
        for name in names:
            self.touch(name)

        results = self.scan()

        self.assertEqual({item.extension for item in results}, SUPPORTED_AUDIO_EXTENSIONS)
        self.assertEqual(len(results), 6)

    def test_ignores_unsupported_extensions(self) -> None:
        self.touch("keep.mp3")
        self.touch("ignore.txt")
        self.touch("ignore.lrc")
        self.touch("no_extension")

        self.assertEqual([item.relative_path.as_posix() for item in self.scan()], ["keep.mp3"])

    def test_recurses_and_returns_stable_case_insensitive_order(self) -> None:
        for name in ("zeta/Track.ogg", "Alpha/song.wav", "beta/A.MP3", "beta/b.flac"):
            self.touch(name)

        first = self.scan()
        second = self.scan()
        relative_paths = [item.relative_path.as_posix() for item in first]

        self.assertEqual(first, second)
        self.assertEqual(relative_paths, sorted(relative_paths, key=lambda value: (value.casefold(), value)))
        self.assertTrue(all(item.path.is_absolute() for item in first))

    def test_empty_directory_returns_empty_tuple(self) -> None:
        self.assertEqual(self.scan(), ())

    def test_rejects_root_outside_allowed_boundary(self) -> None:
        with TemporaryDirectory() as outside_directory:
            outside_root = Path(outside_directory)
            with self.assertRaisesRegex(ScanBoundaryError, "不在允许范围内"):
                enumerate_audio_files(outside_root, allowed_root=self.allowed_root)

    def test_reports_missing_root(self) -> None:
        missing = self.allowed_root / "missing"
        with self.assertRaisesRegex(ScanRootError, "目录不存在"):
            enumerate_audio_files(missing, allowed_root=self.allowed_root)

    def test_reports_root_that_is_a_file(self) -> None:
        root_file = self.allowed_root / "not-a-directory"
        root_file.touch()
        with self.assertRaisesRegex(ScanRootError, "路径不是目录"):
            enumerate_audio_files(root_file, allowed_root=self.allowed_root)

    def test_rejects_root_symlink_when_platform_allows_creation(self) -> None:
        target = self.allowed_root / "target"
        target.mkdir()
        link = self.allowed_root / "linked-root"
        self.create_directory_link(link, target)

        with self.assertRaisesRegex(ScanBoundaryError, "符号链接或重解析点"):
            enumerate_audio_files(link, allowed_root=self.allowed_root)

    def test_does_not_enter_directory_symlink(self) -> None:
        with TemporaryDirectory() as outside_directory:
            outside_root = Path(outside_directory)
            (outside_root / "outside.mp3").touch()
            link = self.scan_root / "linked-directory"
            self.create_directory_link(link, outside_root)

            self.assertEqual(self.scan(), ())

    def test_detects_windows_reparse_attribute_with_standard_stat_flag(self) -> None:
        metadata = SimpleNamespace(st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT)
        self.assertTrue(_is_reparse_point(metadata))
        self.assertFalse(_is_reparse_point(SimpleNamespace(st_file_attributes=0)))

    def test_does_not_open_file_content(self) -> None:
        self.touch("silent.mp3")
        with patch.object(builtins, "open", side_effect=AssertionError("file content was opened")):
            results = self.scan()
        self.assertEqual([item.relative_path.as_posix() for item in results], ["silent.mp3"])

    def test_wraps_directory_access_errors(self) -> None:
        with patch("services.read_only_scanner.os.scandir", side_effect=PermissionError("denied")):
            with self.assertRaisesRegex(ScanAccessError, "无法读取目录"):
                self.scan()


if __name__ == "__main__":
    unittest.main()
