from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from tempfile import TemporaryDirectory
import unittest

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication, QLabel, QPushButton

from dialogs.delete_confirm_dialog import DeleteConfirmDialog, DeleteLyricsConfirmDialog
from ui.main_window import MainWindow


class FakeLibraryController(QObject):
    library_changed = Signal(object)
    lyrics_changed = Signal(object)
    results_ready = Signal(object)
    completed = Signal(object)
    cancelled = Signal(object)
    failed = Signal(str)
    warning = Signal(str)
    match_changed = Signal(str)
    running_changed = Signal(bool)

    def __init__(self, *, fail_roots: tuple[Path, ...] = ()) -> None:
        super().__init__()
        self.running = False
        self.started: list[Path] = []
        self.cancel_count = 0
        self.fail_roots = set(fail_roots)

    def load_library(self) -> tuple[object, ...]:
        return ()

    def load_lyrics_library(self) -> tuple[object, ...]:
        return ()

    def remembered_root(self) -> None:
        return None

    def start_scan(self, root: Path) -> None:
        if self.running:
            raise RuntimeError("already running")
        if root in self.fail_roots:
            raise RuntimeError("deterministic refresh failure")
        self.started.append(root)
        self.running = True
        self.running_changed.emit(True)

    def finish(self) -> None:
        self.running = False
        self.running_changed.emit(False)

    def request_cancel(self) -> None:
        self.cancel_count += 1
        if self.running:
            self.finish()


class FakeOperationController(QObject):
    completed = Signal(object)
    cancelled = Signal(object)
    failed = Signal(str)
    warning = Signal(str)
    running_changed = Signal(bool)

    def __init__(self) -> None:
        super().__init__()
        self.running = False
        self.started: list[tuple[object, ...]] = []
        self.cancel_count = 0

    def start(self, *args: object) -> None:
        self.started.append(args)

    def start_backup(self, *args: object) -> None:
        self.started.append(args)

    def start_restore(self, *args: object) -> None:
        self.started.append(args)

    def start_cleanup(self, *args: object, **kwargs: object) -> None:
        self.started.append(args + tuple(kwargs.items()))

    def request_cancel(self) -> None:
        self.cancel_count += 1
        if self.running:
            self.running = False
            self.running_changed.emit(False)


class UiRefreshTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()

    def _events(self) -> None:
        for _ in range(4):
            self.app.processEvents()

    def _window(
        self,
        *,
        audio: FakeLibraryController | None = None,
        lyric: FakeLibraryController | None = None,
        safe_import: FakeOperationController | None = None,
        backup: FakeOperationController | None = None,
    ) -> MainWindow:
        window = MainWindow(
            audio,
            lyrics_match_controller=lyric,
            safe_import_controller=safe_import,
            backup_controller=backup,
        )
        window.show()
        self.addCleanup(window.close)
        return window

    def test_import_and_undo_refresh_correct_mode_and_root(self) -> None:
        audio = FakeLibraryController()
        lyric = FakeLibraryController()
        window = self._window(audio=audio, lyric=lyric)
        source = self.root / "source"
        target = self.root / "target"
        source.mkdir()
        target.mkdir()

        window._safe_import_completed(
            SimpleNamespace(
                success_count=1,
                action="import",
                mode="audio",
                source_root=source,
                target_root=target,
                items=(),
            )
        )
        self.assertEqual(audio.started, [target])
        audio.finish()
        self._events()
        window._safe_import_completed(
            SimpleNamespace(
                success_count=1,
                action="undo",
                mode="audio",
                source_root=source,
                target_root=target,
                items=(),
            )
        )
        self.assertEqual(audio.started, [target, source])
        audio.finish()
        self._events()

        window._safe_import_completed(
            SimpleNamespace(
                success_count=1,
                action="import",
                mode="lyrics",
                source_root=source,
                target_root=target,
                items=(),
            )
        )
        self.assertEqual(lyric.started, [target])
        lyric.finish()
        self._events()
        window._safe_import_completed(
            SimpleNamespace(
                success_count=1,
                action="undo",
                mode="lyrics",
                source_root=source,
                target_root=target,
                items=(),
            )
        )
        self.assertEqual(lyric.started, [target, source])

    def test_backup_restore_roots_refresh_sequentially_and_cleanup_never_refreshes(self) -> None:
        first = self.root / "first"
        lyrics = self.root / "lyrics"
        failed = self.root / "failed"
        last = self.root / "last"
        for root in (first, lyrics, failed, last):
            root.mkdir()
        audio = FakeLibraryController(fail_roots=(failed,))
        lyric = FakeLibraryController()
        window = self._window(audio=audio, lyric=lyric)
        roots = (
            ("audio", first),
            ("audio", first),
            ("lyric", lyrics),
            ("audio", failed),
            ("audio", last),
        )

        window._backup_completed(
            SimpleNamespace(
                action="restore",
                success_count=4,
                failure_count=0,
                affected_roots=roots,
            )
        )
        self.assertEqual(audio.started, [first])
        self.assertEqual(lyric.started, [])
        audio.finish()
        self._events()
        self.assertEqual(lyric.started, [lyrics])
        lyric.finish()
        self._events()
        self.assertEqual(audio.started, [first, last])
        audio.finish()
        self._events()
        self.assertEqual(window._pending_refresh_roots, [])

        window._backup_completed(
            SimpleNamespace(
                action="cleanup",
                success_count=2,
                failure_count=0,
                affected_roots=(("audio", first),),
            )
        )
        self._events()
        self.assertEqual(audio.started, [first, last])
        self.assertEqual(lyric.started, [lyrics])

    def test_running_task_rejects_other_user_starts(self) -> None:
        audio = FakeLibraryController()
        lyric = FakeLibraryController()
        safe_import = FakeOperationController()
        backup = FakeOperationController()
        window = self._window(
            audio=audio,
            lyric=lyric,
            safe_import=safe_import,
            backup=backup,
        )
        window.open_import()
        self._events()
        audio.running = True

        window._start_safe_import(self.root, self.root / "target", "audio")
        window._start_read_only_scan(self.root)
        window.open_lyrics_match()
        window._start_lyrics_scan(self.root)
        window._restore_backups(("entry",))
        window._cleanup_backups()
        window._confirm_delete(window.pages["所有音乐"], [{"title": "blocked"}])

        self.assertEqual(safe_import.started, [])
        self.assertEqual(audio.started, [])
        self.assertEqual(lyric.started, [])
        self.assertEqual(backup.started, [])
        self.assertIn("已有后台任务", window.pages["所有音乐"].status.text())

    def test_close_clears_queued_roots_and_never_starts_next_refresh(self) -> None:
        first = self.root / "first"
        second = self.root / "second"
        first.mkdir()
        second.mkdir()
        audio = FakeLibraryController()
        lyric = FakeLibraryController()
        window = self._window(audio=audio, lyric=lyric)
        window._queue_library_refresh((("audio", first), ("lyric", second)))
        self.assertTrue(audio.running)

        window.close()
        self._events()

        self.assertEqual(audio.cancel_count, 1)
        self.assertEqual(lyric.started, [])
        self.assertEqual(window._pending_refresh_roots, [])
        self.assertFalse(window.isVisible())

    def test_late_import_completion_after_close_never_requeues_or_starts_refresh(self) -> None:
        source = self.root / "source"
        target = self.root / "target"
        source.mkdir()
        target.mkdir()
        audio = FakeLibraryController()
        safe_import = FakeOperationController()
        safe_import.running = True
        window = self._window(audio=audio, safe_import=safe_import)

        window.close()
        self.assertTrue(window._close_pending)
        self.assertEqual(window._pending_refresh_roots, [])

        window._safe_import_completed(
            SimpleNamespace(
                success_count=1,
                action="import",
                mode="audio",
                source_root=source,
                target_root=target,
                items=(),
            )
        )
        window._safe_import_cancelled(SimpleNamespace())
        window._safe_import_failed("late failure")
        self._events()

        self.assertEqual(window._pending_refresh_roots, [])
        self.assertEqual(audio.started, [])

    def test_late_backup_completion_after_close_drops_multiple_audio_and_lyrics_roots(self) -> None:
        first = self.root / "first"
        second = self.root / "second"
        lyrics = self.root / "lyrics"
        for root in (first, second, lyrics):
            root.mkdir()
        audio = FakeLibraryController()
        lyric = FakeLibraryController()
        backup = FakeOperationController()
        backup.running = True
        window = self._window(audio=audio, lyric=lyric, backup=backup)

        window.close()
        self.assertTrue(window._close_pending)
        self.assertEqual(window._pending_refresh_roots, [])

        window._backup_completed(
            SimpleNamespace(
                action="restore",
                success_count=3,
                failure_count=0,
                affected_roots=(
                    ("audio", first),
                    ("lyric", lyrics),
                    ("audio", second),
                ),
            )
        )
        window._backup_failed("late failure")
        self._events()

        self.assertEqual(window._pending_refresh_roots, [])
        self.assertEqual(audio.started, [])
        self.assertEqual(lyric.started, [])

    def test_live_delete_dialogs_describe_real_backup_and_remove_lyrics_option(self) -> None:
        live_music = DeleteConfirmDialog([{"title": "歌", "artist": "手"}], live_mode=True)
        live_lyrics = DeleteLyricsConfirmDialog([{"title": "歌", "artist": "手"}], live_mode=True)
        mock_music = DeleteConfirmDialog([{"title": "歌", "artist": "手"}])
        self.addCleanup(live_music.close)
        self.addCleanup(live_lyrics.close)
        self.addCleanup(mock_music.close)

        live_music_text = " ".join(label.text() for label in live_music.findChildren(QLabel))
        live_lyrics_text = " ".join(label.text() for label in live_lyrics.findChildren(QLabel))
        mock_text = " ".join(label.text() for label in mock_music.findChildren(QLabel))
        live_buttons = {button.text() for button in live_music.findChildren(QPushButton)}

        self.assertIn("实际移动", live_music_text)
        self.assertIn("不会随音乐自动删除", live_music_text)
        self.assertIn("仍被音乐引用", live_lyrics_text)
        self.assertIn("移入备份", live_buttons)
        self.assertNotIn("界面演示", live_music_text)
        self.assertIn("界面演示", mock_text)
        self.assertFalse(hasattr(live_music, "delete_lyrics"))


if __name__ == "__main__":
    unittest.main()
