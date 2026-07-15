from __future__ import annotations

import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from services.windows_shortcuts import (
    ShortcutBoundaryError,
    ShortcutConflictError,
    create_shortcut,
    read_shortcut,
)


@unittest.skipUnless(os.name == "nt", "Windows .lnk integration requires Windows")
class WindowsShortcutTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.audio_root = self.root / "audio"
        self.playlist_root = self.root / "playlists"
        self.audio_root.mkdir()
        self.playlist_root.mkdir()
        self.target = self.audio_root / "晴天-周杰伦.mp3"
        self.target.write_bytes(b"temporary audio fixture")

    def test_real_shortcut_round_trip_preserves_target_and_audio(self) -> None:
        destination = self.playlist_root / "通勤" / "晴天-周杰伦.lnk"
        destination.parent.mkdir()
        before = (self.target.read_bytes(), self.target.stat().st_mtime_ns)

        created = create_shortcut(
            target_path=self.target,
            audio_root=self.audio_root,
            shortcut_path=destination,
            playlist_root=self.playlist_root,
        )
        loaded = read_shortcut(destination, playlist_root=self.playlist_root)

        self.assertEqual(created, loaded)
        self.assertEqual(os.path.normcase(str(loaded.target_path)), os.path.normcase(str(self.target)))
        self.assertEqual(loaded.working_directory, self.target.parent)
        self.assertEqual(loaded.arguments, "")
        self.assertEqual((self.target.read_bytes(), self.target.stat().st_mtime_ns), before)

    def test_existing_and_windows_equivalent_destination_are_not_overwritten(self) -> None:
        destination = self.playlist_root / "song.lnk"
        destination.write_bytes(b"sentinel")
        before = destination.read_bytes()
        with self.assertRaises(ShortcutConflictError):
            create_shortcut(
                target_path=self.target,
                audio_root=self.audio_root,
                shortcut_path=self.playlist_root / "SONG.LNK",
                playlist_root=self.playlist_root,
            )
        self.assertEqual(destination.read_bytes(), before)

    def test_root_escape_relative_paths_and_wrong_extension_fail_closed(self) -> None:
        outside = self.root / "outside.lnk"
        cases = (
            dict(shortcut_path=outside, target_path=self.target),
            dict(shortcut_path=self.playlist_root / "bad.url", target_path=self.target),
            dict(shortcut_path=self.playlist_root / "bad.lnk", target_path=self.root / "outside.mp3"),
        )
        (self.root / "outside.mp3").write_bytes(b"outside")
        for case in cases:
            with self.subTest(case=case):
                with self.assertRaises(ShortcutBoundaryError):
                    create_shortcut(
                        target_path=case["target_path"],
                        audio_root=self.audio_root,
                        shortcut_path=case["shortcut_path"],
                        playlist_root=self.playlist_root,
                    )
        self.assertFalse(outside.exists())

    def test_read_rejects_non_lnk_and_root_escape(self) -> None:
        text = self.playlist_root / "note.txt"
        text.write_text("x", encoding="utf-8")
        with self.assertRaises(ShortcutBoundaryError):
            read_shortcut(text, playlist_root=self.playlist_root)
        outside = self.root / "outside.lnk"
        outside.write_bytes(b"not a shortcut")
        with self.assertRaises(ShortcutBoundaryError):
            read_shortcut(outside, playlist_root=self.playlist_root)


if __name__ == "__main__":
    unittest.main()
