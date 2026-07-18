from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from PySide6.QtWidgets import QApplication

import services.backup_manager as backup_module
from database import DatabaseConfig
from repositories import IndexBatchItem, LibraryRepository
from services.backup_manager import (
    BACKUP_MANIFEST_KEY,
    PENDING_CLEANUP_KEY,
    PENDING_LINKED_BACKUP_KEY,
    BackupController,
    BackupEntry,
    BackupError,
    BackupInput,
    BackupWorker,
)


class BackupManagerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.media_root = self.root / "media"
        self.backup_root = self.root / "backups"
        self.media_root.mkdir()
        self.file = self.media_root / "song.mp3"
        self.file.write_bytes(b"temporary backup audio")
        self.payload = self.file.read_bytes()
        self.config = DatabaseConfig(self.root / "library.sqlite3")
        with LibraryRepository(self.config) as repository:
            session = repository.create_scan_session(mode="audio", source_folder=self.media_root)
            self.asset = repository.index_scan_batch(
                session.id,
                (IndexBatchItem(self.file, self.file.stat().st_size, self.file.stat().st_mtime_ns),),
            )[0].asset
            repository.complete_scan_and_reconcile(session.id)
        self.controller = BackupController(
            backup_root=self.backup_root,
            repository_factory=lambda: LibraryRepository(self.config),
        )

    def _wait(self) -> None:
        deadline = time.monotonic() + 5
        while self.controller.running and time.monotonic() < deadline:
            self.app.processEvents()
        self.app.processEvents()
        self.assertFalse(self.controller.running)

    def _manifest_value(
        self,
        *,
        entry_id: str,
        backup_path: Path,
        sha256: str,
    ) -> dict[str, object]:
        return {
            "id": entry_id,
            "asset_id": self.asset.id,
            "kind": "audio",
            "original_path": str(self.file),
            "backup_path": str(backup_path),
            "sha256": sha256,
            "created_at": "2000-01-01T00:00:00+00:00",
            "restored_at": None,
        }

    def _set_manifest(self, entries: list[dict[str, object]]) -> None:
        with LibraryRepository(self.config) as repository:
            repository.set_setting(BACKUP_MANIFEST_KEY, entries)

    def _raw_manifest(self) -> object:
        with LibraryRepository(self.config) as repository:
            setting = repository.get_setting(BACKUP_MANIFEST_KEY)
        return None if setting is None else setting.value

    def _seed_linked_lyric(self, *, embedded: bool = False):
        lyric_root = self.root / "lyrics"
        lyric_root.mkdir(exist_ok=True)
        lyric_path = lyric_root / "song.lrc"
        lyric_path.write_text("[00:01.00]fixture", encoding="utf-8")
        metadata = lyric_path.stat()
        with LibraryRepository(self.config) as repository:
            session = repository.create_scan_session(mode="lyric", source_folder=lyric_root)
            lyric_asset = repository.index_scan_batch(
                session.id,
                (IndexBatchItem(
                    lyric_path,
                    metadata.st_size,
                    metadata.st_mtime_ns,
                    kind="lyric",
                ),),
            )[0].asset
            repository.finish_scan_session(session.id, status="completed")
            match = repository.commit_lyrics_match(
                audio_asset_id=self.asset.id,
                lyric_asset_id=None if embedded else lyric_asset.id,
                source_kind="embedded" if embedded else "external",
                confidence=100,
                method="automatic",
            )
        return lyric_root, lyric_path, lyric_asset, match

    def _persist_planned_linked_backup(self):
        _root, lyric_path, lyric_asset, match = self._seed_linked_lyric()
        item = BackupInput(
            self.asset.id,
            self.file,
            self.media_root,
            "audio",
            True,
            self.file.stat().st_size,
            self.file.stat().st_mtime_ns,
        )
        self.backup_root.mkdir(exist_ok=True)
        worker = BackupWorker(
            action="backup",
            payload=(item,),
            backup_root=self.backup_root,
            repository_factory=lambda: LibraryRepository(self.config),
        )
        with LibraryRepository(self.config) as repository:
            prepared = worker._prepare_backup_inputs(repository)
            plan = worker._build_linked_backup_plan(
                prepared[0],
                prepared[1],
                repository.list_lyrics_matches(current_only=True)[0],
            )
            repository.set_setting(PENDING_LINKED_BACKUP_KEY, plan)
        return worker, prepared, plan, lyric_path, lyric_asset, match

    def test_backup_and_restore_roundtrip_never_overwrite(self) -> None:
        results: list[object] = []
        self.controller.completed.connect(results.append)
        self.controller.start_backup(
            (BackupInput(self.asset.id, self.file, self.media_root, "audio"),)
        )
        self._wait()
        self.assertFalse(self.file.exists())
        entries = self.controller.list_entries()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].backup_path.read_bytes(), self.payload)
        self.assertEqual(entries[0].allowed_root, self.media_root)
        self.assertEqual(results[-1].affected_roots, (("audio", self.media_root),))
        self.controller.start_restore((entries[0].id,))
        self._wait()
        self.assertEqual(self.file.read_bytes(), self.payload)
        self.assertFalse(entries[0].backup_path.exists())
        self.assertEqual(self.controller.list_entries(), ())
        self.assertEqual(results[-1].action, "restore")
        self.assertEqual(results[-1].affected_roots, (("audio", self.media_root),))

    def test_linked_lyrics_default_off_and_opt_in_group_restore(self) -> None:
        lyric_root, lyric_path, lyric_asset, match = self._seed_linked_lyric()
        self.controller.start_backup(
            (BackupInput(self.asset.id, self.file, self.media_root, "audio"),)
        )
        self._wait()
        self.assertTrue(lyric_path.exists())
        self.assertEqual(len(self.controller.list_entries()), 1)
        with LibraryRepository(self.config) as repository:
            self.assertEqual(repository.list_lyrics_matches(current_only=True)[0].id, match.id)

        # 恢复后重新按显式 opt-in 删除，选择组内任一条目都必须恢复完整组。
        self.controller.start_restore((self.controller.list_entries()[0].id,))
        self._wait()
        with LibraryRepository(self.config) as repository:
            # 恢复后的文件事实与删除前索引不同；首次扫描标记 external_changed，
            # 第二次以同一事实确认稳定后才恢复 active，删除门禁不得绕过该流程。
            for _ in range(2):
                session = repository.create_scan_session(
                    mode="audio",
                    source_folder=self.media_root,
                )
                self.asset = repository.index_scan_batch(
                    session.id,
                    (
                        IndexBatchItem(
                            self.file,
                            self.file.stat().st_size,
                            self.file.stat().st_mtime_ns,
                        ),
                    ),
                )[0].asset
                repository.complete_scan_and_reconcile(session.id)
        metadata = self.file.stat()
        self.controller.start_backup(
            (BackupInput(
                self.asset.id,
                self.file,
                self.media_root,
                "audio",
                True,
                metadata.st_size,
                metadata.st_mtime_ns,
            ),)
        )
        self._wait()
        self.assertFalse(self.file.exists())
        self.assertFalse(lyric_path.exists())
        entries = self.controller.list_entries()
        self.assertEqual(len(entries), 2)
        self.assertEqual(len({entry.link_group_id for entry in entries}), 1)
        self.assertEqual({entry.kind for entry in entries}, {"audio", "lyric"})
        audio_entry = next(entry for entry in entries if entry.kind == "audio")

        self.controller.start_restore((audio_entry.id,))
        self._wait()
        self.assertTrue(self.file.exists())
        self.assertTrue(lyric_path.exists())
        self.assertEqual(self.controller.list_entries(), ())
        with LibraryRepository(self.config) as repository:
            current = repository.list_lyrics_matches(current_only=True)
        self.assertEqual((len(current), current[0].id, current[0].lyric_asset_id), (1, match.id, lyric_asset.id))

    def test_linked_option_never_treats_embedded_lyrics_as_file(self) -> None:
        _root, lyric_path, _lyric_asset, match = self._seed_linked_lyric(embedded=True)
        self.controller.start_backup(
            (BackupInput(
                self.asset.id,
                self.file,
                self.media_root,
                "audio",
                True,
                self.file.stat().st_size,
                self.file.stat().st_mtime_ns,
            ),)
        )
        self._wait()
        self.assertTrue(lyric_path.exists())
        self.assertEqual(len(self.controller.list_entries()), 1)
        with LibraryRepository(self.config) as repository:
            self.assertEqual(repository.list_lyrics_matches(current_only=True)[0].id, match.id)

    def test_linked_cleanup_releases_relation_and_unlink_failure_compensates(self) -> None:
        _root, lyric_path, lyric_asset, match = self._seed_linked_lyric()
        self.controller.start_backup(
            (BackupInput(
                self.asset.id,
                self.file,
                self.media_root,
                "audio",
                True,
                self.file.stat().st_size,
                self.file.stat().st_mtime_ns,
            ),)
        )
        self._wait()
        manifest = self._raw_manifest()
        for item in manifest:
            item["created_at"] = "2000-01-01T00:00:00+00:00"
        self._set_manifest(manifest)
        lyric_entry = next(entry for entry in self.controller.list_entries() if entry.kind == "lyric")
        original_unlink = os.unlink

        def fail_lyric_unlink(path):
            if Path(path).name.startswith(f".{lyric_entry.backup_path.name}."):
                raise PermissionError("deterministic lyric cleanup failure")
            return original_unlink(path)

        with patch("services.backup_manager.os.unlink", side_effect=fail_lyric_unlink):
            self.controller.start_cleanup(retention_days=0)
            self._wait()
        self.assertTrue(lyric_entry.backup_path.exists())
        with LibraryRepository(self.config) as repository:
            self.assertEqual(repository.list_lyrics_matches(current_only=True), ())
            history = repository.list_lyrics_matches(audio_asset_id=self.asset.id)
        self.assertEqual((history[0].id, history[0].state), (match.id, "cancelled"))
        remaining = self.controller.list_entries()
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0].id, lyric_entry.id)
        self.assertIsNone(remaining[0].link_group_id)
        cleanup = self.controller.list_history()[0]
        self.assertEqual(
            [(item.kind, item.result) for item in cleanup.items],
            [("audio", "success"), ("lyric", "failed")],
        )

        self.controller.start_cleanup(retention_days=0)
        self._wait()
        self.assertFalse(lyric_entry.backup_path.exists())
        with LibraryRepository(self.config) as repository:
            self.assertEqual(repository.list_lyrics_matches(current_only=True), ())
            history = repository.list_lyrics_matches(audio_asset_id=self.asset.id)
        self.assertEqual((history[0].id, history[0].state), (match.id, "cancelled"))

    def test_linked_cleanup_first_unlink_failure_restores_whole_group(self) -> None:
        _root, _lyric_path, _lyric_asset, match = self._seed_linked_lyric()
        self.controller.start_backup(
            (
                BackupInput(
                    self.asset.id,
                    self.file,
                    self.media_root,
                    "audio",
                    True,
                    self.file.stat().st_size,
                    self.file.stat().st_mtime_ns,
                ),
            )
        )
        self._wait()
        manifest = self._raw_manifest()
        for item in manifest:
            item["created_at"] = "2000-01-01T00:00:00+00:00"
        self._set_manifest(manifest)
        entries = self.controller.list_entries()
        audio_entry = next(entry for entry in entries if entry.kind == "audio")
        original_unlink = os.unlink

        def fail_first_unlink(path):
            if Path(path).name.startswith(f".{audio_entry.backup_path.name}."):
                raise PermissionError("deterministic first unlink failure")
            return original_unlink(path)

        with patch("services.backup_manager.os.unlink", side_effect=fail_first_unlink):
            self.controller.start_cleanup(retention_days=0)
            self._wait()
        restored = self.controller.list_entries()
        self.assertEqual({entry.id for entry in restored}, {entry.id for entry in entries})
        self.assertTrue(all(entry.backup_path.exists() for entry in restored))
        self.assertEqual(len({entry.link_group_id for entry in restored}), 1)
        with LibraryRepository(self.config) as repository:
            current = repository.list_lyrics_matches(current_only=True)
            journal = repository.get_setting(PENDING_CLEANUP_KEY)
        self.assertEqual(current[0].id, match.id)
        self.assertEqual(journal.value, [])
        cleanup = self.controller.list_history()[0]
        self.assertEqual([item.result for item in cleanup.items], ["failed", "failed"])

    def test_interrupted_linked_cleanup_restores_file_manifest_and_relation(self) -> None:
        _root, _lyric_path, _lyric_asset, match = self._seed_linked_lyric()
        self.controller.start_backup(
            (
                BackupInput(
                    self.asset.id,
                    self.file,
                    self.media_root,
                    "audio",
                    True,
                    self.file.stat().st_size,
                    self.file.stat().st_mtime_ns,
                ),
            )
        )
        self._wait()
        entries = self.controller.list_entries()
        raw_manifest = self._raw_manifest()
        self.assertIsInstance(raw_manifest, list)
        tombstones = {
            entry.id: entry.backup_path.with_name(
                f".{entry.backup_path.name}.interrupted.cleanup"
            )
            for entry in entries
        }
        journal = {
            "version": 2,
            "state": "tombstoned",
            "members": [
                {
                    "entry": next(
                        value for value in raw_manifest if value["id"] == entry.id
                    ),
                    "tombstone_path": str(tombstones[entry.id]),
                }
                for entry in entries
            ],
            "deleted_entry_ids": [],
        }
        for entry in entries:
            entry.backup_path.rename(tombstones[entry.id])
        with LibraryRepository(self.config) as repository:
            lyric_entry = next(entry for entry in entries if entry.kind == "lyric")
            repository.cancel_external_lyrics_match_with_journal(
                match_id=match.id,
                audio_asset_id=self.asset.id,
                lyric_asset_id=lyric_entry.asset_id,
                journal_key=PENDING_CLEANUP_KEY,
                journal=journal,
            )
            repository.set_setting(
                BACKUP_MANIFEST_KEY,
                [],
            )

        # 任一后续 worker 启动都先恢复未完成清理，再执行新动作。
        self.controller.start_cleanup(retention_days=999999)
        self._wait()
        self.assertTrue(all(entry.backup_path.exists() for entry in entries))
        self.assertTrue(all(not tombstone.exists() for tombstone in tombstones.values()))
        self.assertEqual({entry.id for entry in self.controller.list_entries()}, {
            entry.id for entry in entries
        })
        with LibraryRepository(self.config) as repository:
            current = repository.list_lyrics_matches(current_only=True)
            journal = repository.get_setting(PENDING_CLEANUP_KEY)
        self.assertEqual((current[0].id, current[0].state), (match.id, "matched"))
        self.assertEqual(journal.value, [])

    def test_interrupted_cleanup_after_first_unlink_keeps_relation_cancelled(self) -> None:
        _root, _lyric_path, _lyric_asset, match = self._seed_linked_lyric()
        self.controller.start_backup(
            (
                BackupInput(
                    self.asset.id,
                    self.file,
                    self.media_root,
                    "audio",
                    True,
                    self.file.stat().st_size,
                    self.file.stat().st_mtime_ns,
                ),
            )
        )
        self._wait()
        entries = sorted(
            self.controller.list_entries(),
            key=lambda entry: 0 if entry.kind == "audio" else 1,
        )
        raw_manifest = self._raw_manifest()
        tombstones = {
            entry.id: entry.backup_path.with_name(
                f".{entry.backup_path.name}.crash.cleanup"
            )
            for entry in entries
        }
        journal = {
            "version": 2,
            "state": "tombstoned",
            "members": [
                {
                    "entry": next(
                        value for value in raw_manifest if value["id"] == entry.id
                    ),
                    "tombstone_path": str(tombstones[entry.id]),
                }
                for entry in entries
            ],
            "deleted_entry_ids": [],
        }
        for entry in entries:
            entry.backup_path.rename(tombstones[entry.id])
        with LibraryRepository(self.config) as repository:
            repository.cancel_external_lyrics_match_with_journal(
                match_id=match.id,
                audio_asset_id=entries[0].asset_id,
                lyric_asset_id=entries[1].asset_id,
                journal_key=PENDING_CLEANUP_KEY,
                journal=journal,
            )
        tombstones[entries[0].id].unlink()

        self.controller.start_cleanup(retention_days=999999)
        self._wait()
        remaining = self.controller.list_entries()
        self.assertEqual(len(remaining), 1)
        self.assertEqual((remaining[0].kind, remaining[0].link_group_id), ("lyric", None))
        self.assertTrue(remaining[0].backup_path.exists())
        with LibraryRepository(self.config) as repository:
            self.assertEqual(repository.list_lyrics_matches(current_only=True), ())
            history = repository.list_lyrics_matches(audio_asset_id=self.asset.id)
            journal_setting = repository.get_setting(PENDING_CLEANUP_KEY)
        self.assertEqual((history[0].id, history[0].state), (match.id, "cancelled"))
        self.assertEqual(journal_setting.value, [])

        # 第二次启动不重复删除、不恢复旧关系。
        self.controller.start_cleanup(retention_days=999999)
        self._wait()
        self.assertEqual(self.controller.list_entries(), remaining)
        with LibraryRepository(self.config) as repository:
            self.assertEqual(repository.list_lyrics_matches(current_only=True), ())

    def test_linked_backup_lyric_failure_rolls_audio_back_without_group(self) -> None:
        _root, lyric_path, _lyric_asset, _match = self._seed_linked_lyric()
        original_import = backup_module.import_one

        def fail_lyric(source_path, **kwargs):
            if Path(source_path) == lyric_path:
                raise PermissionError("deterministic linked lyric failure")
            return original_import(source_path, **kwargs)

        results: list[object] = []
        self.controller.completed.connect(results.append)
        with patch("services.backup_manager.import_one", side_effect=fail_lyric):
            self.controller.start_backup(
                (
                    BackupInput(
                        self.asset.id,
                        self.file,
                        self.media_root,
                        "audio",
                        True,
                        self.file.stat().st_size,
                        self.file.stat().st_mtime_ns,
                    ),
                )
            )
            self._wait()
        self.assertTrue(self.file.exists())
        self.assertTrue(lyric_path.exists())
        self.assertEqual(self.controller.list_entries(), ())
        self.assertEqual((results[-1].success_count, results[-1].failure_count), (0, 2))
        self.assertEqual([item.result for item in results[-1].items], ["failed", "failed"])
        with LibraryRepository(self.config) as repository:
            journal = repository.get_setting(PENDING_LINKED_BACKUP_KEY)
        self.assertEqual(journal.value, [])

    def test_linked_backup_rollback_failure_is_recovered_on_next_worker(self) -> None:
        _root, lyric_path, _lyric_asset, _match = self._seed_linked_lyric()
        original_import = backup_module.import_one
        calls = {"audio": 0}

        def fail_lyric_and_first_rollback(source_path, **kwargs):
            source = Path(source_path)
            if source == lyric_path:
                raise PermissionError("deterministic linked lyric failure")
            if source.name == self.file.name:
                calls["audio"] += 1
                if calls["audio"] == 2:
                    raise PermissionError("deterministic audio rollback failure")
            return original_import(source_path, **kwargs)

        with patch(
            "services.backup_manager.import_one",
            side_effect=fail_lyric_and_first_rollback,
        ):
            self.controller.start_backup(
                (
                    BackupInput(
                        self.asset.id,
                        self.file,
                        self.media_root,
                        "audio",
                        True,
                        self.file.stat().st_size,
                        self.file.stat().st_mtime_ns,
                    ),
                )
            )
            self._wait()
        self.assertFalse(self.file.exists())
        with LibraryRepository(self.config) as repository:
            journal = repository.get_setting(PENDING_LINKED_BACKUP_KEY)
        self.assertIsInstance(journal.value, dict)

        self.controller.start_cleanup(retention_days=999999)
        self._wait()
        self.assertTrue(self.file.exists())
        self.assertEqual(self.controller.list_entries(), ())
        with LibraryRepository(self.config) as repository:
            journal = repository.get_setting(PENDING_LINKED_BACKUP_KEY)
        self.assertEqual(journal.value, [])

    def test_planned_journal_recovers_audio_moved_before_manifest(self) -> None:
        _worker, prepared, plan, lyric_path, _lyric_asset, match = (
            self._persist_planned_linked_backup()
        )
        audio = prepared[0]
        member = next(value for value in plan["members"] if value["kind"] == "audio")
        backup_path = Path(member["backup_path"])
        backup_path.parent.mkdir()
        moved = backup_module.import_one(
            audio.source_path,
            source_root=audio.allowed_root,
            target_root=backup_path.parent,
        )
        self.assertEqual(moved.target_path, backup_path)
        self.assertFalse(self.file.exists())
        self.assertTrue(backup_path.exists())
        self.assertIsNone(self._raw_manifest())

        self.controller.start_cleanup(retention_days=999999)
        self._wait()
        self.assertTrue(self.file.exists())
        self.assertTrue(lyric_path.exists())
        self.assertFalse(backup_path.exists())
        self.assertEqual(self.controller.list_entries(), ())
        with LibraryRepository(self.config) as repository:
            journal = repository.get_setting(PENDING_LINKED_BACKUP_KEY)
            current = repository.list_lyrics_matches(current_only=True)
        self.assertEqual(journal.value, [])
        self.assertEqual(current[0].id, match.id)

        self.controller.start_cleanup(retention_days=999999)
        self._wait()
        self.assertTrue(self.file.exists())
        self.assertEqual(self.controller.list_entries(), ())

    def test_linked_plan_journal_failure_moves_no_file(self) -> None:
        _root, lyric_path, _lyric_asset, _match = self._seed_linked_lyric()
        original_set_setting = LibraryRepository.set_setting

        def fail_plan(repository, key, value):
            if key == PENDING_LINKED_BACKUP_KEY:
                raise RuntimeError("deterministic planned journal failure")
            return original_set_setting(repository, key, value)

        with (
            patch.object(LibraryRepository, "set_setting", new=fail_plan),
            patch(
                "services.backup_manager.import_one",
                wraps=backup_module.import_one,
            ) as mover,
        ):
            self.controller.start_backup(
                (
                    BackupInput(
                        self.asset.id,
                        self.file,
                        self.media_root,
                        "audio",
                        True,
                        self.file.stat().st_size,
                        self.file.stat().st_mtime_ns,
                    ),
                )
            )
            self._wait()
        self.assertEqual(mover.call_count, 0)
        self.assertTrue(self.file.exists())
        self.assertTrue(lyric_path.exists())
        self.assertEqual(self.controller.list_entries(), ())

    def test_audio_manifest_saved_before_state_advance_recovers_idempotently(self) -> None:
        worker, prepared, plan, lyric_path, _lyric_asset, match = (
            self._persist_planned_linked_backup()
        )
        audio = prepared[0]
        member = next(value for value in plan["members"] if value["kind"] == "audio")
        backup_path = Path(member["backup_path"])
        backup_path.parent.mkdir()
        moved = backup_module.import_one(
            audio.source_path,
            source_root=audio.allowed_root,
            target_root=backup_path.parent,
        )
        with LibraryRepository(self.config) as repository:
            worker._advance_linked_backup_plan(
                repository,
                plan,
                state="audio_moved",
                kind="audio",
                sha256=moved.sha256,
            )
        # 模拟 audio_moved 状态已提交、manifest 已保存，而下一状态尚未推进。
        audio_entry = BackupEntry(
            id=str(member["entry_id"]),
            asset_id=audio.asset_id,
            kind="audio",
            original_path=audio.source_path,
            backup_path=backup_path,
            sha256=str(moved.sha256),
            created_at=str(member["created_at"]),
            allowed_root=audio.allowed_root,
            link_group_id=audio.link_group_id,
            lyrics_match_id=audio.lyrics_match_id,
            linked_audio_asset_id=audio.linked_audio_asset_id,
        )
        with LibraryRepository(self.config) as repository:
            repository.set_setting(
                BACKUP_MANIFEST_KEY,
                [{
                    "id": audio_entry.id,
                    "asset_id": audio_entry.asset_id,
                    "kind": audio_entry.kind,
                    "original_path": str(audio_entry.original_path),
                    "backup_path": str(audio_entry.backup_path),
                    "sha256": audio_entry.sha256,
                    "created_at": audio_entry.created_at,
                    "restored_at": None,
                    "allowed_root": str(audio_entry.allowed_root),
                    "linked_entry_id": None,
                    "link_group_id": audio_entry.link_group_id,
                    "lyrics_match_id": audio_entry.lyrics_match_id,
                    "linked_audio_asset_id": audio_entry.linked_audio_asset_id,
                }],
            )

        self.controller.start_cleanup(retention_days=999999)
        self._wait()
        self.assertTrue(self.file.exists())
        self.assertTrue(lyric_path.exists())
        self.assertEqual(self.controller.list_entries(), ())
        with LibraryRepository(self.config) as repository:
            self.assertEqual(
                repository.get_setting(PENDING_LINKED_BACKUP_KEY).value,
                [],
            )
            self.assertEqual(
                repository.list_lyrics_matches(current_only=True)[0].id,
                match.id,
            )

        self.controller.start_cleanup(retention_days=999999)
        self._wait()
        self.assertEqual(self.controller.list_entries(), ())

    def test_linked_backup_cancel_before_lyric_rolls_audio_back(self) -> None:
        _root, lyric_path, _lyric_asset, _match = self._seed_linked_lyric()
        original_import = backup_module.import_one

        def cancel_after_audio(source_path, **kwargs):
            result = original_import(source_path, **kwargs)
            if Path(source_path) == self.file:
                self.controller.request_cancel()
            return result

        results: list[object] = []
        self.controller.completed.connect(results.append)
        with patch("services.backup_manager.import_one", side_effect=cancel_after_audio):
            self.controller.start_backup(
                (
                    BackupInput(
                        self.asset.id,
                        self.file,
                        self.media_root,
                        "audio",
                        True,
                        self.file.stat().st_size,
                        self.file.stat().st_mtime_ns,
                    ),
                )
            )
            self._wait()
        self.assertTrue(self.file.exists())
        self.assertTrue(lyric_path.exists())
        self.assertEqual(self.controller.list_entries(), ())
        self.assertEqual(results[-1].status, "cancelled")
        self.assertEqual(
            [item.result for item in results[-1].items],
            ["cancelled", "cancelled"],
        )

    def test_corrupt_link_group_manifest_fails_closed(self) -> None:
        self._seed_linked_lyric()
        self.controller.start_backup(
            (
                BackupInput(
                    self.asset.id,
                    self.file,
                    self.media_root,
                    "audio",
                    True,
                    self.file.stat().st_size,
                    self.file.stat().st_mtime_ns,
                ),
            )
        )
        self._wait()
        manifest = self._raw_manifest()
        self.assertIsInstance(manifest, list)
        manifest[1]["lyrics_match_id"] = "different-match"
        self._set_manifest(manifest)
        with self.assertRaisesRegex(BackupError, "关系字段不一致"):
            self.controller.list_entries()

    def test_successful_roots_are_stably_deduplicated(self) -> None:
        second = self.media_root / "second.mp3"
        second.write_bytes(b"second")
        other_root = self.root / "other-media"
        other_root.mkdir()
        third = other_root / "third.flac"
        third.write_bytes(b"third")
        with LibraryRepository(self.config) as repository:
            session = repository.create_scan_session(mode="audio", source_folder=self.media_root)
            records = repository.index_scan_batch(
                session.id,
                tuple(
                    IndexBatchItem(path, path.stat().st_size, path.stat().st_mtime_ns)
                    for path in (self.file, second)
                ),
            )
            repository.complete_scan_and_reconcile(session.id)
            other_session = repository.create_scan_session(mode="audio", source_folder=other_root)
            third_asset = repository.index_scan_batch(
                other_session.id,
                (IndexBatchItem(third, third.stat().st_size, third.stat().st_mtime_ns),),
            )[0].asset
            repository.complete_scan_and_reconcile(other_session.id)
        by_path = {record.asset.canonical_path: record.asset for record in records}
        results: list[object] = []
        self.controller.completed.connect(results.append)

        self.controller.start_backup(
            (
                BackupInput(by_path[self.file].id, self.file, self.media_root, "audio"),
                BackupInput(by_path[second].id, second, self.media_root, "audio"),
                BackupInput(third_asset.id, third, other_root, "audio"),
            )
        )
        self._wait()

        self.assertEqual(results[-1].success_count, 3)
        self.assertEqual(
            results[-1].affected_roots,
            (("audio", self.media_root), ("audio", other_root)),
        )

    def test_legacy_manifest_without_allowed_root_still_loads(self) -> None:
        self.controller.start_backup(
            (BackupInput(self.asset.id, self.file, self.media_root, "audio"),)
        )
        self._wait()
        manifest = self._raw_manifest()
        self.assertIsInstance(manifest, list)
        del manifest[0]["allowed_root"]  # type: ignore[index]
        self._set_manifest(manifest)  # type: ignore[arg-type]

        entries = self.controller.list_entries()

        self.assertEqual(len(entries), 1)
        self.assertIsNone(entries[0].allowed_root)

    def test_restore_conflict_keeps_backup_and_existing_file(self) -> None:
        self.controller.start_backup(
            (BackupInput(self.asset.id, self.file, self.media_root, "audio"),)
        )
        self._wait()
        entry = self.controller.list_entries()[0]
        self.file.write_bytes(b"external")
        self.controller.start_restore((entry.id,))
        self._wait()
        self.assertEqual(self.file.read_bytes(), b"external")
        self.assertTrue(entry.backup_path.exists())

    def test_cleanup_rejects_manifest_path_outside_backup_root(self) -> None:
        outside = self.root / "outside" / self.file.name
        outside.parent.mkdir()
        outside.write_bytes(b"must not be deleted")
        self._set_manifest(
            [
                self._manifest_value(
                    entry_id="forged-entry",
                    backup_path=outside,
                    sha256=hashlib.sha256(outside.read_bytes()).hexdigest(),
                )
            ]
        )

        self.controller.start_cleanup(retention_days=0)
        self._wait()

        self.assertEqual(outside.read_bytes(), b"must not be deleted")
        self.assertEqual(len(self._raw_manifest()), 1)  # type: ignore[arg-type]

    def test_cleanup_rejects_same_root_directory_not_matching_entry_id(self) -> None:
        forged = self.backup_root / "other-entry" / self.file.name
        forged.parent.mkdir(parents=True)
        forged.write_bytes(b"same root but wrong entry directory")
        self._set_manifest(
            [
                self._manifest_value(
                    entry_id="claimed-entry",
                    backup_path=forged,
                    sha256=hashlib.sha256(forged.read_bytes()).hexdigest(),
                )
            ]
        )

        self.controller.start_cleanup(retention_days=0)
        self._wait()

        self.assertEqual(forged.read_bytes(), b"same root but wrong entry directory")
        self.assertEqual(len(self._raw_manifest()), 1)  # type: ignore[arg-type]

    def test_tampered_backup_is_not_moved_during_restore(self) -> None:
        self.controller.start_backup(
            (BackupInput(self.asset.id, self.file, self.media_root, "audio"),)
        )
        self._wait()
        entry = self.controller.list_entries()[0]
        entry.backup_path.write_bytes(b"tampered backup payload")

        self.controller.start_restore((entry.id,))
        self._wait()

        self.assertFalse(self.file.exists())
        self.assertEqual(entry.backup_path.read_bytes(), b"tampered backup payload")
        self.assertIsNone(self.controller.list_entries()[0].restored_at)

    def test_tampered_backup_is_not_deleted_during_cleanup(self) -> None:
        self.controller.start_backup(
            (BackupInput(self.asset.id, self.file, self.media_root, "audio"),)
        )
        self._wait()
        entry = self.controller.list_entries()[0]
        entry.backup_path.write_bytes(b"tampered backup payload")

        self.controller.start_cleanup(retention_days=0)
        self._wait()

        self.assertEqual(entry.backup_path.read_bytes(), b"tampered backup payload")
        self.assertEqual(len(self.controller.list_entries()), 1)

    def test_cleanup_removes_verified_expired_backup_and_manifest(self) -> None:
        results: list[object] = []
        self.controller.completed.connect(results.append)
        self.controller.start_backup(
            (BackupInput(self.asset.id, self.file, self.media_root, "audio"),)
        )
        self._wait()
        entry = self.controller.list_entries()[0]

        self.controller.start_cleanup(retention_days=0)
        self._wait()

        self.assertFalse(entry.backup_path.exists())
        self.assertEqual(self.controller.list_entries(), ())
        self.assertEqual(results[-1].action, "cleanup")
        self.assertEqual(results[-1].affected_roots, ())

    def test_restore_manifest_save_failure_rolls_file_back_to_backup(self) -> None:
        self.controller.start_backup(
            (BackupInput(self.asset.id, self.file, self.media_root, "audio"),)
        )
        self._wait()
        entry = self.controller.list_entries()[0]

        original_set_setting = LibraryRepository.set_setting

        def fail_manifest_save(repository, key, value):
            if key == BACKUP_MANIFEST_KEY:
                raise RuntimeError("deterministic manifest failure")
            return original_set_setting(repository, key, value)

        with patch.object(LibraryRepository, "set_setting", new=fail_manifest_save):
            self.controller.start_restore((entry.id,))
            self._wait()

        self.assertFalse(self.file.exists())
        self.assertEqual(entry.backup_path.read_bytes(), self.payload)
        self.assertIsNone(self.controller.list_entries()[0].restored_at)

    def test_cleanup_manifest_save_failure_keeps_file_and_manifest(self) -> None:
        self.controller.start_backup(
            (BackupInput(self.asset.id, self.file, self.media_root, "audio"),)
        )
        self._wait()
        entry = self.controller.list_entries()[0]

        original_set_setting = LibraryRepository.set_setting

        def fail_manifest_save(repository, key, value):
            if key == BACKUP_MANIFEST_KEY:
                raise RuntimeError("deterministic manifest failure")
            return original_set_setting(repository, key, value)

        with patch.object(LibraryRepository, "set_setting", new=fail_manifest_save):
            self.controller.start_cleanup(retention_days=0)
            self._wait()

        self.assertFalse(entry.backup_path.exists())
        with LibraryRepository(self.config) as repository:
            journal = repository.get_setting(PENDING_CLEANUP_KEY)
        self.assertIsInstance(journal.value, dict)

        self.controller.start_cleanup(retention_days=0)
        self._wait()
        self.assertEqual(self.controller.list_entries(), ())
        with LibraryRepository(self.config) as repository:
            journal = repository.get_setting(PENDING_CLEANUP_KEY)
        self.assertEqual(journal.value, [])

    def test_retention_setting_round_trip(self) -> None:
        self.assertEqual(self.controller.retention_days(), 7)
        self.controller.set_retention_days(30)
        self.assertEqual(self.controller.retention_days(), 30)
        self.controller.set_retention_days(None)
        self.assertIsNone(self.controller.retention_days())

    def test_backup_root_is_read_only_and_prepare_creates_valid_directory(self) -> None:
        self.assertEqual(self.controller.backup_root, self.backup_root)
        self.assertFalse(self.backup_root.exists())

        prepared = self.controller.prepare_backup_root()

        self.assertEqual(prepared, self.backup_root)
        self.assertTrue(prepared.is_dir())

    def test_prepare_backup_root_fails_closed_when_directory_validation_rejects(self) -> None:
        with patch.object(
            BackupWorker,
            "_validate_backup_root_path",
            side_effect=BackupError("deterministic reparse rejection"),
        ):
            with self.assertRaisesRegex(BackupError, "reparse rejection"):
                self.controller.prepare_backup_root()

    def test_cleanup_preview_reports_real_retention_root_and_eligible_count(self) -> None:
        initial = self.controller.cleanup_preview()
        self.assertEqual(
            (initial.backup_root, initial.retention_days, initial.eligible_count),
            (self.backup_root, 7, 0),
        )
        self.controller.start_backup(
            (BackupInput(self.asset.id, self.file, self.media_root, "audio"),)
        )
        self._wait()
        manifest = self._raw_manifest()
        self.assertIsInstance(manifest, list)
        manifest[0]["created_at"] = "2000-01-01T00:00:00+00:00"  # type: ignore[index]
        self._set_manifest(manifest)  # type: ignore[arg-type]

        preview = self.controller.cleanup_preview()

        self.assertEqual(
            (preview.backup_root, preview.retention_days, preview.eligible_count),
            (self.backup_root, 7, 1),
        )
        self.controller.set_retention_days(None)
        permanent = self.controller.cleanup_preview()
        self.assertEqual((permanent.retention_days, permanent.eligible_count), (None, 0))

    def test_cleanup_preview_rejects_invalid_manifest_timestamp(self) -> None:
        self.controller.start_backup(
            (BackupInput(self.asset.id, self.file, self.media_root, "audio"),)
        )
        self._wait()
        manifest = self._raw_manifest()
        self.assertIsInstance(manifest, list)
        manifest[0]["created_at"] = "not-a-timestamp"  # type: ignore[index]
        self._set_manifest(manifest)  # type: ignore[arg-type]

        with self.assertRaisesRegex(BackupError, "创建时间"):
            self.controller.cleanup_preview()


if __name__ == "__main__":
    unittest.main()
