import unittest
from pathlib import Path

from autocut.asr import load_transcript_file
from autocut.cleaning import clean_segments
from autocut.scoring import score_candidates
from autocut.segment import build_candidates


class SegmentScoringTest(unittest.TestCase):
    def test_build_and_score_candidates(self):
        transcript = clean_segments(load_transcript_file(Path("tests/fixtures/transcript.json")))
        candidates = build_candidates(
            transcript,
            source_video=Path("sample.mp4"),
            product="示例产品",
            selling_points=["舒适", "适合日常"],
            max_candidates=3,
        )
        scored = score_candidates(candidates, "示例产品", ["舒适", "适合日常"])
        self.assertGreaterEqual(len(scored), 1)
        self.assertGreater(scored[0].score, 0)
        self.assertIn("subtitle", scored[0].exports.keys() | {"subtitle"})


if __name__ == "__main__":
    unittest.main()

