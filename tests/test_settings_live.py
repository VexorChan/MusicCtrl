from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication, QDialog, QLineEdit, QMessageBox

from database import DatabaseConfig
from dialogs.settings_dialog import SettingsDialog
from repositories import LibraryRepository
from services.backup_manager import BackupController
from ui.main_window import MainWindow


class RememberedAudioController(QObject):
    library_changed = Signal(object)
    running_changed = Signal(bool)
    warning = Signal(str)

    def __init__(self, root: Path | None) -> None:
        super().__init__()
        self.root = root
        self.running = False
        self.started: list[Path] = []
        self.cancel_count = 0

    def remembered_root(self) -> Path | None:
        return self.root

    def load_library(self) -> tuple[object, ...]:
        return ()

    def start_scan(self, root: Path) -> None:
        if self.running:
            raise RuntimeError("already running")
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


class RememberedLyricsController(QObject):
    lyrics_changed = Signal(object)
    results_ready = Signal(object)
    cancelled = Signal(int)
    failed = Signal(str)
    warning = Signal(str)
    match_changed = Signal(str)
    running_changed = Signal(bool)

    def __init__(self, root: Path | None) -> None:
        super().__init__()
        self.root = root
        self.running = False
        self.started: list[Path] = []
        self.cancel_count = 0

    def remembered_root(self) -> Path | None:
        return self.root

    def load_lyrics_library(self) -> tuple[object, ...]:
        return ()

    def start_scan(self, root: Path) -> None:
        if self.running:
            raise RuntimeError("already running")
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


class RememberedPlaylistController(QObject):
    playlists_changed = Signal(object)
    playlist_changed = Signal(str, object)
    completed = Signal(object)
    cancelled = Signal(object)
    failed = Signal(str)
    running_changed = Signal(bool)

    def __init__(self, root: Path | None) -> None:
        super().__init__()
        self.root = root
        self.running = False

    def remembered_root(self) -> Path | None:
        return self.root

    def list_playlists(self) -> tuple[str, ...]:
        return ()

    def request_cancel(self) -> None:
        self.running = False
        self.running_changed.emit(False)


class SettingsLiveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.audio_root = self.root / "audio"
        self.lyrics_root = self.root / "lyrics"
        self.playlist_root = self.root / "playlists"
        for value in (self.audio_root, self.lyrics_root, self.playlist_root):
            value.mkdir()
        self.config = DatabaseConfig(self.root / "library.sqlite3")
        self.backup = BackupController(
            backup_root=self.root / "app-data" / "backup",
            repository_factory=lambda: LibraryRepository(self.config),
        )

    def _events(self) -> None:
        for _ in range(8):
            self.app.processEvents()

    def _window(
        self,
        *,
        audio_root: Path | None | object = ...,
        lyrics_root: Path | None | object = ...,
        playlist_root: Path | None | object = ...,
    ) -> tuple[MainWindow, RememberedAudioController, RememberedLyricsController]:
        audio = RememberedAudioController(
            self.audio_root if audio_root is ... else audio_root  # type: ignore[arg-type]
        )
        lyrics = RememberedLyricsController(
            self.lyrics_root if lyrics_root is ... else lyrics_root  # type: ignore[arg-type]
        )
        playlist = RememberedPlaylistController(
            self.playlist_root if playlist_root is ... else playlist_root  # type: ignore[arg-type]
        )
        window = MainWindow(
            audio,
            lyrics_match_controller=lyrics,
            playlist_controller=playlist,
            backup_controller=self.backup,
        )
        window.show()
        self.addCleanup(window.close)
        return window, audio, lyrics

    def test_live_dialog_shows_only_real_read_only_paths_and_fixed_rename_rules(self) -> None:
        dialog = SettingsDialog(
            live_mode=True,
            remembered_paths={
                "audio": self.audio_root,
                "lyrics": None,
                "playlist": self.playlist_root,
            },
            backup_root=self.backup.backup_root,
        )
        self.addCleanup(dialog.close)

        self.assertEqual(dialog.path_fields["audio"].text(), str(self.audio_root))
        self.assertEqual(dialog.path_fields["lyrics"].text(), "未选择")
        self.assertEqual(dialog.path_fields["playlist"].text(), str(self.playlist_root))
        self.assertTrue(all(field.isReadOnly() for field in dialog.path_fields.values()))
        self.assertEqual(dialog.backup_path.text(), str(self.backup.backup_root))
        texts = " ".join(
            widget.text() for widget in dialog.findChildren(QLineEdit)
        )
        self.assertNotIn("MusicCtrlDemo", texts)
        self.assertNotIn("Downloads", texts)
        self.assertNotIn("、", texts)
        self.assertEqual(
            set(dialog.maintenance_buttons),
            {"重新检查已标记文件", "打开备份目录", "清理过期备份"},
        )

        mock = SettingsDialog()
        self.addCleanup(mock.close)
        mock_text = " ".join(widget.text() for widget in mock.findChildren(QLineEdit))
        self.assertIn("MusicCtrlDemo", mock_text)

    def test_open_settings_reads_three_real_roots_and_opens_exact_backup_url(self) -> None:
        window, _audio, _lyrics = self._window()
        window.open_settings()
        dialog = window._settings_dialog
        self.assertIsNotNone(dialog)
        self.assertEqual(dialog.path_fields["audio"].text(), str(self.audio_root))  # type: ignore[union-attr]
        self.assertEqual(dialog.path_fields["lyrics"].text(), str(self.lyrics_root))  # type: ignore[union-attr]
        self.assertEqual(dialog.path_fields["playlist"].text(), str(self.playlist_root))  # type: ignore[union-attr]

        with patch("ui.main_window.QDesktopServices.openUrl", return_value=True) as opened:
            dialog.maintenance_buttons["打开备份目录"].click()  # type: ignore[union-attr]

        self.assertTrue(self.backup.backup_root.is_dir())
        self.assertEqual(opened.call_count, 1)
        self.assertEqual(
            Path(opened.call_args.args[0].toLocalFile()).resolve(),
            self.backup.backup_root,
        )
        self.assertIn("已打开备份目录", dialog.status.text())  # type: ignore[union-attr]

    def test_retention_save_failure_keeps_dialog_open_and_success_closes_it(self) -> None:
        window, _audio, _lyrics = self._window()
        window.open_settings()
        dialog = window._settings_dialog
        self.assertIsNotNone(dialog)
        dialog.retention.setCurrentText("15 天")  # type: ignore[union-attr]

        with patch.object(
            self.backup,
            "set_retention_days",
            side_effect=RuntimeError("deterministic save failure"),
        ):
            dialog._save()  # type: ignore[union-attr]

        self.assertEqual(dialog.result(), 0)  # type: ignore[union-attr]
        self.assertIn("save failure", dialog.status.text())  # type: ignore[union-attr]

        dialog._save()  # type: ignore[union-attr]

        self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted)  # type: ignore[union-attr]
        self.assertEqual(self.backup.retention_days(), 15)

    def test_recheck_runs_remembered_audio_then_lyrics_and_rejects_busy_or_empty(self) -> None:
        window, audio, lyrics = self._window()
        window.open_settings()
        dialog = window._settings_dialog
        dialog.maintenance_buttons["重新检查已标记文件"].click()  # type: ignore[union-attr]
        self.assertEqual(audio.started, [self.audio_root])
        self.assertEqual(lyrics.started, [])

        audio.finish()
        self._events()
        self.assertEqual(lyrics.started, [self.lyrics_root])
        lyrics.finish()
        self._events()

        audio.running = True
        audio.running_changed.emit(True)
        self.assertFalse(
            dialog.maintenance_buttons["重新检查已标记文件"].isEnabled()  # type: ignore[union-attr]
        )
        self.assertIn("已有后台任务", dialog.status.text())  # type: ignore[union-attr]
        dialog.maintenance_buttons["重新检查已标记文件"].setEnabled(True)  # type: ignore[union-attr]
        dialog.maintenance_buttons["重新检查已标记文件"].click()  # type: ignore[union-attr]
        self.assertIn("已有后台任务", dialog.status.text())  # type: ignore[union-attr]
        self.assertEqual(audio.started, [self.audio_root])
        audio.running = False

        empty_window, empty_audio, empty_lyrics = self._window(
            audio_root=None,
            lyrics_root=None,
            playlist_root=None,
        )
        empty_window.open_settings()
        empty_dialog = empty_window._settings_dialog
        empty_dialog.maintenance_buttons["重新检查已标记文件"].click()  # type: ignore[union-attr]
        self.assertEqual((empty_audio.started, empty_lyrics.started), ([], []))
        self.assertIn("尚未记住", empty_dialog.status.text())  # type: ignore[union-attr]

    def test_cleanup_requires_real_nonzero_preview_and_confirmation_with_count_and_root(self) -> None:
        window, _audio, _lyrics = self._window()
        window.open_settings()
        dialog = window._settings_dialog
        self.assertIsNotNone(dialog)
        cleanup = dialog.maintenance_buttons["清理过期备份"]  # type: ignore[union-attr]

        with patch.object(
            self.backup,
            "cleanup_preview",
            return_value=SimpleNamespace(
                retention_days=None,
                eligible_count=0,
                backup_root=self.backup.backup_root,
            ),
        ), patch.object(self.backup, "start_cleanup") as started, patch(
            "ui.main_window.QMessageBox.warning"
        ) as warning:
            cleanup.click()
        started.assert_not_called()
        warning.assert_not_called()
        self.assertIn("永久保留", dialog.status.text())  # type: ignore[union-attr]

        preview = SimpleNamespace(
            retention_days=7,
            eligible_count=3,
            backup_root=self.backup.backup_root,
        )
        with patch.object(self.backup, "cleanup_preview", return_value=preview), patch.object(
            self.backup, "start_cleanup"
        ) as started, patch(
            "ui.main_window.QMessageBox.warning",
            return_value=QMessageBox.StandardButton.No,
        ) as warning:
            cleanup.click()
        started.assert_not_called()
        message = warning.call_args.args[2]
        self.assertIn("3 个", message)
        self.assertIn(str(self.backup.backup_root), message)

        with patch.object(self.backup, "cleanup_preview", return_value=preview), patch.object(
            self.backup, "start_cleanup"
        ) as started, patch(
            "ui.main_window.QMessageBox.warning",
            return_value=QMessageBox.StandardButton.Yes,
        ):
            cleanup.click()
        started.assert_called_once_with()
        self.assertIn("正在后台清理", dialog.status.text())  # type: ignore[union-attr]

        window._backup_completed(
            SimpleNamespace(action="cleanup", success_count=3, failure_count=0)
        )
        self.assertIn("已永久清理", dialog.status.text())  # type: ignore[union-attr]
        window._backup_failed("deterministic cleanup failure")
        self.assertIn("cleanup failure", dialog.status.text())  # type: ignore[union-attr]


if __name__ == "__main__":
    unittest.main()
