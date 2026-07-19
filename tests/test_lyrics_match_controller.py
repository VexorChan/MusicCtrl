from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import unittest
from unittest.mock import patch
import wave

from PySide6.QtCore import QEventLoop, QTimer

from database import DatabaseConfig
from main import build_app
from mutagen.id3 import USLT
from mutagen.wave import WAVE
from repositories import AssetUpsert, IndexBatchItem, LibraryRepository
from services.backup_manager import BackupController
from services.lyrics_match_controller import (
    IGNORED_AUDIO_ASSET_IDS_KEY,
    LAST_SUCCESSFUL_LYRICS_ROOT_KEY,
    LyricsBatchCommitWorker,
    LyricsIgnoreWorker,
    LyricsMatchController,
    LyricsMatchWorker,
)
from ui.main_window import MainWindow


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
        observed = {
            "completed": [], "partial": [], "cancelled": [], "failed": [],
            "factory_threads": [],
        }
        worker.completed.connect(observed["completed"].append)
        if hasattr(worker, "partial"):
            worker.partial.connect(observed["partial"].append)
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

    def two_manual_candidates(self):
        self.seed_audio("歌曲一-歌手甲.wav")
        self.seed_audio("歌曲二-歌手乙.wav")
        self.write_lrc("歌曲一-其他甲.lrc", title="歌曲一", artist="其他甲")
        self.write_lrc("歌曲二-其他乙.lrc", title="歌曲二", artist="其他乙")
        result = self.run_worker(
            LyricsMatchWorker(
                root=self.lyrics_root,
                repository_factory=lambda: LibraryRepository(self.config),
            )
        )["completed"][0]
        selected = []
        for title in ("歌曲一", "歌曲二"):
            selected.append(next(
                item for item in result.items
                if item.lyric_path is not None
                and item.lyric_path.stem.startswith(title)
                and item.requires_confirmation
            ))
        controller = LyricsMatchController(self.config)
        controller._review_by_token = {item.token: item for item in selected}
        return controller._freeze_batch(tuple(item.token for item in selected))

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

    def test_library_uses_each_lyrics_asset_completed_root_and_old_root_can_backup(self) -> None:
        root_a = self.root / "lyrics-a"
        root_b = self.root / "lyrics-b"
        orphan_root = self.root / "lyrics-orphan"
        for root in (root_a, root_b, orphan_root):
            root.mkdir()
        path_a = root_a / "歌曲甲-歌手甲.lrc"
        path_b = root_b / "歌曲乙-歌手乙.lrc"
        orphan_path = orphan_root / "无来源-歌手丙.lrc"
        for path in (path_a, path_b, orphan_path):
            path.write_text("[00:01.00]test", encoding="utf-8")

        with LibraryRepository(self.config) as repository:
            for scan_root, path in ((root_a, path_a), (root_b, path_b)):
                metadata = path.stat()
                session = repository.create_scan_session(
                    mode="lyric",
                    source_folder=scan_root,
                )
                repository.index_scan_batch(
                    session.id,
                    (
                        IndexBatchItem(
                            path,
                            metadata.st_size,
                            metadata.st_mtime_ns,
                            kind="lyric",
                        ),
                    ),
                )
                repository.finish_scan_session(session.id, status="completed")
            orphan_metadata = orphan_path.stat()
            repository.upsert_asset(
                AssetUpsert(
                    orphan_path,
                    orphan_metadata.st_size,
                    orphan_metadata.st_mtime_ns,
                    kind="lyric",
                )
            )
            repository.set_setting(
                LAST_SUCCESSFUL_LYRICS_ROOT_KEY,
                str(root_b),
            )

        controller = LyricsMatchController(self.config)
        records = {
            record["_canonical_path"].name: record
            for record in controller.load_lyrics_library()
        }
        self.assertEqual(records[path_a.name]["_allowed_root"], root_a)
        self.assertEqual(records[path_b.name]["_allowed_root"], root_b)
        self.assertIsNone(records[orphan_path.name]["_allowed_root"])

        backup_input = MainWindow._backup_input(records[path_a.name], "lyrics")
        self.assertEqual(backup_input.allowed_root, root_a)
        backup = BackupController(
            backup_root=self.root / "backups",
            repository_factory=lambda: LibraryRepository(self.config),
        )
        observed: dict[str, list[object]] = {"completed": [], "failed": []}
        backup.completed.connect(observed["completed"].append)
        backup.failed.connect(observed["failed"].append)
        loop = QEventLoop()
        backup.running_changed.connect(
            lambda running: loop.quit() if not running else None
        )
        timeout = QTimer()
        timeout.setSingleShot(True)
        timeout.timeout.connect(loop.quit)
        backup.start_backup((backup_input,))
        timeout.start(5000)
        loop.exec()
        timeout.stop()

        self.assertFalse(backup.running)
        self.assertEqual(observed["failed"], [])
        self.assertEqual(len(observed["completed"]), 1)
        self.assertEqual(observed["completed"][0].success_count, 1)
        self.assertFalse(path_a.exists())
        self.assertEqual(len(backup.list_entries()), 1)

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
        controller.commit_candidate(candidate.token)
        loop = QEventLoop()
        controller.running_changed.connect(lambda running: loop.quit() if not running else None)
        QTimer.singleShot(5000, loop.quit)
        loop.exec()
        with LibraryRepository(self.config) as repository:
            committed = repository.list_lyrics_matches(current_only=True)
        self.assertEqual(
            (committed[0].audio_asset_id, committed[0].method),
            (audio_id, "manual"),
        )
        with self.assertRaisesRegex(ValueError, "候选已失效"):
            controller.commit_candidate(candidate.token)

        controller._review_by_token = {
            "auto-token": replace(candidate, token="auto-token", requires_confirmation=False)
        }
        with self.assertRaisesRegex(ValueError, "候选已失效"):
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

    def test_persistent_ignore_skips_worker_then_unignore_allows_recovery(self) -> None:
        audio_id = self.seed_audio()
        self.write_lrc("晴天-周杰伦.lrc", title="晴天", artist="周杰伦")
        with LibraryRepository(self.config) as repository:
            repository.set_setting(IGNORED_AUDIO_ASSET_IDS_KEY, [audio_id])

        ignored_result = self.run_worker(
            LyricsMatchWorker(
                root=self.lyrics_root,
                repository_factory=lambda: LibraryRepository(self.config),
            )
        )["completed"][0]
        self.assertEqual(ignored_result.automatic_count, 0)
        ignored_item = next(item for item in ignored_result.items if item.audio_asset_id == audio_id)
        self.assertTrue(ignored_item.ignored)
        self.assertEqual(ignored_item.status, "已忽略")
        with LibraryRepository(self.config) as repository:
            self.assertEqual(repository.list_lyrics_matches(), ())

        controller = LyricsMatchController(self.config)
        controller._review_by_token = {ignored_item.token: ignored_item}
        controller._original_review_by_token = {
            ignored_item.token: replace(
                ignored_item,
                ignored=False,
                status="未匹配",
                requires_confirmation=True,
            )
        }
        controller._last_result = ignored_result
        controller.unignore_audio_assets((audio_id,))
        loop = QEventLoop()
        controller.running_changed.connect(lambda running: loop.quit() if not running else None)
        QTimer.singleShot(5000, loop.quit)
        loop.exec()
        with LibraryRepository(self.config) as repository:
            self.assertEqual(repository.get_setting(IGNORED_AUDIO_ASSET_IDS_KEY).value, [])

        recovered = self.run_worker(
            LyricsMatchWorker(
                root=self.lyrics_root,
                repository_factory=lambda: LibraryRepository(self.config),
            )
        )["completed"][0]
        self.assertEqual(recovered.automatic_count, 1)

    def test_batch_duplicate_external_lyric_is_rejected_before_worker_and_writes(self) -> None:
        self.seed_audio("a/晴天-周杰伦.wav")
        self.seed_audio("b/晴天-周杰伦.wav")
        self.write_lrc("晴天-周杰伦.lrc", title="晴天", artist="周杰伦")
        result = self.run_worker(
            LyricsMatchWorker(
                root=self.lyrics_root,
                repository_factory=lambda: LibraryRepository(self.config),
            )
        )["completed"][0]
        candidates = tuple(
            item for item in result.items
            if item.lyric_asset_id is not None and item.requires_confirmation
        )
        self.assertEqual(len(candidates), 2)
        controller = LyricsMatchController(self.config)
        controller._review_by_token = {item.token: item for item in candidates}
        with self.assertRaisesRegex(ValueError, "重复使用同一歌词"):
            controller.commit_candidates(tuple(item.token for item in candidates))
        self.assertFalse(controller.running)
        with LibraryRepository(self.config) as repository:
            self.assertEqual(repository.list_lyrics_matches(), ())

    def test_ignore_worker_owns_repository_and_failed_setting_keeps_old_value(self) -> None:
        audio_id = self.seed_audio()
        events: list[tuple[str, int]] = []

        class TrackedRepository(LibraryRepository):
            def close(inner_self) -> None:
                events.append(("close", threading.get_ident()))
                super().close()

        worker = LyricsIgnoreWorker(
            audio_asset_ids=(audio_id,),
            ignored=True,
            repository_factory=lambda: (
                events.append(("factory", threading.get_ident())),
                TrackedRepository(self.config),
            )[1],
        )
        worker.finished.connect(lambda: events.append(("finished", threading.get_ident())))
        observed = self.run_worker(worker)
        self.assertEqual(len(observed["completed"]), 1)
        self.assertNotEqual(events[0][1], threading.get_ident())
        self.assertLess(
            next(index for index, item in enumerate(events) if item[0] == "close"),
            next(index for index, item in enumerate(events) if item[0] == "finished"),
        )
        with LibraryRepository(self.config) as repository:
            self.assertEqual(repository.get_setting(IGNORED_AUDIO_ASSET_IDS_KEY).value, [audio_id])

        original = LibraryRepository.set_setting
        with patch.object(
            LibraryRepository,
            "set_setting",
            autospec=True,
            side_effect=RuntimeError("setting write failed"),
        ):
            failed = self.run_worker(
                LyricsIgnoreWorker(
                    audio_asset_ids=(audio_id,),
                    ignored=False,
                    repository_factory=lambda: LibraryRepository(self.config),
                )
            )
        self.assertEqual(failed["completed"], [])
        self.assertIn("setting write failed", failed["failed"][0])
        with LibraryRepository(self.config) as repository:
            self.assertEqual(repository.get_setting(IGNORED_AUDIO_ASSET_IDS_KEY).value, [audio_id])
        self.assertTrue(callable(original))

    def test_ignore_worker_rejects_unknown_non_audio_and_corrupt_setting(self) -> None:
        audio_id = self.seed_audio()
        unknown = self.run_worker(
            LyricsIgnoreWorker(
                audio_asset_ids=("unknown",),
                ignored=True,
                repository_factory=lambda: LibraryRepository(self.config),
            )
        )
        self.assertEqual(len(unknown["failed"]), 1)

        lyric = self.write_lrc("只是一首歌词.lrc", title="歌词", artist="歌手")
        metadata = lyric.stat()
        with LibraryRepository(self.config) as repository:
            session = repository.create_scan_session(mode="lyric", source_folder=self.lyrics_root)
            lyric_id = repository.index_scan_batch(
                session.id,
                (IndexBatchItem(lyric, metadata.st_size, metadata.st_mtime_ns, kind="lyric"),),
            )[0].asset.id
            repository.finish_scan_session(session.id, status="completed")
        non_audio = self.run_worker(
            LyricsIgnoreWorker(
                audio_asset_ids=(lyric_id,),
                ignored=True,
                repository_factory=lambda: LibraryRepository(self.config),
            )
        )
        self.assertEqual(len(non_audio["failed"]), 1)

        with LibraryRepository(self.config) as repository:
            repository.set_setting(IGNORED_AUDIO_ASSET_IDS_KEY, [audio_id])
            repository._connection.execute(
                "UPDATE settings SET value_json = ? WHERE key = ?",
                ("{broken", IGNORED_AUDIO_ASSET_IDS_KEY),
            )
        corrupt = self.run_worker(
            LyricsIgnoreWorker(
                audio_asset_ids=(audio_id,),
                ignored=False,
                repository_factory=lambda: LibraryRepository(self.config),
            )
        )
        self.assertEqual(len(corrupt["failed"]), 1)

    def test_both_workers_fail_closed_for_persisted_unknown_and_lyric_ids(self) -> None:
        audio_id = self.seed_audio()
        lyric = self.write_lrc("晴天-周杰伦.lrc", title="晴天", artist="周杰伦")

        with LibraryRepository(self.config) as repository:
            repository.set_setting(IGNORED_AUDIO_ASSET_IDS_KEY, ["missing-audio"])
        for worker in (
            LyricsMatchWorker(
                root=self.lyrics_root,
                repository_factory=lambda: LibraryRepository(self.config),
            ),
            LyricsIgnoreWorker(
                audio_asset_ids=(audio_id,),
                ignored=True,
                repository_factory=lambda: LibraryRepository(self.config),
            ),
        ):
            observed = self.run_worker(worker)
            self.assertEqual(len(observed["failed"]), 1)
        with LibraryRepository(self.config) as repository:
            self.assertEqual(
                repository.get_setting(IGNORED_AUDIO_ASSET_IDS_KEY).value,
                ["missing-audio"],
            )
            self.assertEqual(repository.list_lyrics_matches(), ())

        metadata = lyric.stat()
        with LibraryRepository(self.config) as repository:
            session = repository.create_scan_session(mode="lyric", source_folder=self.lyrics_root)
            lyric_id = repository.index_scan_batch(
                session.id,
                (IndexBatchItem(lyric, metadata.st_size, metadata.st_mtime_ns, kind="lyric"),),
            )[0].asset.id
            repository.finish_scan_session(session.id, status="completed")
            repository.set_setting(IGNORED_AUDIO_ASSET_IDS_KEY, [lyric_id])
        for worker in (
            LyricsMatchWorker(
                root=self.lyrics_root,
                repository_factory=lambda: LibraryRepository(self.config),
            ),
            LyricsIgnoreWorker(
                audio_asset_ids=(audio_id,),
                ignored=False,
                repository_factory=lambda: LibraryRepository(self.config),
            ),
        ):
            observed = self.run_worker(worker)
            self.assertEqual(len(observed["failed"]), 1)
        with LibraryRepository(self.config) as repository:
            self.assertEqual(repository.get_setting(IGNORED_AUDIO_ASSET_IDS_KEY).value, [lyric_id])
            self.assertEqual(repository.list_lyrics_matches(), ())

    def test_ignore_controller_three_rounds_leave_no_worker_or_thread(self) -> None:
        audio_id = self.seed_audio()
        controller = LyricsMatchController(self.config)
        for ignored in (True, False, True):
            if ignored:
                controller.ignore_audio_assets((audio_id,))
            else:
                controller.unignore_audio_assets((audio_id,))
            loop = QEventLoop()
            controller.running_changed.connect(
                lambda running, current_loop=loop: current_loop.quit() if not running else None
            )
            QTimer.singleShot(5000, loop.quit)
            loop.exec()
            self.assertFalse(controller.running)
            self.assertIsNone(controller._worker)
        with LibraryRepository(self.config) as repository:
            self.assertEqual(repository.get_setting(IGNORED_AUDIO_ASSET_IDS_KEY).value, [audio_id])

    def test_batch_item_results_cover_success_partial_and_mid_cancel(self) -> None:
        inputs = self.two_manual_candidates()
        factory_threads: list[int] = []
        success_worker = LyricsBatchCommitWorker(
            inputs=inputs,
            repository_factory=lambda: (
                factory_threads.append(threading.get_ident()),
                LibraryRepository(self.config),
            )[1],
        )
        success = self.run_worker(success_worker)["completed"][0]
        self.assertEqual(
            (success.status, success.success_count, tuple(item.result for item in success.items)),
            ("completed", 2, ("success", "success")),
        )
        self.assertNotEqual(factory_threads, [threading.get_ident()])

        # 新数据库重新建立同样两项，第二个 repository 写入确定性失败，第一项仍精确报告成功。
        self.temporary.cleanup()
        self.setUp()
        inputs = self.two_manual_candidates()
        original_commit = LibraryRepository.commit_lyrics_match
        calls = 0

        def fail_second(repository, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("second item failed")
            return original_commit(repository, **kwargs)

        with patch.object(LibraryRepository, "commit_lyrics_match", new=fail_second):
            partial_observed = self.run_worker(
                LyricsBatchCommitWorker(
                    inputs=inputs,
                    repository_factory=lambda: LibraryRepository(self.config),
                )
            )
            partial = partial_observed["partial"][0]
        self.assertEqual(
            (partial.status, partial.success_count, partial.failure_count),
            ("partial", 1, 1),
        )
        self.assertEqual(tuple(item.result for item in partial.items), ("success", "failed"))

        self.temporary.cleanup()
        self.setUp()
        inputs = self.two_manual_candidates()
        original_commit = LibraryRepository.commit_lyrics_match
        cancel_worker: LyricsBatchCommitWorker
        calls = 0

        def cancel_after_first(repository, **kwargs):
            nonlocal calls
            record = original_commit(repository, **kwargs)
            calls += 1
            if calls == 1:
                cancel_worker.request_cancel()
            return record

        cancel_worker = LyricsBatchCommitWorker(
            inputs=inputs,
            repository_factory=lambda: LibraryRepository(self.config),
        )
        with patch.object(LibraryRepository, "commit_lyrics_match", new=cancel_after_first):
            cancelled = self.run_worker(cancel_worker)["cancelled"][0]
        self.assertEqual(
            (cancelled.status, cancelled.success_count, cancelled.cancelled_count),
            ("cancelled", 1, 1),
        )
        self.assertEqual(tuple(item.result for item in cancelled.items), ("success", "cancelled"))


if __name__ == "__main__":
    unittest.main()
