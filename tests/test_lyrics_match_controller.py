from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import unittest
import wave

from PySide6.QtCore import QEventLoop, QTimer

from database import DatabaseConfig
from main import build_app
from mutagen.id3 import USLT
from mutagen.wave import WAVE
from repositories import IndexBatchItem, LibraryRepository
from services.lyrics_match_controller import LyricsMatchController, LyricsMatchWorker


class LyricsMatchControllerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = build_app()

    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.audio_root = self.root / "audio"
        self.lyrics_root = self.root / "lyrics"
        self.audio_root.mkdir()
        self.lyrics_root.mkdir()
        self.config = DatabaseConfig(self.root / "library.sqlite3")

    def seed_audio(self, name: str = "晴天-周杰伦.wav", *, embedded: bool = False) -> str:
        path = self.audio_root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(path), "wb") as stream:
            stream.setnchannels(1)
            stream.setsampwidth(2)
            stream.setframerate(8000)
            stream.writeframes(b"\0\0" * 80)
        if embedded:
            media = WAVE(str(path))
            media.add_tags()
            media.tags.add(USLT(encoding=3, lang="zho", desc="", text="内嵌歌词"))
            media.save()
        metadata = path.stat()
        with LibraryRepository(self.config) as repository:
            session = repository.create_scan_session(mode="audio", source_folder=self.audio_root)
            record = repository.index_scan_batch(
                session.id,
                (IndexBatchItem(path, metadata.st_size, metadata.st_mtime_ns),),
            )[0]
            repository.finish_scan_session(session.id, status="completed")
            return record.asset.id

    def write_lrc(self, name: str, *, title: str, artist: str) -> Path:
        path = self.lyrics_root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"[ti:{title}]\n[ar:{artist}]\n[00:01.00]test", encoding="utf-8")
        return path

    def run_worker(self, worker: LyricsMatchWorker) -> dict[str, list]:
        observed = {"completed": [], "cancelled": [], "failed": [], "factory_threads": []}
        worker.completed.connect(observed["completed"].append)
        worker.cancelled.connect(observed["cancelled"].append)
        worker.failed.connect(observed["failed"].append)
        loop = QEventLoop()
        worker.finished.connect(loop.quit)
        timeout = QTimer()
        timeout.setSingleShot(True)
        timeout.timeout.connect(loop.quit)
        worker.start()
        timeout.start(5000)
        loop.exec()
        timeout.stop()
        self.assertFalse(worker.isRunning())
        return observed

    def test_real_worker_indexes_lrc_and_auto_commits_exact_match_off_main_thread(self) -> None:
        audio_id = self.seed_audio()
        self.write_lrc("晴天-周杰伦.lrc", title="晴天", artist="周杰伦")
        factory_threads: list[int] = []

        def factory() -> LibraryRepository:
            factory_threads.append(threading.get_ident())
            return LibraryRepository(self.config)

        worker = LyricsMatchWorker(root=self.lyrics_root, repository_factory=factory)
        observed = self.run_worker(worker)

        self.assertEqual(observed["failed"], [])
        self.assertEqual(observed["cancelled"], [])
        self.assertEqual(len(observed["completed"]), 1)
        result = observed["completed"][0]
        self.assertEqual((result.indexed_count, result.automatic_count), (1, 1))
        self.assertNotEqual(factory_threads, [threading.get_ident()])
        with LibraryRepository(self.config) as repository:
            lyrics = repository.list_assets(kind="lyric")
            matches = repository.list_lyrics_matches(current_only=True)
        self.assertEqual(len(lyrics), 1)
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0].audio_asset_id, audio_id)
        self.assertEqual(matches[0].lyric_asset_id, lyrics[0].id)

    def test_low_confidence_is_not_auto_committed_and_manual_token_can_commit(self) -> None:
        audio_id = self.seed_audio("晴天-周杰伦.wav")
        self.write_lrc("晴天-其他歌手.lrc", title="晴天", artist="其他歌手")
        worker = LyricsMatchWorker(
            root=self.lyrics_root,
            repository_factory=lambda: LibraryRepository(self.config),
        )
        result = self.run_worker(worker)["completed"][0]
        candidate = next(item for item in result.items if item.lyric_asset_id is not None)
        self.assertLess(candidate.confidence, 95)
        with LibraryRepository(self.config) as repository:
            self.assertEqual(repository.list_lyrics_matches(), ())

        controller = LyricsMatchController(self.config)
        controller._review_by_token = {candidate.token: candidate}
        committed = controller.commit_candidate(candidate.token)
        self.assertEqual((committed.audio_asset_id, committed.method), (audio_id, "manual"))

        controller._review_by_token = {
            "auto-token": replace(candidate, token="auto-token", requires_confirmation=False)
        }
        with self.assertRaisesRegex(ValueError, "token 无效"):
            controller.commit_candidate("auto-token")

    def test_pre_cancel_creates_no_repository_or_database(self) -> None:
        calls: list[bool] = []
        worker = LyricsMatchWorker(
            root=self.lyrics_root,
            repository_factory=lambda: (calls.append(True), LibraryRepository(self.config))[1],
        )
        worker.request_cancel()
        observed = self.run_worker(worker)
        self.assertEqual(calls, [])
        self.assertEqual(observed["cancelled"], [0])
        self.assertFalse(self.config.path.exists())

    def test_embedded_lyrics_auto_commit_without_external_lrc(self) -> None:
        audio_id = self.seed_audio("内嵌歌曲-歌手.wav", embedded=True)
        worker = LyricsMatchWorker(
            root=self.lyrics_root,
            repository_factory=lambda: LibraryRepository(self.config),
        )

        result = self.run_worker(worker)["completed"][0]

        self.assertEqual((result.indexed_count, result.automatic_count), (0, 1))
        with LibraryRepository(self.config) as repository:
            current = repository.list_lyrics_matches(current_only=True)
        self.assertEqual(len(current), 1)
        self.assertEqual(
            (current[0].audio_asset_id, current[0].source_kind, current[0].lyric_asset_id),
            (audio_id, "embedded", None),
        )

    def test_shared_high_confidence_lrc_conflict_is_never_auto_committed(self) -> None:
        self.seed_audio("a/晴天-周杰伦.wav")
        self.seed_audio("b/晴天-周杰伦.wav")
        self.write_lrc("晴天-周杰伦.lrc", title="晴天", artist="周杰伦")
        worker = LyricsMatchWorker(
            root=self.lyrics_root,
            repository_factory=lambda: LibraryRepository(self.config),
        )

        result = self.run_worker(worker)["completed"][0]

        self.assertEqual(result.automatic_count, 0)
        self.assertEqual(sum(item.status == "冲突" for item in result.items), 2)
        with LibraryRepository(self.config) as repository:
            self.assertEqual(repository.list_lyrics_matches(), ())

    def test_same_audio_equal_top_candidates_require_manual_choice(self) -> None:
        self.seed_audio()
        self.write_lrc("a/晴天-周杰伦.lrc", title="晴天", artist="周杰伦")
        self.write_lrc("b/晴天-周杰伦.lrc", title="晴天", artist="周杰伦")
        worker = LyricsMatchWorker(
            root=self.lyrics_root,
            repository_factory=lambda: LibraryRepository(self.config),
        )

        result = self.run_worker(worker)["completed"][0]
        candidates = [item for item in result.items if item.lyric_asset_id is not None]

        self.assertEqual(result.automatic_count, 0)
        self.assertEqual(len(candidates), 2)
        self.assertTrue(all(item.status == "冲突" for item in candidates))
        self.assertTrue(all(item.requires_confirmation for item in candidates))


if __name__ == "__main__":
    unittest.main()
