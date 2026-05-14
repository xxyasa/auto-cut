from __future__ import annotations

import json
import unittest
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from autocut.exporter import export_segments_zip


class SegmentsZipExportTests(unittest.TestCase):
    def test_exports_safe_named_segments_and_manifest(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.mp4"
            source.write_bytes(b"source")
            output = root / "segments.zip"

            def fake_export_clip(_, segment_path, start, end):
                segment_path.write_bytes(f"{start}-{end}".encode("utf-8"))
                return None

            with patch("autocut.exporter.media.export_clip", side_effect=fake_export_clip):
                warning = export_segments_zip(
                    source,
                    output,
                    [
                        {
                            "id": "p001",
                            "start": 1.2,
                            "end": 3.4,
                            "summary": "哈利/波特 .. 片段?",
                            "text": "第一段文案",
                        },
                        {
                            "id": "p002",
                            "start": 5.0,
                            "end": 8.0,
                            "summary": "第二段",
                            "text": "第二段文案",
                        },
                    ],
                )

            self.assertIsNone(warning)
            self.assertTrue(output.exists())
            with zipfile.ZipFile(output) as archive:
                names = archive.namelist()
                self.assertIn("manifest.json", names)
                mp4_names = [name for name in names if name.endswith(".mp4")]
                self.assertEqual(len(mp4_names), 2)
                self.assertTrue(mp4_names[0].startswith("001_00-00-01.200_00-00-03.400_"))
                self.assertNotIn("/", mp4_names[0])
                self.assertNotIn("..", mp4_names[0])

                manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
                self.assertEqual(manifest["schema_version"], 1)
                self.assertEqual(manifest["segment_count"], 2)
                self.assertEqual(manifest["segments"][0]["id"], "p001")
                self.assertEqual(manifest["segments"][0]["text"], "第一段文案")

    def test_failure_removes_incomplete_zip(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.mp4"
            source.write_bytes(b"source")
            output = root / "segments.zip"

            with patch("autocut.exporter.media.export_clip", return_value="boom"):
                warning = export_segments_zip(
                    source,
                    output,
                    [{"start": 1.0, "end": 2.0, "summary": "x"}],
                )

            self.assertEqual(warning, "boom")
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
