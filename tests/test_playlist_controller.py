from __future__ import annotations

import os
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import threading
import unittest
from unittest.mock import patch

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from database import DatabaseConfig
from repositories import IndexBatchItem, LibraryRepository
import services.playlist_controller as playlist_module
from services.playlist_controller import (
    PENDING_RETARGET_KEY,
    PLAYLIST_HISTORY_KEY,
    PLAYLIST_ROOT_KEY,
    PlaylistAudioInput,
    PlaylistController,
    PlaylistRemovalInput,
    PlaylistRetargetInput,
    PlaylistSnapshot,
)
from services.windows_shortcuts import create_playlist_directory, create_shortcut, read_shortcut
from ui.main_window import MainWindow


@unittest.skipUnless(os.name == "nt", "Windows .lnk integration requires Windows")
class PlaylistControllerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.audio_root = self.root / "audio"
        self.playlist_root = self.root / "playlists"
        self.audio_root.mkdir()
        self.playlist_root.mkdir()
        self.audio = self.audio_root / "晴天-周杰伦.mp3"
        self.audio.write_bytes(b"temporary playlist fixture")
        self.config = DatabaseConfig(self.root / "library.sqlite3")
        with LibraryRepository(self.config) as repository:
            session = repository.create_scan_session(mode="audio", source_folder=self.audio_root)
            batch = repository.index_scan_batch(
                session.id,
                (IndexBatchItem(self.audio, self.audio.stat().st_size, self.audio.stat().st_mtime_ns),),
            )[0]
            repository.complete_scan_and_reconcile(session.id)
        self.asset = batch.asset
        self.controller = PlaylistController(self.config)
        self.controller.set_root(self.playlist_root)
        self.controller.create_playlist("通勤")

    def _wait(self, controller: PlaylistController | None = None) -> None:
        active = self.controller if controller is None else controller
        deadline = time.monotonic() + 5
        while active.running and time.monotonic() < deadline:
            self.app.processEvents()
        self.app.processEvents()
        self.assertFalse(active.running, "playlist worker did not finish")

    def _setting_value(self, key: str) -> object | None:
        with LibraryRepository(self.config) as repository:
            setting = repository.get_setting(key)
        return None if setting is None else setting.value

    def _install_retarget_fixture(
        self, suffix: str = "新版"
    ) -> tuple[Path, Path, Path, Path, PlaylistRetargetInput]:
        old_audio = self.audio
        old_shortcut = self.playlist_root / "通勤" / f"{old_audio.name}.lnk"
        create_shortcut(
            target_path=old_audio,
            audio_root=self.audio_root,
            shortcut_path=old_shortcut,
            playlist_root=self.playlist_root,
        )
        new_audio = self.audio_root / f"晴天{suffix}-周杰伦.mp3"
        os.rename(old_audio, new_audio)
        new_shortcut = self.playlist_root / "通勤" / f"{new_audio.name}.lnk"
        return (
            old_audio,
            new_audio,
            old_shortcut,
            new_shortcut,
            PlaylistRetargetInput(old_audio, new_audio, self.audio_root),
        )

    def test_add_load_and_remove_shortcut_preserve_audio(self) -> None:
        snapshot = (self.audio.read_bytes(), self.audio.stat().st_mtime_ns)
        self.controller.start_add(
            "通勤",
            (PlaylistAudioInput(self.asset.id, self.audio, self.audio_root, "active"),),
        )
        self._wait()

        rows = self.controller.load_playlist("通勤")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["title"], "晴天")
        shortcut = rows[0]["_shortcut_path"]
        self.assertTrue(shortcut.is_file())

        self.controller.start_remove(
            "通勤",
            (PlaylistRemovalInput(shortcut, self.audio),),
        )
        self._wait()
        self.assertEqual(self.controller.load_playlist("通勤"), ())
        self.assertEqual((self.audio.read_bytes(), self.audio.stat().st_mtime_ns), snapshot)

    def test_duplicate_is_skipped_without_overwrite(self) -> None:
        results = []
        self.controller.completed.connect(results.append)
        item = PlaylistAudioInput(self.asset.id, self.audio, self.audio_root, "active")
        self.controller.start_add("通勤", (item,))
        self._wait()
        shortcut = self.controller.load_playlist("通勤")[0]["_shortcut_path"]
        before = shortcut.read_bytes()
        self.controller.start_add("通勤", (item,))
        self._wait()
        self.assertEqual(results[-1].skipped_count, 1)
        self.assertEqual(shortcut.read_bytes(), before)
        history = self.controller.list_history()
        self.assertEqual(history[0].status, "completed")
        self.assertEqual(history[0].items[0].result, "skipped")

    def test_retarget_updates_managed_shortcut_after_audio_rename(self) -> None:
        item = PlaylistAudioInput(self.asset.id, self.audio, self.audio_root, "active")
        self.controller.start_add("通勤", (item,))
        self._wait()
        old_shortcut = self.controller.load_playlist("通勤")[0]["_shortcut_path"]
        renamed = self.audio_root / "晴天现场版-周杰伦.mp3"
        os.rename(self.audio, renamed)

        self.controller.start_retarget(
            (PlaylistRetargetInput(self.audio, renamed, self.audio_root),)
        )
        self._wait()

        rows = self.controller.load_playlist("通勤")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["_target_path"], renamed)
        self.assertFalse(old_shortcut.exists())
        self.assertTrue(rows[0]["_shortcut_path"].is_file())

    def test_retarget_impact_counts_managed_shortcuts_read_only(self) -> None:
        self.controller.create_playlist("怀旧")
        for name in ("通勤", "怀旧"):
            create_shortcut(
                target_path=self.audio,
                audio_root=self.audio_root,
                shortcut_path=self.playlist_root / name / f"{self.audio.name}.lnk",
                playlist_root=self.playlist_root,
            )
        shortcut_bytes = {
            path: path.read_bytes() for path in self.playlist_root.rglob("*.lnk")
        }
        history_before = self.controller.list_history()
        ready: list[int] = []
        self.controller.retarget_impact_ready.connect(ready.append)

        self.controller.start_retarget_impact(
            (
                PlaylistRetargetInput(
                    self.audio,
                    self.audio_root / "晴天现场版-周杰伦.mp3",
                    self.audio_root,
                ),
            )
        )
        self._wait()

        self.assertEqual(ready, [2])
        self.assertEqual(
            {path: path.read_bytes() for path in self.playlist_root.rglob("*.lnk")},
            shortcut_bytes,
        )
        self.assertEqual(self.controller.list_history(), history_before)

    def test_retarget_impact_read_error_fails_instead_of_underreporting(self) -> None:
        create_shortcut(
            target_path=self.audio,
            audio_root=self.audio_root,
            shortcut_path=self.playlist_root / "通勤" / f"{self.audio.name}.lnk",
            playlist_root=self.playlist_root,
        )
        ready: list[int] = []
        failures: list[str] = []
        self.controller.retarget_impact_ready.connect(ready.append)
        self.controller.retarget_impact_failed.connect(failures.append)

        with patch(
            "services.playlist_controller.read_shortcut",
            side_effect=PermissionError("拒绝访问快捷方式"),
        ):
            self.controller.start_retarget_impact(
                (
                    PlaylistRetargetInput(
                        self.audio,
                        self.audio_root / "晴天现场版-周杰伦.mp3",
                        self.audio_root,
                    ),
                )
            )
            self._wait()

        self.assertEqual(ready, [])
        self.assertEqual(failures, ["拒绝访问快捷方式"])

    def test_retarget_converges_existing_new_link_and_rejects_wrong_target(self) -> None:
        item = PlaylistAudioInput(self.asset.id, self.audio, self.audio_root, "active")
        self.controller.start_add("通勤", (item,))
        self._wait()
        old_shortcut = self.controller.load_playlist("通勤")[0]["_shortcut_path"]
        renamed = self.audio_root / "晴天新版-周杰伦.mp3"
        os.rename(self.audio, renamed)
        destination = self.playlist_root / "通勤" / f"{renamed.name}.lnk"
        create_shortcut(
            target_path=renamed,
            audio_root=self.audio_root,
            shortcut_path=destination,
            playlist_root=self.playlist_root,
        )

        results = []
        self.controller.completed.connect(results.append)
        self.controller.start_retarget(
            (PlaylistRetargetInput(self.audio, renamed, self.audio_root),)
        )
        self._wait()
        self.assertEqual(results[-1].success_count, 1)
        self.assertFalse(old_shortcut.exists())
        self.assertTrue(destination.exists())

        results.clear()
        self.controller.start_retarget(
            (PlaylistRetargetInput(self.audio, renamed, self.audio_root),)
        )
        self._wait()
        self.assertEqual(results[-1].skipped_count, 1)
        self.assertIn("已收敛", results[-1].items[0].message)

        source_again = self.audio_root / "旧名-歌手.mp3"
        source_again.write_bytes(b"old")
        wrong = self.audio_root / "其他-歌手.mp3"
        wrong.write_bytes(b"wrong")
        old_again = self.playlist_root / "通勤" / f"{source_again.name}.lnk"
        target_again = self.playlist_root / "通勤" / "目标-歌手.mp3.lnk"
        create_shortcut(
            target_path=source_again,
            audio_root=self.audio_root,
            shortcut_path=old_again,
            playlist_root=self.playlist_root,
        )
        create_shortcut(
            target_path=wrong,
            audio_root=self.audio_root,
            shortcut_path=target_again,
            playlist_root=self.playlist_root,
        )
        requested_target = self.audio_root / "目标-歌手.mp3"
        requested_target.write_bytes(b"target")
        self.controller.start_retarget(
            (PlaylistRetargetInput(source_again, requested_target, self.audio_root),)
        )
        self._wait()
        self.assertEqual(results[-1].failure_count, 1)
        self.assertTrue(old_again.exists())
        self.assertTrue(target_again.exists())

        target_again.unlink()
        target_again.write_bytes(b"not-a-shortcut")
        self.controller.start_retarget(
            (PlaylistRetargetInput(source_again, requested_target, self.audio_root),)
        )
        self._wait()
        self.assertEqual(results[-1].failure_count, 1)
        self.assertTrue(old_again.exists())
        self.assertEqual(target_again.read_bytes(), b"not-a-shortcut")

    def test_retarget_failure_persists_journal_before_shortcut_write(self) -> None:
        old_audio, new_audio, old_shortcut, new_shortcut, item = (
            self._install_retarget_fixture("失败")
        )
        pending_seen: list[object | None] = []

        def fail_after_journal(**_kwargs) -> None:
            pending_seen.append(self._setting_value(PENDING_RETARGET_KEY))
            raise RuntimeError("确定性快捷方式写入失败")

        with patch.object(
            playlist_module, "create_shortcut", side_effect=fail_after_journal
        ):
            self.controller.start_retarget((item,))
            self._wait()

        self.assertEqual(len(pending_seen), 1)
        self.assertIsInstance(pending_seen[0], dict)
        pending = self._setting_value(PENDING_RETARGET_KEY)
        self.assertEqual(pending, pending_seen[0])
        self.assertEqual(pending["playlist_root"], str(self.playlist_root))
        self.assertEqual(
            pending["items"],
            [
                {
                    "source_path": str(old_audio),
                    "target_path": str(new_audio),
                    "audio_root": str(self.audio_root),
                }
            ],
        )
        self.assertTrue(old_shortcut.exists())
        self.assertFalse(new_shortcut.exists())
        self.assertEqual(read_shortcut(old_shortcut, playlist_root=self.playlist_root).target_path, old_audio)
        latest = self.controller.list_history()[0]
        self.assertEqual(latest.action, "retarget")
        self.assertEqual(latest.failure_count, 1)

    def test_pending_retarget_recovers_after_restart_and_finalizes_once(self) -> None:
        _old_audio, new_audio, old_shortcut, new_shortcut, item = (
            self._install_retarget_fixture("恢复")
        )
        with patch.object(
            playlist_module,
            "create_shortcut",
            side_effect=RuntimeError("首次写入失败"),
        ):
            self.controller.start_retarget((item,))
            self._wait()

        pending = self._setting_value(PENDING_RETARGET_KEY)
        self.assertIsInstance(pending, dict)
        batch_id = pending["batch_id"]
        reopened = PlaylistController(self.config)
        completed: list[object] = []
        failed: list[str] = []
        reopened.completed.connect(completed.append)
        reopened.failed.connect(failed.append)
        reopened.start_pending_retarget_recovery()
        self._wait(reopened)

        self.assertEqual(failed, [])
        self.assertEqual(len(completed), 1)
        self.assertFalse(old_shortcut.exists())
        self.assertTrue(new_shortcut.exists())
        self.assertEqual(
            read_shortcut(new_shortcut, playlist_root=self.playlist_root).target_path,
            new_audio,
        )
        self.assertIsNone(self._setting_value(PENDING_RETARGET_KEY))
        raw_history = self._setting_value(PLAYLIST_HISTORY_KEY)
        self.assertIsInstance(raw_history, list)
        matching = [entry for entry in raw_history if entry.get("id") == batch_id]
        self.assertEqual(len(matching), 1)
        self.assertEqual(matching[0]["action"], "retarget")
        self.assertEqual(matching[0]["failure_count"], 0)

    def test_pending_retarget_root_mismatch_fails_closed_and_retains_journal(self) -> None:
        _old_audio, _new_audio, old_shortcut, new_shortcut, item = (
            self._install_retarget_fixture("根不匹配")
        )
        with patch.object(
            playlist_module,
            "create_shortcut",
            side_effect=RuntimeError("先保留恢复日志"),
        ):
            self.controller.start_retarget((item,))
            self._wait()
        pending_before = self._setting_value(PENDING_RETARGET_KEY)

        other_root = self.root / "other-playlists"
        other_root.mkdir()
        self.controller.set_root(other_root)
        failed: list[str] = []
        self.controller.failed.connect(failed.append)
        with (
            patch.object(playlist_module, "create_shortcut") as create_spy,
            patch.object(playlist_module, "remove_shortcut") as remove_spy,
        ):
            self.controller.start_pending_retarget_recovery()
            self._wait()

        self.assertEqual(len(failed), 1)
        self.assertIn("歌单根", failed[0])
        self.assertEqual(self._setting_value(PENDING_RETARGET_KEY), pending_before)
        self.assertTrue(old_shortcut.exists())
        self.assertFalse(new_shortcut.exists())
        create_spy.assert_not_called()
        remove_spy.assert_not_called()

    def test_pending_retarget_rejects_replaced_playlist_root_identity(self) -> None:
        _old_audio, _new_audio, old_shortcut, _new_shortcut, item = (
            self._install_retarget_fixture("目录被替换")
        )
        with patch.object(
            playlist_module,
            "create_shortcut",
            side_effect=RuntimeError("先保留恢复日志"),
        ):
            self.controller.start_retarget((item,))
            self._wait()
        pending_before = self._setting_value(PENDING_RETARGET_KEY)

        parked_root = self.root / "parked-playlists"
        os.rename(self.playlist_root, parked_root)
        self.playlist_root.mkdir()
        failed: list[str] = []
        completed: list[object] = []
        self.controller.failed.connect(failed.append)
        self.controller.completed.connect(completed.append)

        self.controller.start_pending_retarget_recovery()
        self._wait()

        self.assertEqual(completed, [])
        self.assertEqual(len(failed), 1)
        self.assertIn("已被替换", failed[0])
        self.assertEqual(self._setting_value(PENDING_RETARGET_KEY), pending_before)
        self.assertTrue((parked_root / old_shortcut.relative_to(self.playlist_root)).exists())
        with self.assertRaisesRegex(RuntimeError, "尚未完成"):
            self.controller.start_retarget((item,))
        self.assertFalse(self.controller.running)
        self.assertEqual(self._setting_value(PENDING_RETARGET_KEY), pending_before)

    def test_pending_retarget_no_journal_is_silent(self) -> None:
        history_before = self._setting_value(PLAYLIST_HISTORY_KEY)
        completed: list[object] = []
        cancelled: list[object] = []
        failed: list[str] = []
        warnings: list[str] = []
        running: list[bool] = []
        self.controller.completed.connect(completed.append)
        self.controller.cancelled.connect(cancelled.append)
        self.controller.failed.connect(failed.append)
        self.controller.warning.connect(warnings.append)
        self.controller.running_changed.connect(running.append)

        self.controller.start_pending_retarget_recovery()
        self._wait()

        self.assertEqual(running, [True, False])
        self.assertEqual(completed, [])
        self.assertEqual(cancelled, [])
        self.assertEqual(failed, [])
        self.assertEqual(warnings, [])
        self.assertEqual(self._setting_value(PLAYLIST_HISTORY_KEY), history_before)

    def test_existing_pending_retarget_blocks_overwrite(self) -> None:
        _old_audio, _new_audio, _old_shortcut, _new_shortcut, item = (
            self._install_retarget_fixture("已有日志")
        )
        with patch.object(
            playlist_module,
            "create_shortcut",
            side_effect=RuntimeError("保留首个恢复日志"),
        ):
            self.controller.start_retarget((item,))
            self._wait()
        pending_before = self._setting_value(PENDING_RETARGET_KEY)

        another_source = self.audio_root / "另一首-歌手.mp3"
        another_source.write_bytes(b"another")
        another_target = self.audio_root / "另一首新版-歌手.mp3"
        another_item = PlaylistRetargetInput(
            another_source, another_target, self.audio_root
        )
        with (
            patch.object(playlist_module, "create_shortcut") as create_spy,
            self.assertRaisesRegex(RuntimeError, "尚未完成"),
        ):
            self.controller.start_retarget((another_item,))

        self.assertFalse(self.controller.running)
        self.assertEqual(self._setting_value(PENDING_RETARGET_KEY), pending_before)
        create_spy.assert_not_called()

    def test_refresh_worker_is_read_only_off_thread_and_publishes_complete_snapshot(self) -> None:
        item = PlaylistAudioInput(self.asset.id, self.audio, self.audio_root, "active")
        self.controller.start_add("通勤", (item,))
        self._wait()
        media_before = (self.audio.read_bytes(), self.audio.stat().st_mtime_ns)
        history_before = self.controller.list_history()
        snapshots: list[PlaylistSnapshot] = []
        self.controller.snapshot_ready.connect(snapshots.append)
        repository_threads: list[int] = []
        heartbeat = [0]
        timer = QTimer()
        timer.setInterval(1)
        timer.timeout.connect(lambda: heartbeat.__setitem__(0, heartbeat[0] + 1))
        real_repository = LibraryRepository
        real_read = playlist_module.read_shortcut

        def repository_factory(config):
            repository_threads.append(threading.get_ident())
            return real_repository(config)

        def slow_read(*args, **kwargs):
            time.sleep(0.05)
            return real_read(*args, **kwargs)

        timer.start()
        with patch.object(playlist_module, "LibraryRepository", side_effect=repository_factory), patch.object(
            playlist_module,
            "read_shortcut",
            side_effect=slow_read,
        ):
            self.controller.start_refresh(self.playlist_root)
            self._wait()
        timer.stop()

        self.assertEqual(len(snapshots), 1)
        self.assertEqual(snapshots[0].root, self.playlist_root)
        self.assertEqual([item.name for item in snapshots[0].playlists], ["通勤"])
        self.assertEqual(len(snapshots[0].playlists[0].records), 1)
        self.assertTrue(repository_threads)
        self.assertTrue(all(value != threading.get_ident() for value in repository_threads))
        self.assertGreater(heartbeat[0], 0)
        self.assertEqual(self.controller.list_history(), history_before)
        self.assertEqual((self.audio.read_bytes(), self.audio.stat().st_mtime_ns), media_before)

    def test_candidate_root_changes_only_after_success_and_failure_or_cancel_preserves_old(self) -> None:
        item = PlaylistAudioInput(self.asset.id, self.audio, self.audio_root, "active")
        self.controller.start_add("通勤", (item,))
        self._wait()
        other_root = self.root / "playlists-b"
        other_root.mkdir()
        folder = create_playlist_directory(playlist_root=other_root, name="粤语")
        create_shortcut(
            target_path=self.audio,
            audio_root=self.audio_root,
            shortcut_path=folder / f"{self.audio.name}.lnk",
            playlist_root=other_root,
        )
        snapshots: list[PlaylistSnapshot] = []
        failures: list[str] = []
        cancellations: list[object] = []
        self.controller.snapshot_ready.connect(snapshots.append)
        self.controller.failed.connect(failures.append)
        self.controller.cancelled.connect(cancellations.append)

        self.controller.start_refresh(other_root, remember_on_success=True)
        self._wait()
        self.assertEqual(self.controller.remembered_root(), other_root)
        self.assertEqual(snapshots[-1].root, other_root)

        with patch.object(
            LibraryRepository,
            "set_setting",
            side_effect=RuntimeError("deterministic setting failure"),
        ):
            self.controller.start_refresh(self.playlist_root, remember_on_success=True)
            self._wait()
        self.assertIn("保存设置失败", failures[-1])
        self.assertEqual(self.controller.remembered_root(), other_root)
        self.assertEqual(snapshots[-1].root, other_root)

        self.controller.start_refresh(self.playlist_root, remember_on_success=True)
        self.controller.request_cancel()
        self._wait()
        self.assertTrue(cancellations)
        self.assertEqual(self.controller.remembered_root(), other_root)
        self.assertEqual(snapshots[-1].root, other_root)

    def test_external_playlist_changes_refresh_atomically_and_removed_navigation_falls_back(self) -> None:
        window = MainWindow(playlist_controller=self.controller)
        self.addCleanup(window.close)
        self.app.processEvents()
        self._wait()
        self.app.processEvents()
        self.assertIn("playlist:通勤", window.pages)
        window.navigate("playlist:通勤")

        external = create_playlist_directory(
            playlist_root=self.playlist_root,
            name="外部新增",
        )
        create_shortcut(
            target_path=self.audio,
            audio_root=self.audio_root,
            shortcut_path=external / f"{self.audio.name}.lnk",
            playlist_root=self.playlist_root,
        )
        self.controller.start_refresh()
        self._wait()
        self.app.processEvents()
        self.assertIn("playlist:外部新增", window.pages)
        self.assertEqual(len(window.pages["playlist:外部新增"].visible_data), 1)

        os.rmdir(self.playlist_root / "通勤")
        self.controller.start_refresh()
        self._wait()
        self.app.processEvents()
        self.assertNotIn("playlist:通勤", window.pages)
        self.assertIs(window.stack.currentWidget(), window.pages["所有音乐"])

    def test_main_close_cancels_refresh_and_late_terminal_does_not_restart_queue(self) -> None:
        item = PlaylistAudioInput(self.asset.id, self.audio, self.audio_root, "active")
        self.controller.start_add("通勤", (item,))
        self._wait()
        window = MainWindow(playlist_controller=self.controller)
        self.addCleanup(window.close)
        window.show()
        self.app.processEvents()
        self._wait()
        entered = threading.Event()
        real_read = playlist_module.read_shortcut

        def slow_read(*args, **kwargs):
            entered.set()
            time.sleep(0.1)
            return real_read(*args, **kwargs)

        with patch.object(playlist_module, "read_shortcut", side_effect=slow_read):
            self.controller.start_refresh(self.playlist_root)
            deadline = time.monotonic() + 2
            while not entered.is_set() and time.monotonic() < deadline:
                self.app.processEvents()
            self.assertTrue(entered.is_set())
            window._pending_refresh_roots.append(("playlist", self.playlist_root))
            window.close()
            self.assertTrue(window.isVisible())
            self.assertEqual(window._pending_refresh_roots, [])
            self._wait()
            for _ in range(6):
                self.app.processEvents()
        self.assertFalse(window.isVisible())
        self.assertEqual(window._pending_refresh_roots, [])
        self.assertFalse(self.controller.running)

    def test_main_window_uses_dynamic_playlists_and_mock_fallback(self) -> None:
        window = MainWindow(playlist_controller=self.controller)
        try:
            self.app.processEvents()
            self._wait()
            self.app.processEvents()
            self.assertIn("playlist:通勤", window.pages)
            self.assertNotIn("playlist:我喜欢的", window.pages)
        finally:
            window.close()
        mock = MainWindow()
        try:
            self.assertIn("playlist:我喜欢的", mock.pages)
        finally:
            mock.close()

    def test_missing_provenance_and_non_active_input_fail_per_item(self) -> None:
        results = []
        self.controller.completed.connect(results.append)
        self.controller.start_add(
            "通勤",
            (PlaylistAudioInput(self.asset.id, self.audio, self.audio_root, "missing"),),
        )
        self._wait()
        self.assertEqual(results[-1].failure_count, 1)
        self.assertEqual(self.controller.load_playlist("通勤"), ())
        history = self.controller.list_history()
        self.assertEqual(history[0].status, "completed")
        self.assertEqual(history[0].items[0].result, "failed")

    def test_cancelled_and_failed_operations_are_persisted_with_terminal_status(self) -> None:
        item = PlaylistAudioInput(self.asset.id, self.audio, self.audio_root, "active")
        create_shortcut = playlist_module.create_shortcut

        def create_then_cancel(**kwargs):
            result = create_shortcut(**kwargs)
            self.controller.request_cancel()
            return result

        cancelled_results = []
        self.controller.cancelled.connect(cancelled_results.append)
        with patch.object(playlist_module, "create_shortcut", side_effect=create_then_cancel):
            self.controller.start_add("通勤", (item, item))
            self._wait()

        self.assertEqual(len(cancelled_results), 1)
        history = self.controller.list_history()
        self.assertEqual(history[0].status, "cancelled")
        self.assertEqual([detail.result for detail in history[0].items], ["success", "cancelled"])

        failed_messages = []
        self.controller.failed.connect(failed_messages.append)
        self.controller.start_remove(
            "不存在的歌单",
            (PlaylistRemovalInput(self.playlist_root / "missing.lnk", self.audio),),
        )
        self._wait()
        self.assertEqual(len(failed_messages), 1)
        history = self.controller.list_history()
        self.assertEqual(history[0].action, "remove")
        self.assertEqual(history[0].status, "failed")
        self.assertEqual(history[0].failure_count, 1)
        self.assertEqual(len(history[0].items), 1)
        self.assertEqual(history[0].items[0].source_path, self.playlist_root / "missing.lnk")
        self.assertEqual(history[0].items[0].target_path, self.audio)
        self.assertEqual(history[0].items[0].result, "failed")
        self.assertIn("不存在", history[0].items[0].message)

    def test_create_add_remove_and_retarget_history_persists_across_restart(self) -> None:
        item = PlaylistAudioInput(self.asset.id, self.audio, self.audio_root, "active")
        self.controller.start_add("通勤", (item,))
        self._wait()
        shortcut = self.controller.load_playlist("通勤")[0]["_shortcut_path"]
        self.controller.start_remove(
            "通勤",
            (PlaylistRemovalInput(shortcut, self.audio),),
        )
        self._wait()
        self.controller.start_add("通勤", (item,))
        self._wait()
        renamed = self.audio_root / "晴天新版-周杰伦.mp3"
        os.rename(self.audio, renamed)
        self.controller.start_retarget(
            (PlaylistRetargetInput(self.audio, renamed, self.audio_root),)
        )
        self._wait()

        reopened = PlaylistController(self.config)
        history = reopened.list_history()
        self.assertEqual(
            {record.action for record in history},
            {"create", "add", "remove", "retarget"},
        )
        self.assertEqual(history[0].action, "retarget")
        self.assertTrue(all(record.created_at for record in history))
        self.assertTrue(all(record.status == "completed" for record in history))
        self.assertTrue(all(record.items for record in history))
        self.assertEqual(history[0].items[0].source_path, self.audio)
        self.assertEqual(history[0].items[0].target_path, renamed)

    def test_history_is_strict_capped_and_rejects_cross_thread_reads(self) -> None:
        for index in range(205):
            self.controller.create_playlist(f"历史{index:03d}")
        history = self.controller.list_history()
        self.assertEqual(len(history), 200)
        self.assertEqual(history[0].playlist_name, "历史204")
        self.assertNotIn("通勤", {record.playlist_name for record in history})

        errors: list[type[BaseException]] = []

        def cross_thread() -> None:
            try:
                self.controller.list_history()
            except BaseException as error:
                errors.append(type(error))

        thread = threading.Thread(target=cross_thread)
        thread.start()
        thread.join(timeout=5)
        self.assertEqual(errors, [RuntimeError])

        with LibraryRepository(self.config) as repository:
            repository.set_setting(PLAYLIST_HISTORY_KEY, [{"action": "forged"}])
        with self.assertRaisesRegex(ValueError, "历史"):
            PlaylistController(self.config).list_history()

        with LibraryRepository(self.config) as repository:
            repository.set_setting(
                PLAYLIST_HISTORY_KEY,
                [
                    {
                        "playlist_name": "伪造",
                        "success_count": 2,
                        "skipped_count": 0,
                        "failure_count": 0,
                        "messages": [],
                        "affected_playlists": ["伪造"],
                        "action": "create",
                        "status": "completed",
                        "created_at": "2026-07-16T00:00:00+00:00",
                        "items": [
                            {
                                "source_path": None,
                                "target_path": str(self.playlist_root / "伪造"),
                                "result": "success",
                                "message": "伪造",
                            }
                        ],
                    }
                ],
            )
        with self.assertRaisesRegex(ValueError, "计数"):
            PlaylistController(self.config).list_history()

        valid_empty = {
            "playlist_name": "空操作",
            "success_count": 0,
            "skipped_count": 0,
            "failure_count": 0,
            "messages": [],
            "affected_playlists": [],
            "action": "create",
            "status": "completed",
            "created_at": "2026-07-16T00:00:00+00:00",
            "items": [],
        }
        with LibraryRepository(self.config) as repository:
            repository.set_setting(PLAYLIST_HISTORY_KEY, [valid_empty] * 201)
        with self.assertRaisesRegex(ValueError, "超过 200"):
            PlaylistController(self.config).list_history()

        cancelled_without_cancelled_item = {
            **valid_empty,
            "playlist_name": "伪造取消",
            "success_count": 1,
            "status": "cancelled",
            "items": [
                {
                    "source_path": str(self.audio),
                    "target_path": str(self.playlist_root / "通勤" / "晴天-周杰伦.mp3.lnk"),
                    "result": "success",
                    "message": "已完成",
                }
            ],
        }
        with LibraryRepository(self.config) as repository:
            repository.set_setting(PLAYLIST_HISTORY_KEY, [cancelled_without_cancelled_item])
        with self.assertRaisesRegex(ValueError, "取消历史"):
            PlaylistController(self.config).list_history()

    def test_history_status_matches_worker_item_results(self) -> None:
        item = PlaylistAudioInput(self.asset.id, self.audio, self.audio_root, "active")
        failed_results = []
        self.controller.completed.connect(failed_results.append)
        with patch.object(playlist_module, "create_shortcut", side_effect=RuntimeError("写入失败")):
            self.controller.start_add("通勤", (item,))
            self._wait()
        self.assertEqual(failed_results[-1].status, "completed")
        self.assertEqual(self.controller.list_history()[0].status, "completed")
        self.assertEqual(self.controller.list_history()[0].items[0].result, "failed")

        original_create = playlist_module.create_shortcut
        calls = 0

        def fail_then_cancel(**kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                self.controller.request_cancel()
                raise RuntimeError("首项失败")
            return original_create(**kwargs)

        cancelled_results = []
        self.controller.cancelled.connect(cancelled_results.append)
        with patch.object(playlist_module, "create_shortcut", side_effect=fail_then_cancel):
            self.controller.start_add("通勤", (item, item))
            self._wait()
        self.assertEqual(cancelled_results[-1].status, "cancelled")
        history = self.controller.list_history()[0]
        self.assertEqual(history.status, "cancelled")
        self.assertEqual([detail.result for detail in history.items], ["failed", "cancelled"])


if __name__ == "__main__":
    unittest.main()
