from __future__ import annotations

import ast
import os
from pathlib import Path
import stat
import subprocess
from tempfile import TemporaryDirectory
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import wave

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from mutagen.id3 import TIT2, TPE1
from mutagen.wave import WAVE
from PySide6.QtCore import QCoreApplication, QThread
from PySide6.QtWidgets import QApplication

from services.metadata_preview import (
    MetadataPreviewController,
    MetadataPreviewError,
    MetadataPreviewInput,
    MetadataPreviewWorker,
    build_metadata_previews,
)


class _TaggedMedia:
    def __init__(self, *, title=None, artist=None) -> None:
        self.tags = {}
        if title is not None:
            self.tags["title"] = title
        if artist is not None:
            self.tags["artist"] = artist


def _app() -> QApplication:
    instance = QApplication.instance()
    if instance is not None:
        return instance
    return QApplication([])


def _wait_until(predicate, timeout_ms: int = 3000) -> bool:
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        QCoreApplication.processEvents()
        if predicate():
            return True
        threading.Event().wait(0.005)
    QCoreApplication.processEvents()
    return bool(predicate())


class MetadataPreviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = _app()

    def setUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        self.root = Path(self.temporary_directory.name).resolve()

    def tearDown(self) -> None:
        QCoreApplication.processEvents()
        self.temporary_directory.cleanup()

    def _file(self, name: str, content: bytes = b"fixture") -> Path:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def _wave(self, name: str) -> Path:
        path = self.root / name
        with wave.open(os.fspath(path), "wb") as stream:
            stream.setnchannels(1)
            stream.setsampwidth(1)
            stream.setframerate(8000)
            stream.writeframes(b"\x80" * 32)
        return path

    def _input(
        self,
        path: Path,
        *,
        asset_id: str = "asset-1",
        allowed_root: Path | None = None,
        state: str = "active",
        size_bytes: int | None = None,
        mtime_ns: int | None = None,
    ) -> MetadataPreviewInput:
        if path.exists():
            metadata = path.stat()
            if size_bytes is None:
                size_bytes = metadata.st_size
            if mtime_ns is None:
                mtime_ns = metadata.st_mtime_ns
        return MetadataPreviewInput(
            asset_id=asset_id,
            canonical_path=path,
            allowed_root=self.root if allowed_root is None else allowed_root,
            file_state=state,
            size_bytes=0 if size_bytes is None else size_bytes,
            mtime_ns=mtime_ns,
        )

    def _analyze(self, *items: MetadataPreviewInput):
        return build_metadata_previews(items)

    def test_both_tags_are_required_and_multiple_artists_are_normalized(self) -> None:
        tagged = self._file("ignored-name.mp3")
        fallback = self._file("回退歌名-回退歌手.flac")
        with patch(
            "services.metadata_preview.MutagenFile",
            side_effect=[
                _TaggedMedia(title=["  标题  "], artist=["歌手甲", " 歌手乙 "]),
                _TaggedMedia(title=["只有标题"], artist=[]),
            ],
        ):
            results = self._analyze(
                self._input(tagged, asset_id="tagged"),
                self._input(fallback, asset_id="fallback"),
            )

        self.assertEqual(results[0].suggested_stem, "标题-歌手甲、歌手乙")
        self.assertEqual(results[0].source, "标签")
        self.assertEqual(results[0].status, "可预览")
        self.assertFalse(results[0].requires_confirmation)
        self.assertEqual(results[1].suggested_stem, "回退歌名-回退歌手")
        self.assertEqual(results[1].source, "文件名")

    def test_file_name_fallback_uses_the_last_half_width_hyphen(self) -> None:
        path = self._file("歌-名-歌手.m4a")
        with patch("services.metadata_preview.MutagenFile", return_value=None):
            result = self._analyze(self._input(path))[0]
        self.assertEqual(result.suggested_stem, "歌-名-歌手")
        self.assertEqual(result.source, "文件名")

    def test_unidentifiable_and_unsafe_names_require_manual_confirmation(self) -> None:
        no_separator = self._file("无法识别.ogg")
        empty_side = self._file("歌名-.aac")
        reserved = self._file("placeholder.wav")
        with patch(
            "services.metadata_preview.MutagenFile",
            side_effect=[None, None, _TaggedMedia(title=["CON"], artist=["歌手"]),],
        ):
            results = self._analyze(
                self._input(no_separator, asset_id="no-separator"),
                self._input(empty_side, asset_id="empty-side"),
                self._input(reserved, asset_id="reserved"),
            )
        for result in results:
            self.assertTrue(result.requires_confirmation)
            self.assertIn(result.status, {"待手动确认", "冲突"})
        self.assertIsNone(results[0].suggested_stem)
        self.assertIsNone(results[1].suggested_stem)
        self.assertIn("保留", results[2].message)

    def test_six_corrupt_formats_are_single_item_failures_not_batch_failure(self) -> None:
        extensions = (".mp3", ".flac", ".wav", ".m4a", ".ogg", ".aac")
        items = [
            self._input(self._file(f"标题{index}-歌手{index}{extension}", b"not-media"), asset_id=str(index))
            for index, extension in enumerate(extensions)
        ]
        results = self._analyze(*items)
        self.assertEqual(len(results), len(items))
        self.assertTrue(all(result.suggested_stem for result in results))
        self.assertTrue(all(result.source == "文件名" for result in results))

    def test_corrupt_media_with_parseable_file_name_is_never_ready_by_default(self) -> None:
        path = self._file("损坏歌-歌手.mp3", b"definitely-not-an-mp3")
        result = self._analyze(self._input(path))[0]
        self.assertEqual(result.suggested_stem, "损坏歌-歌手")
        self.assertNotEqual(result.status, "可预览")
        self.assertTrue(result.requires_confirmation)
        self.assertIn("标签", result.message)

    def test_real_wave_is_read_only_and_falls_back_without_tags(self) -> None:
        path = self._wave("真实波形-测试歌手.wav")
        before = (path.read_bytes(), path.stat().st_size, path.stat().st_mtime_ns, tuple(self.root.iterdir()))
        result = self._analyze(self._input(path))[0]
        after = (path.read_bytes(), path.stat().st_size, path.stat().st_mtime_ns, tuple(self.root.iterdir()))
        self.assertEqual(result.suggested_stem, "真实波形-测试歌手")
        self.assertEqual(before, after)

    def test_real_wave_id3_frames_are_read_and_analysis_does_not_change_fixture(self) -> None:
        path = self._wave("不应使用文件名-回退.wav")
        media = WAVE(os.fspath(path))
        media.add_tags()
        media.tags.add(TIT2(encoding=3, text=["真实标题"]))
        media.tags.add(TPE1(encoding=3, text=["歌手甲", "歌手乙"]))
        media.save()
        snapshot = (
            path.read_bytes(),
            path.stat().st_size,
            path.stat().st_mtime_ns,
            tuple(sorted(item.name for item in self.root.iterdir())),
        )

        result = self._analyze(self._input(path))[0]

        self.assertEqual(result.title, "真实标题")
        self.assertEqual(result.artist, "歌手甲、歌手乙")
        self.assertEqual(result.suggested_stem, "真实标题-歌手甲、歌手乙")
        self.assertEqual(result.source, "标签")
        self.assertEqual(
            snapshot,
            (
                path.read_bytes(),
                path.stat().st_size,
                path.stat().st_mtime_ns,
                tuple(sorted(item.name for item in self.root.iterdir())),
            ),
        )

    @unittest.skipUnless(os.name == "nt", "真实 junction 换根门禁仅适用于 Windows")
    def test_verified_open_handle_blocks_or_survives_transient_junction_root_swap(self) -> None:
        import services.metadata_preview as metadata_module

        allowed = self.root / "allowed"
        inside_directory = allowed / "music"
        outside_directory = self.root / "outside"
        inside_directory.mkdir(parents=True)
        outside_directory.mkdir()
        inside_path = inside_directory / "track.wav"
        outside_path = outside_directory / "track.wav"

        def write_tagged_wave(path: Path, title: str, artist: str) -> None:
            with wave.open(os.fspath(path), "wb") as stream:
                stream.setnchannels(1)
                stream.setsampwidth(1)
                stream.setframerate(8000)
                stream.writeframes(b"\x80" * 32)
            media = WAVE(os.fspath(path))
            media.add_tags()
            media.tags.add(TIT2(encoding=3, text=[title]))
            media.tags.add(TPE1(encoding=3, text=[artist]))
            media.save()

        write_tagged_wave(inside_path, "INSIDE", "可信歌手")
        write_tagged_wave(outside_path, "OUTSIDE", "越界歌手")
        item = self._input(inside_path, allowed_root=allowed)

        attack_link = allowed / "prepared-junction"
        completed = subprocess.run(
            ["cmd.exe", "/d", "/c", "mklink", "/J", str(attack_link), str(outside_directory)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if completed.returncode != 0:
            self.skipTest(f"当前环境无法创建真实 junction：{completed.stderr.strip()}")
        self.addCleanup(
            lambda: attack_link.rmdir() if os.path.lexists(attack_link) else None
        )
        self.assertTrue(int(getattr(os.lstat(attack_link), "st_file_attributes", 0)) & 0x0400)

        actual_mutagen = metadata_module.MutagenFile
        real_fstat = os.fstat
        real_lstat = os.lstat
        received_streams: list[object] = []
        handle_stats: list[os.stat_result] = []
        path_stats: list[os.stat_result] = []
        swap_outcome: list[str] = []

        def tracking_fstat(fd: int):
            metadata = real_fstat(fd)
            handle_stats.append(metadata)
            return metadata

        def tracking_lstat(candidate):
            metadata = real_lstat(candidate)
            if Path(candidate) == inside_path:
                path_stats.append(metadata)
            return metadata

        def transient_swap(stream, *, easy=True):
            self.assertNotIsInstance(stream, (str, bytes, Path))
            self.assertTrue(callable(getattr(stream, "read", None)))
            self.assertTrue(callable(getattr(stream, "seek", None)))
            self.assertTrue(callable(getattr(stream, "fileno", None)))
            self.assertFalse(stream.closed)
            received_streams.append(stream)
            holding = allowed / "held-original"
            try:
                os.rename(inside_directory, holding)
            except PermissionError:
                swap_outcome.append("blocked-by-open-handle")
                return actual_mutagen(stream, easy=easy)

            swap_outcome.append("junction-swapped")
            os.rename(attack_link, inside_directory)
            try:
                return actual_mutagen(stream, easy=easy)
            finally:
                os.rename(inside_directory, attack_link)
                os.rename(holding, inside_directory)

        with (
            patch("services.metadata_preview.MutagenFile", side_effect=transient_swap),
            patch("services.metadata_preview.os.fstat", side_effect=tracking_fstat),
            patch("services.metadata_preview.os.lstat", side_effect=tracking_lstat),
        ):
            result = self._analyze(item)[0]

        self.assertEqual(len(swap_outcome), 1)
        self.assertIn(swap_outcome[0], {"blocked-by-open-handle", "junction-swapped"})
        self.assertEqual(len(received_streams), 1)
        self.assertTrue(received_streams[0].closed)
        self.assertGreaterEqual(len(handle_stats), 2)
        self.assertGreaterEqual(len(path_stats), 2)
        self.assertEqual(
            (handle_stats[0].st_dev, handle_stats[0].st_ino),
            (handle_stats[-1].st_dev, handle_stats[-1].st_ino),
        )
        self.assertEqual(
            (path_stats[0].st_dev, path_stats[0].st_ino),
            (path_stats[-1].st_dev, path_stats[-1].st_ino),
        )
        self.assertEqual(result.title, "INSIDE")
        self.assertEqual(result.artist, "可信歌手")
        self.assertNotEqual(result.title, "OUTSIDE")
        self.assertEqual(result.status, "可预览")
        self.assertFalse(result.requires_confirmation)
        self.assertTrue(inside_path.is_file())
        self.assertTrue(outside_path.is_file())

    def test_missing_never_calls_mutagen_or_touches_the_file(self) -> None:
        missing = self.root / "missing.mp3"
        item = self._input(missing, state="missing", size_bytes=123, mtime_ns=456)
        with patch("services.metadata_preview.MutagenFile", side_effect=AssertionError("Mutagen must not run")):
            result = self._analyze(item)[0]
        self.assertEqual(result.status, "文件缺失")
        self.assertTrue(result.requires_confirmation)
        self.assertFalse(missing.exists())

    def test_external_changed_is_reparsed_every_time_and_requires_confirmation(self) -> None:
        path = self._file("标题-歌手.mp3")
        item = self._input(path, state="external_changed")
        with patch(
            "services.metadata_preview.MutagenFile",
            return_value=_TaggedMedia(title=["新标题"], artist=["新歌手"]),
        ) as mutagen:
            first = self._analyze(item)[0]
            second = self._analyze(item)[0]
        self.assertEqual(mutagen.call_count, 2)
        self.assertEqual(first.suggested_stem, "新标题-新歌手")
        self.assertEqual(second.status, "外部变化")
        self.assertTrue(second.requires_confirmation)

    def test_active_snapshot_drift_and_during_analysis_drift_fail_closed(self) -> None:
        before_open = self._file("漂移前-歌手.mp3")
        with patch(
            "services.metadata_preview.MutagenFile",
            return_value=_TaggedMedia(title=["漂移前"], artist=["歌手"]),
        ):
            changed = self._analyze(self._input(before_open, size_bytes=before_open.stat().st_size + 1))[0]
        self.assertEqual(changed.status, "外部变化")
        self.assertTrue(changed.requires_confirmation)

        during = self._file("分析中-歌手.flac")
        item = self._input(during)

        def mutate(_stream):
            during.write_bytes(during.read_bytes() + b"changed")
            return "分析中", "歌手", None, False

        with patch("services.metadata_preview._read_tags", side_effect=mutate):
            result = self._analyze(item)[0]
        self.assertEqual(result.status, "分析失败")
        self.assertTrue(
            "期间发生变化" in result.message or "无法安全打开" in result.message,
            result.message,
        )
        self.assertTrue(result.requires_confirmation)

    def test_same_size_same_mtime_path_replacement_during_tag_read_is_never_ready(self) -> None:
        path = self._file("身份替换-歌手.mp3", b"AAAA")
        item = self._input(path)
        original = path.stat()

        replace_blocked: list[bool] = []

        def replace_during_read(_stream):
            replacement = self.root / "replacement.tmp"
            replacement.write_bytes(b"BBBB")
            os.utime(
                replacement,
                ns=(original.st_atime_ns, original.st_mtime_ns),
            )
            try:
                os.replace(replacement, path)
            except PermissionError:
                replace_blocked.append(True)
                replacement.unlink()
                raise
            os.utime(path, ns=(original.st_atime_ns, original.st_mtime_ns))
            return "身份替换", "歌手", None, False

        with patch("services.metadata_preview._read_tags", side_effect=replace_during_read):
            result = self._analyze(item)[0]

        self.assertIn(path.read_bytes(), {b"AAAA", b"BBBB"})
        self.assertEqual(path.stat().st_size, original.st_size)
        self.assertEqual(path.stat().st_mtime_ns, original.st_mtime_ns)
        self.assertNotEqual(result.status, "可预览")
        self.assertTrue(result.requires_confirmation)
        self.assertTrue(
            "期间发生变化" in result.message
            or "无法安全打开" in result.message
            or bool(replace_blocked),
            result.message,
        )

    def test_disappearing_file_is_a_single_failure(self) -> None:
        path = self._file("消失-歌手.mp3")
        item = self._input(path)

        def disappear(_stream):
            try:
                path.unlink()
            except PermissionError:
                raise
            return "消失", "歌手", None, False

        with patch("services.metadata_preview._read_tags", side_effect=disappear):
            result = self._analyze(item)[0]
        self.assertEqual(result.status, "分析失败")
        self.assertTrue("分析后" in result.message or "无法安全打开" in result.message)

    def test_permission_error_is_a_single_item_failure(self) -> None:
        path = self._file("无权限-歌手.mp3")
        item = self._input(path)
        real_lstat = os.lstat

        def denied(candidate):
            if Path(candidate) == path:
                raise PermissionError("denied")
            return real_lstat(candidate)

        with patch("services.metadata_preview.os.lstat", side_effect=denied):
            result = self._analyze(item)[0]
        self.assertEqual(result.status, "分析失败")
        self.assertIn("无法读取文件信息", result.message)

    def test_root_escape_and_reparse_file_are_rejected(self) -> None:
        with TemporaryDirectory() as outside_directory:
            outside = Path(outside_directory).resolve() / "outside.mp3"
            outside.write_bytes(b"outside")
            with self.assertRaises(MetadataPreviewError):
                self._analyze(self._input(outside))

        path = self._file("link.mp3")
        item = self._input(path)
        real_lstat = os.lstat

        def reparse_lstat(candidate):
            if Path(candidate) == path:
                current = real_lstat(candidate)
                return SimpleNamespace(
                    st_mode=stat.S_IFREG,
                    st_file_attributes=0x0400,
                    st_size=current.st_size,
                    st_mtime_ns=current.st_mtime_ns,
                )
            return real_lstat(candidate)

        with patch("services.metadata_preview.os.lstat", side_effect=reparse_lstat):
            result = self._analyze(item)[0]
        self.assertEqual(result.status, "分析失败")
        self.assertIn("重解析点", result.message)

    def test_existing_target_batch_collision_and_source_identity(self) -> None:
        first = self._file("one.mp3")
        second = self._file("two.mp3")
        existing_source = self._file("原样-歌手.mp3")
        self._file("目标-歌手.MP3", b"existing")
        with patch(
            "services.metadata_preview.MutagenFile",
            side_effect=[
                _TaggedMedia(title=["目标"], artist=["歌手"]),
                _TaggedMedia(title=["目标"], artist=["歌手"]),
                _TaggedMedia(title=["原样"], artist=["歌手"]),
            ],
        ):
            results = self._analyze(
                self._input(first, asset_id="one"),
                self._input(second, asset_id="two"),
                self._input(existing_source, asset_id="self"),
            )
        self.assertEqual([result.status for result in results[:2]], ["冲突", "冲突"])
        self.assertEqual(results[2].status, "可预览")

    def test_windows_trailing_dot_and_space_equivalent_target_is_a_conflict(self) -> None:
        source = self._file("source.mp3")
        fake_entries = [SimpleNamespace(name="目标-歌手.MP3. ")]
        with (
            patch(
                "services.metadata_preview.MutagenFile",
                return_value=_TaggedMedia(title=["目标"], artist=["歌手"]),
            ),
            patch("services.metadata_preview.os.scandir", return_value=fake_entries),
        ):
            result = self._analyze(self._input(source))[0]
        self.assertEqual(result.status, "冲突")
        self.assertTrue(result.requires_confirmation)

    def test_invalid_batch_inputs_fail_before_mutagen_runs(self) -> None:
        path = self._file("标题-歌手.mp3")
        duplicate = self._input(path, asset_id="duplicate")
        with patch("services.metadata_preview.MutagenFile") as mutagen:
            with self.assertRaises(MetadataPreviewError):
                self._analyze(duplicate, duplicate)
        mutagen.assert_not_called()

    def test_worker_runs_off_main_thread_and_cancel_discards_partial_results(self) -> None:
        first = self._file("first-artist.mp3")
        second = self._file("second-artist.mp3")
        entered = threading.Event()
        release = threading.Event()
        calls: list[Path] = []
        worker_threads: list[QThread] = []

        def blocking_read(stream):
            opened_path = Path(stream.name)
            calls.append(opened_path)
            worker_threads.append(QThread.currentThread())
            entered.set()
            self.assertTrue(release.wait(2))
            return opened_path.stem.rsplit("-", 1)[0], "artist", None, False

        completed: list[object] = []
        cancelled: list[int] = []
        failed: list[str] = []
        worker = MetadataPreviewWorker(
            (self._input(first, asset_id="first"), self._input(second, asset_id="second")),
        )
        worker.completed.connect(completed.append)
        worker.cancelled.connect(cancelled.append)
        worker.failed.connect(failed.append)
        with patch("services.metadata_preview._read_tags", side_effect=blocking_read):
            worker.start()
            self.assertTrue(entered.wait(2))
            worker.request_cancel()
            release.set()
            self.assertTrue(
                _wait_until(
                    lambda: not worker.isRunning()
                    and bool(completed or cancelled or failed)
                )
            )
            QCoreApplication.processEvents()
        self.assertEqual(len(calls), 1)
        self.assertNotEqual(worker_threads, [QCoreApplication.instance().thread()])
        self.assertEqual(completed, [])
        self.assertEqual(cancelled, [0], f"unexpected terminal: completed={completed}, failed={failed}")
        self.assertEqual(failed, [])
        with self.assertRaises(RuntimeError):
            worker.start()

    def test_controller_three_rounds_have_one_terminal_and_no_thread_leak(self) -> None:
        path = self._file("标题-歌手.mp3")
        controller = MetadataPreviewController()
        results: list[object] = []
        cancelled: list[int] = []
        failed: list[str] = []
        controller.results_ready.connect(results.append)
        controller.cancelled.connect(cancelled.append)
        controller.failed.connect(failed.append)
        with patch("services.metadata_preview.MutagenFile", return_value=None):
            for _ in range(3):
                controller.start((self._input(path),))
                self.assertTrue(_wait_until(lambda: not controller.running))
        self.assertEqual(len(results), 3)
        self.assertEqual(cancelled, [])
        self.assertEqual(failed, [])
        self.assertEqual(controller.findChildren(QThread), [])

    def test_production_module_has_no_write_database_or_hash_operations(self) -> None:
        source_path = Path(__file__).resolve().parents[1] / "services" / "metadata_preview.py"
        source = source_path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        forbidden_imports = {"hashlib", "sqlite3", "repositories", "database", "shutil"}
        imported: set[str] = set()
        called_attributes: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                called_attributes.add(node.func.attr)
        self.assertTrue(forbidden_imports.isdisjoint(imported))
        self.assertTrue(
            {"save", "write", "write_bytes", "write_text", "rename", "replace", "unlink", "remove", "move"}
            .isdisjoint(called_attributes)
        )


if __name__ == "__main__":
    unittest.main()
