from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from database import DatabaseConfig
from repositories import AssetUpsert, LibraryRepository, RenamePlanItem
from services.lyrics_match_controller import LyricsMatchController
from services.safe_rename import SafeRenameController


class HistorySourceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.config = DatabaseConfig(self.root / "history.sqlite3")

    def _asset(self, repository: LibraryRepository, name: str, *, kind: str = "audio"):
        path = self.root / name
        path.write_bytes(b"history fixture")
        metadata = path.stat()
        return repository.upsert_asset(
            AssetUpsert(path, metadata.st_size, metadata.st_mtime_ns, kind=kind)
        )

    def test_safe_rename_controller_returns_operations_with_file_items(self) -> None:
        with LibraryRepository(self.config) as repository:
            asset = self._asset(repository, "source.mp3")
            operation, items = repository.create_rename_operation(
                allowed_root=self.root,
                items=(
                    RenamePlanItem(
                        asset.id,
                        asset.canonical_path,
                        self.root / "target.mp3",
                        asset.size_bytes,
                        asset.mtime_ns,
                    ),
                ),
            )
        before = self.config.path.read_bytes()
        controller = SafeRenameController(lambda: LibraryRepository(self.config))

        history = controller.list_history()

        self.assertEqual(history, ((operation, items),))
        self.assertEqual(self.config.path.read_bytes(), before)

    def test_lyrics_controller_returns_real_audio_and_lyric_paths_for_history(self) -> None:
        with LibraryRepository(self.config) as repository:
            audio = self._asset(repository, "song.mp3")
            first_lyric = self._asset(repository, "song-first.lrc", kind="lyric")
            second_lyric = self._asset(repository, "song-second.lrc", kind="lyric")
            matched = repository.commit_lyrics_match(
                audio_asset_id=audio.id,
                lyric_asset_id=first_lyric.id,
                source_kind="external",
                confidence=100,
                method="automatic",
            )
            current = repository.commit_lyrics_match(
                audio_asset_id=audio.id,
                lyric_asset_id=second_lyric.id,
                source_kind="external",
                confidence=90,
                method="manual",
            )
            cancelled = repository.cancel_current_lyrics_match(audio.id)
            self.assertEqual(cancelled.id, current.id)
        before = self.config.path.read_bytes()
        controller = LyricsMatchController(self.config)

        history = controller.list_history()

        self.assertEqual([record["id"] for record in history], [cancelled.id, matched.id])
        self.assertEqual(history[0]["audio_path"], audio.canonical_path)
        self.assertEqual(history[0]["lyric_path"], second_lyric.canonical_path)
        self.assertEqual(history[0]["state"], "cancelled")
        self.assertEqual(history[1]["lyric_path"], first_lyric.canonical_path)
        self.assertEqual(history[1]["state"], "matched")
        self.assertEqual(self.config.path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
