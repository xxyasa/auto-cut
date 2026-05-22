import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from autocut import media


class MediaExportTests(unittest.TestCase):
    def test_export_clip_applies_default_audio_gain(self):
        commands: list[list[str]] = []

        def fake_run(args):
            commands.append(args)
            return subprocess.CompletedProcess(args, 0, "", "")

        with (
            patch("autocut.media.executable", return_value="ffmpeg"),
            patch("autocut.media.run_command", side_effect=fake_run),
            patch.dict("os.environ", {}, clear=True),
        ):
            warning = media.export_clip(Path("source.mp4"), Path("clip.mp4"), 1.0, 3.0)

        self.assertIsNone(warning)
        self.assertIn("-filter:a", commands[0])
        filter_index = commands[0].index("-filter:a")
        self.assertEqual(commands[0][filter_index + 1], "volume=20dB")

    def test_export_clip_audio_gain_can_be_disabled(self):
        commands: list[list[str]] = []

        def fake_run(args):
            commands.append(args)
            return subprocess.CompletedProcess(args, 0, "", "")

        with (
            patch("autocut.media.executable", return_value="ffmpeg"),
            patch("autocut.media.run_command", side_effect=fake_run),
            patch.dict("os.environ", {"AUTOCUT_EXPORT_AUDIO_GAIN_DB": "0"}, clear=True),
        ):
            warning = media.export_clip(Path("source.mp4"), Path("clip.mp4"), 1.0, 3.0)

        self.assertIsNone(warning)
        self.assertNotIn("-filter:a", commands[0])


if __name__ == "__main__":
    unittest.main()
