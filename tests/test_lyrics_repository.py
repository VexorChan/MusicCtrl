from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import unittest

from database import DatabaseConfig
from repositories import (
    AssetUpsert,
    LibraryRepository,
    RecordNotFoundError,
    RepositoryClosedError,
    RepositoryDataError,
    RepositoryThreadError,
)


class LyricsRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.repository = LibraryRepository(DatabaseConfig(self.root / "library.sqlite3"))

    def tearDown(self) -> None:
        try:
            self.repository.close()
        except Exception:
            pass
        self.temporary.cleanup()

    def _asset(self, name: str, *, kind: str):
        path = self.root / name
        path.write_bytes(b"fixture")
        metadata = path.stat()
        return self.repository.upsert_asset(
            AssetUpsert(path, metadata.st_size, metadata.st_mtime_ns, kind=kind)
        )

    def test_external_match_replacement_retains_history_and_current_uniqueness(self) -> None:
        audio = self._asset("song.mp3", kind="audio")
        first = self._asset("first.lrc", kind="lyric")
        second = self._asset("second.lrc", kind="lyric")

        old = self.repository.commit_lyrics_match(
            audio_asset_id=audio.id,
            lyric_asset_id=first.id,
            source_kind="external",
            confidence=100,
            method="automatic",
        )
        new = self.repository.commit_lyrics_match(
            audio_asset_id=audio.id,
            lyric_asset_id=second.id,
            source_kind="external",
            confidence=80,
            method="manual",
        )

        history = self.repository.list_lyrics_matches(audio_asset_id=audio.id)
        self.assertEqual([item.id for item in history], [old.id, new.id])
        self.assertEqual([item.is_current for item in history], [False, True])
        self.assertEqual(self.repository.list_lyrics_matches(current_only=True), (new,))

    def test_one_external_lrc_cannot_be_current_for_two_audio_assets(self) -> None:
        first_audio = self._asset("first.mp3", kind="audio")
        second_audio = self._asset("second.mp3", kind="audio")
        lyric = self._asset("shared.lrc", kind="lyric")
        self.repository.commit_lyrics_match(
            audio_asset_id=first_audio.id,
            lyric_asset_id=lyric.id,
            source_kind="external",
            confidence=100,
            method="automatic",
        )

        with self.assertRaisesRegex(RepositoryDataError, "占用"):
            self.repository.commit_lyrics_match(
                audio_asset_id=second_audio.id,
                lyric_asset_id=lyric.id,
                source_kind="external",
                confidence=100,
                method="automatic",
            )
        self.assertEqual(len(self.repository.list_lyrics_matches()), 1)

    def test_embedded_lyrics_take_priority_and_external_cannot_replace_them(self) -> None:
        audio = self._asset("embedded.mp3", kind="audio")
        lyric = self._asset("external.lrc", kind="lyric")
        embedded = self.repository.commit_lyrics_match(
            audio_asset_id=audio.id,
            lyric_asset_id=None,
            source_kind="embedded",
            confidence=100,
            method="automatic",
        )
        with self.assertRaisesRegex(RepositoryDataError, "内嵌"):
            self.repository.commit_lyrics_match(
                audio_asset_id=audio.id,
                lyric_asset_id=lyric.id,
                source_kind="external",
                confidence=100,
                method="manual",
            )
        self.assertEqual(self.repository.list_lyrics_matches(current_only=True), (embedded,))

    def test_low_confidence_automatic_and_wrong_asset_kinds_are_rejected(self) -> None:
        audio = self._asset("audio.mp3", kind="audio")
        lyric = self._asset("lyric.lrc", kind="lyric")
        with self.assertRaisesRegex(RepositoryDataError, "低置信度"):
            self.repository.commit_lyrics_match(
                audio_asset_id=audio.id,
                lyric_asset_id=lyric.id,
                source_kind="external",
                confidence=94,
                method="automatic",
            )
        with self.assertRaises(RepositoryDataError):
            self.repository.commit_lyrics_match(
                audio_asset_id=lyric.id,
                lyric_asset_id=audio.id,
                source_kind="external",
                confidence=100,
                method="manual",
            )
        self.assertEqual(self.repository.list_lyrics_matches(), ())

    def test_cancel_current_match_keeps_cancelled_history(self) -> None:
        audio = self._asset("cancel.mp3", kind="audio")
        lyric = self._asset("cancel.lrc", kind="lyric")
        match = self.repository.commit_lyrics_match(
            audio_asset_id=audio.id,
            lyric_asset_id=lyric.id,
            source_kind="external",
            confidence=100,
            method="manual",
        )
        cancelled = self.repository.cancel_current_lyrics_match(audio.id)
        self.assertEqual(cancelled.id, match.id)
        self.assertEqual((cancelled.state, cancelled.is_current), ("cancelled", False))
        self.assertEqual(self.repository.list_lyrics_matches(current_only=True), ())
        with self.assertRaises(RecordNotFoundError):
            self.repository.cancel_current_lyrics_match(audio.id)

    def test_replacement_insert_failure_rolls_back_current_history_change(self) -> None:
        audio = self._asset("atomic.mp3", kind="audio")
        first = self._asset("atomic-first.lrc", kind="lyric")
        second = self._asset("atomic-second.lrc", kind="lyric")
        current = self.repository.commit_lyrics_match(
            audio_asset_id=audio.id,
            lyric_asset_id=first.id,
            source_kind="external",
            confidence=100,
            method="automatic",
        )
        self.repository._connection.execute(  # test-only deterministic SQL failure
            """
            CREATE TRIGGER fail_lyrics_insert
            BEFORE INSERT ON lyrics_matches
            BEGIN
                SELECT RAISE(ABORT, 'injected lyrics insert failure');
            END
            """
        )

        with self.assertRaisesRegex(Exception, "injected lyrics insert failure"):
            self.repository.commit_lyrics_match(
                audio_asset_id=audio.id,
                lyric_asset_id=second.id,
                source_kind="external",
                confidence=80,
                method="manual",
            )

        self.assertEqual(self.repository.list_lyrics_matches(current_only=True), (current,))
        self.assertEqual(self.repository.list_lyrics_matches(), (current,))

    def test_missing_or_external_changed_assets_are_not_matchable(self) -> None:
        audio = self._asset("state.mp3", kind="audio")
        lyric = self._asset("state.lrc", kind="lyric")
        self.repository.upsert_asset(
            AssetUpsert(
                Path(audio.canonical_path),
                audio.size_bytes,
                audio.mtime_ns,
                kind="audio",
                file_state="external_changed",
            )
        )
        with self.assertRaisesRegex(RepositoryDataError, "active audio"):
            self.repository.commit_lyrics_match(
                audio_asset_id=audio.id,
                lyric_asset_id=lyric.id,
                source_kind="external",
                confidence=100,
                method="manual",
            )

    def test_new_api_respects_thread_and_closed_repository_boundaries(self) -> None:
        errors: list[Exception] = []

        def cross_thread() -> None:
            try:
                self.repository.list_lyrics_matches()
            except Exception as error:
                errors.append(error)

        thread = threading.Thread(target=cross_thread)
        thread.start()
        thread.join()
        self.assertIsInstance(errors[0], RepositoryThreadError)
        self.repository.close()
        with self.assertRaises(RepositoryClosedError):
            self.repository.list_lyrics_matches()


if __name__ == "__main__":
    unittest.main()
