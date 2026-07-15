from __future__ import annotations

import ast
import base64
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from services.safe_metadata import (
    MetadataWriteInput,
    SafeMetadataError,
    _read_title_artist,
    apply_prepared_metadata,
    discard_prepared_metadata,
    finalize_metadata_write,
    prepare_metadata_write,
    rollback_metadata_write,
)


_AUDIO_FIXTURES = {
    ".flac": "ZkxhQwAAACICQAJAAAANAAANAfQA8AAAAZBrQxvy2nyTErPlwh5n9lkbBAAAUwwAAABMYXZmNjEuNy4xMDADAAAADwAAAHRpdGxlPeWOn+atjOWQjRAAAABhcnRpc3Q95Y6f5q2M5omLFAAAAGVuY29kZXI9TGF2ZjYxLjcuMTAwgQAAAP/4dAgAAY8kAAAAfV8=",
    ".m4a": "AAAAHGZ0eXBNNEEgAAACAE00QSBpc29taXNvMgAAAAhmcmVlAAAAIW1kYXTeAgBMYXZjNjEuMTkuMTAxAAIwQA4BGCAHAAADRG1vb3YAAABsbXZoZAAAAAAAAAAAAAAAAAAAA+gAAAAyAAEAAAEAAAAAAAAAAAAAAAABAAAAAAAAAAAAAAAAAAAAAQAAAAAAAAAAAAAAAAAAQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAIAAAItdHJhawAAAFx0a2hkAAAAAwAAAAAAAAAAAAAAAQAAAAAAAAAyAAAAAAAAAAAAAAABAQAAAAABAAAAAAAAAAAAAAAAAAAAAQAAAAAAAAAAAAAAAAAAQAAAAAAAAAAAAAAAAAAAJGVkdHMAAAAcZWxzdAAAAAAAAAABAAAAMgAABAAAAQAAAAABpW1kaWEAAAAgbWRoZAAAAAAAAAAAAAAAAAAAH0AAAAWQVcQAAAAAAC1oZGxyAAAAAAAAAABzb3VuAAAAAAAAAAAAAAAAU291bmRIYW5kbGVyAAAAAVBtaW5mAAAAEHNtaGQAAAAAAAAAAAAAACRkaW5mAAAAHGRyZWYAAAAAAAAAAQAAAAx1cmwgAAAAAQAAARRzdGJsAAAAanN0c2QAAAAAAAAAAQAAAFptcDRhAAAAAAAAAAEAAAAAAAAAAAABABAAAAAAH0AAAAAAADZlc2RzAAAAAAOAgIAlAAEABICAgBdAFQAAAAAAu4AAAARjBYCAgAUViFblAAaAgIABAgAAACBzdHRzAAAAAAAAAAIAAAABAAAEAAAAAAEAAAGQAAAAHHN0c2MAAAAAAAAAAQAAAAEAAAACAAAAAQAAABxzdHN6AAAAAAAAAAAAAAACAAAAFQAAAAQAAAAUc3RjbwAAAAAAAAABAAAALAAAABpzZ3BkAQAAAHJvbGwAAAACAAAAAf//AAAAHHNiZ3AAAAAAcm9sbAAAAAEAAAACAAAAAQAAAKN1ZHRhAAAAm21ldGEAAAAAAAAAIWhkbHIAAAAAAAAAAG1kaXJhcHBsAAAAAAAAAAAAAAAAbmlsc3QAAAAhqW5hbQAAABlkYXRhAAAAAQAAAADljp/mrYzlkI0AAAAhqUFSVAAAABlkYXRhAAAAAQAAAADljp/mrYzmiYsAAAAkqXRvbwAAABxkYXRhAAAAAQAAAABMYXZmNjEuNy4xMDA=",
    ".mp3": "SUQzBAAAAAAATFRJVDIAAAALAAAD5Y6f5q2M5ZCNAFRQRTEAAAALAAAD5Y6f5q2M5omLAFRTU0UAAAAOAAADTGF2ZjYxLjcuMTAwAAAAAAAAAAAAAAD/4zjAAAAAAAAAAAAASW5mbwAAAA8AAAADAAABsACqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqrV1dXV1dXV1dXV1dXV1dXV1dXV1dXV1dXV1dXV1dXV1dX///////////////////////////////////////////8AAAAATGF2YzYxLjE5AAAAAAAAAAAAAAAAJALwAAAAAAAAAbD3CmUrAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAD/4xjEAAAAA0gAAAAATEFNRTMuMTAwVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVX/4xjEOwAAA0gAAAAAVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVX/4xjEdgAAA0gAAAAAVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVU=",
}


class SafeMetadataTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        self.root = Path(self.temporary_directory.name).resolve()

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def _audio(self, extension: str) -> Path:
        path = self.root / f"原歌名-原歌手{extension}"
        path.write_bytes(base64.b64decode(_AUDIO_FIXTURES[extension]))
        return path

    def _request(self, path: Path) -> MetadataWriteInput:
        metadata = path.stat()
        return MetadataWriteInput(
            asset_id="asset-1",
            source_path=path,
            allowed_root=self.root,
            expected_size_bytes=metadata.st_size,
            expected_mtime_ns=metadata.st_mtime_ns,
            title="新标题",
            artist="新歌手",
        )

    def _temp_artifacts(self) -> list[Path]:
        return sorted(self.root.glob(".musicctrl-*"))

    def test_three_formats_prepare_apply_and_finalize(self) -> None:
        for extension in (".mp3", ".flac", ".m4a"):
            with self.subTest(extension=extension):
                source = self._audio(extension)
                original = source.read_bytes()
                prepared = prepare_metadata_write(self._request(source))
                self.assertEqual(source.read_bytes(), original)
                self.assertEqual(_read_title_artist(prepared.candidate_path), ("新标题", "新歌手"))
                snapshot = json.loads(prepared.original_metadata_json)
                self.assertEqual(snapshot["title"], ["原歌名"])
                self.assertEqual(snapshot["artist"], ["原歌手"])
                applied = apply_prepared_metadata(prepared)
                self.assertTrue(applied.backup_path.exists())
                self.assertEqual(applied.backup_path.read_bytes(), original)
                self.assertEqual(_read_title_artist(source), ("新标题", "新歌手"))
                finalize_metadata_write(applied)
                self.assertEqual(self._temp_artifacts(), [])
                source.unlink()

    def test_rollback_restores_exact_original_bytes(self) -> None:
        source = self._audio(".mp3")
        original = source.read_bytes()
        applied = apply_prepared_metadata(prepare_metadata_write(self._request(source)))
        rollback_metadata_write(applied)
        self.assertEqual(source.read_bytes(), original)
        self.assertEqual(_read_title_artist(source), ("原歌名", "原歌手"))
        self.assertEqual(self._temp_artifacts(), [])

    def test_prepare_failure_keeps_source_and_cleans_candidate(self) -> None:
        source = self._audio(".flac")
        original = source.read_bytes()
        with patch("services.safe_metadata._write_tags", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                prepare_metadata_write(self._request(source))
        self.assertEqual(source.read_bytes(), original)
        self.assertEqual(self._temp_artifacts(), [])

    def test_unused_prepared_candidate_can_be_safely_discarded(self) -> None:
        source = self._audio(".mp3")
        original = source.read_bytes()
        prepared = prepare_metadata_write(self._request(source))
        discard_prepared_metadata(prepared)
        self.assertEqual(source.read_bytes(), original)
        self.assertEqual(self._temp_artifacts(), [])

    def test_apply_second_rename_failure_restores_source(self) -> None:
        source = self._audio(".m4a")
        original = source.read_bytes()
        prepared = prepare_metadata_write(self._request(source))
        real_rename = os.rename
        calls = 0

        def fail_second(old, new):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise PermissionError("occupied")
            return real_rename(old, new)

        with patch("services.safe_metadata.os.rename", side_effect=fail_second):
            with self.assertRaisesRegex(SafeMetadataError, "已恢复原文件"):
                apply_prepared_metadata(prepared)
        self.assertEqual(source.read_bytes(), original)
        self.assertTrue(prepared.candidate_path.exists())
        prepared.candidate_path.unlink()

    def test_rejects_unsupported_and_changed_sources(self) -> None:
        unsupported = self.root / "a.wav"
        unsupported.write_bytes(b"wave")
        metadata = unsupported.stat()
        request = MetadataWriteInput(
            "a", unsupported, self.root, metadata.st_size, metadata.st_mtime_ns, "t", "a"
        )
        with self.assertRaisesRegex(SafeMetadataError, "只支持"):
            prepare_metadata_write(request)

        source = self._audio(".mp3")
        request = self._request(source)
        source.write_bytes(source.read_bytes() + b"changed")
        with self.assertRaisesRegex(SafeMetadataError, "已变化"):
            prepare_metadata_write(request)
        self.assertEqual(self._temp_artifacts(), [])

    def test_production_module_has_no_hash_copy_library_or_database_access(self) -> None:
        module_path = Path(__file__).resolve().parents[1] / "services" / "safe_metadata.py"
        tree = ast.parse(module_path.read_text(encoding="utf-8"))
        imports = {
            alias.name.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, (ast.Import, ast.ImportFrom))
            for alias in node.names
        }
        self.assertFalse({"hashlib", "shutil", "sqlite3", "repositories", "database"} & imports)
        source = module_path.read_text(encoding="utf-8").casefold()
        self.assertNotIn("os.replace", source)
        self.assertNotIn("shutil.", source)


if __name__ == "__main__":
    unittest.main()
