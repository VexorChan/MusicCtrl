from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from services.history_service import HistoryService


class _Source:
    def __init__(self, values=(), error: Exception | None = None) -> None:
        self.values = values
        self.error = error
        self.calls = 0

    def list_history(self):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.values


class _BackupSource:
    def __init__(
        self,
        history,
        entries,
        *,
        history_error: Exception | None = None,
        manifest_error: Exception | None = None,
    ) -> None:
        self.history = history
        self.entries = entries
        self.history_error = history_error
        self.manifest_error = manifest_error
        self.history_calls = 0
        self.manifest_calls = 0

    def list_operation_history(self):
        self.history_calls += 1
        if self.history_error is not None:
            raise self.history_error
        return self.history

    def list_entries(self):
        self.manifest_calls += 1
        if self.manifest_error is not None:
            raise self.manifest_error
        return self.entries


class HistoryServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()

    def _import(self, identifier: str, created: str, *, complete=True, undone=None):
        source = self.root / f"{identifier}.mp3"
        target = self.root / "target" / source.name
        return {
            "id": identifier,
            "created_at": created,
            "mode": "audio",
            "source_root": str(self.root),
            "target_root": str(self.root / "target"),
            "complete": complete,
            "undone_at": undone,
            "items": [
                {
                    "source_path": str(source),
                    "target_path": str(target),
                    "status": "success" if complete else "failed",
                    "message": "完成" if complete else "失败原因",
                }
            ],
        }

    def test_merges_utc_descending_and_marks_only_latest_complete_import_undoable(self) -> None:
        imports = _Source(
            (
                self._import("old", "2026-07-16T01:00:00+00:00"),
                self._import("new", "2026-07-16T10:00:00+08:00"),
                self._import("undone", "2026-07-16T03:00:00+00:00", undone="done"),
            )
        )
        lyrics = _Source(
            (
                {
                    "id": "lyrics",
                    "created_at": "2026-07-16T04:00:00+00:00",
                    "updated_at": "2026-07-16T04:01:00+00:00",
                    "audio_path": self.root / "song.mp3",
                    "lyric_path": self.root / "song.lrc",
                    "source_kind": "external",
                    "method": "manual",
                    "state": "matched",
                },
            )
        )

        snapshot = HistoryService(import_controller=imports, lyrics_controller=lyrics).load()

        self.assertEqual([record.id for record in snapshot.records], ["lyrics", "undone", "new", "old"])
        self.assertEqual([record.id for record in snapshot.records if record.undoable], ["new"])
        self.assertTrue(all(record.created_at.endswith("+00:00") for record in snapshot.records))

    def test_one_source_failure_is_warning_and_other_sources_remain(self) -> None:
        broken = _Source(error=RuntimeError("数据库损坏"))
        imports = _Source((self._import("ok", "2026-07-16T01:00:00+00:00"),))

        snapshot = HistoryService(import_controller=imports, rename_controller=broken).load()

        self.assertEqual([record.id for record in snapshot.records], ["ok"])
        self.assertEqual(snapshot.warnings, ("重命名历史读取失败：数据库损坏",))

    def test_corrupt_item_discards_that_entire_source_instead_of_showing_partial_history(self) -> None:
        imports = _Source(
            (
                self._import("valid", "2026-07-16T01:00:00+00:00"),
                {"id": "corrupt", "created_at": "not-a-time"},
            )
        )

        snapshot = HistoryService(import_controller=imports).load()

        self.assertEqual(snapshot.records, ())
        self.assertEqual(len(snapshot.warnings), 1)
        self.assertIn("导入历史读取失败", snapshot.warnings[0])

    def test_all_source_failures_return_empty_snapshot_with_each_warning(self) -> None:
        controllers = [_Source(error=RuntimeError(f"failure-{index}")) for index in range(5)]

        snapshot = HistoryService(
            import_controller=controllers[0],
            rename_controller=controllers[1],
            backup_controller=controllers[2],
            playlist_controller=controllers[3],
            lyrics_controller=controllers[4],
        ).load()

        self.assertEqual(snapshot.records, ())
        self.assertEqual(len(snapshot.warnings), 5)

    def test_permanent_backup_history_is_not_duplicated_and_restore_is_cross_checked(self) -> None:
        first = SimpleNamespace(id="entry-a", restored_at=None)
        second = SimpleNamespace(id="entry-b", restored_at="2026-07-16T02:00:00+00:00")
        operation = SimpleNamespace(
            id="operation-a",
            action="backup",
            status="success",
            created_at="2026-07-16T01:00:00+00:00",
            success_count=2,
            failure_count=0,
            items=(
                SimpleNamespace(
                    entry_id="entry-a",
                    source_path=self.root / "a.mp3",
                    backup_path=self.root / "backup" / "a.mp3",
                    restore_target=None,
                    result="success",
                    message="已备份",
                    completed_at="2026-07-16T01:00:01+00:00",
                ),
                SimpleNamespace(
                    entry_id="entry-b",
                    source_path=self.root / "b.mp3",
                    backup_path=self.root / "backup" / "b.mp3",
                    restore_target=None,
                    result="success",
                    message="已备份",
                    completed_at="2026-07-16T01:00:02+00:00",
                ),
            ),
        )
        backup = _BackupSource((operation,), (first, second))

        snapshot = HistoryService(backup_controller=backup).load()

        self.assertEqual(len(snapshot.records), 1)
        self.assertEqual(snapshot.records[0].id, "operation-a")
        self.assertEqual(snapshot.records[0].restore_ids, ("entry-a",))

    def test_manifest_failure_keeps_permanent_delete_audit_but_disables_restore(self) -> None:
        operation = SimpleNamespace(
            id="operation-a",
            action="backup",
            status="success",
            created_at="2026-07-16T01:00:00+00:00",
            success_count=1,
            failure_count=0,
            items=(
                SimpleNamespace(
                    entry_id="entry-a",
                    source_path=self.root / "a.mp3",
                    backup_path=self.root / "backup" / "a.mp3",
                    restore_target=None,
                    result="success",
                    message="已备份",
                    completed_at="2026-07-16T01:00:01+00:00",
                ),
            ),
        )
        backup = _BackupSource(
            (operation,),
            (),
            manifest_error=RuntimeError("manifest corrupt"),
        )

        snapshot = HistoryService(backup_controller=backup).load()

        self.assertEqual([record.id for record in snapshot.records], ["operation-a"])
        self.assertEqual(snapshot.records[0].restore_ids, ())
        self.assertEqual(
            snapshot.warnings,
            ("删除历史恢复资格读取失败：manifest corrupt",),
        )
        self.assertEqual((backup.history_calls, backup.manifest_calls), (1, 1))

    def test_permanent_history_failure_never_falls_back_to_manifest_as_audit(self) -> None:
        entry = SimpleNamespace(id="entry-a", restored_at=None)
        backup = _BackupSource(
            (),
            (entry,),
            history_error=RuntimeError("audit corrupt"),
        )

        snapshot = HistoryService(backup_controller=backup).load()

        self.assertEqual(snapshot.records, ())
        self.assertEqual(snapshot.warnings, ("删除历史读取失败：audit corrupt",))
        self.assertEqual((backup.history_calls, backup.manifest_calls), (1, 0))

    def test_legacy_backup_manifest_is_a_compatibility_fallback_only(self) -> None:
        entry = SimpleNamespace(
            id="legacy",
            original_path=self.root / "old.mp3",
            backup_path=self.root / "backup" / "old.mp3",
            created_at="2026-07-16T01:00:00+00:00",
            restored_at=None,
        )
        legacy = SimpleNamespace(list_entries=lambda: (entry,))

        snapshot = HistoryService(backup_controller=legacy).load()

        self.assertEqual([record.id for record in snapshot.records], ["backup:legacy"])
        self.assertEqual(snapshot.records[0].restore_ids, ("legacy",))


if __name__ == "__main__":
    unittest.main()
