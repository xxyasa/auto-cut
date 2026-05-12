import unittest
from pathlib import Path

from autocut.jumpcut import build_compact_plan
from autocut.models import CandidateClip, TranscriptSegment


class JumpCutTest(unittest.TestCase):
    def test_build_compact_plan_removes_short_fillers(self):
        candidate = CandidateClip(
            clip_id="clip_001",
            source_video=Path("sample.mp4"),
            start_time=0,
            end_time=5,
            transcript="这个伞啊真的很好",
            clean_transcript="伞真的很好",
            segment_indexes=[0],
        )
        segment = TranscriptSegment(
            start=0,
            end=5,
            text="这个伞啊真的很好",
            words=[
                {"start": 0.1, "end": 0.4, "word": "这个"},
                {"start": 0.5, "end": 0.8, "word": "伞"},
                {"start": 0.9, "end": 1.2, "word": "啊"},
                {"start": 2.0, "end": 2.5, "word": "真的"},
                {"start": 2.6, "end": 3.0, "word": "很好"},
            ],
        )

        plan = build_compact_plan(candidate, [segment], padding=0.05, merge_gap=0.3)

        self.assertIsNotNone(plan)
        assert plan is not None
        self.assertGreater(plan.removed_duration, 0.5)
        self.assertTrue(any(item["text"] == "啊" for item in plan.removed_terms))
        self.assertLess(plan.compact_duration, candidate.duration)

    def test_build_compact_plan_removes_live_filler_phrases(self):
        candidate = CandidateClip(
            clip_id="clip_002",
            source_video=Path("sample.mp4"),
            start_time=0,
            end_time=6,
            transcript="伞面可以来看一看学院图案",
            clean_transcript="伞面学院图案",
            segment_indexes=[0],
        )
        segment = TranscriptSegment(
            start=0,
            end=6,
            text="伞面可以来看一看学院图案",
            words=[
                {"start": 0.1, "end": 0.4, "word": "伞面"},
                {"start": 0.5, "end": 0.8, "word": "可以"},
                {"start": 0.8, "end": 1.2, "word": "来看"},
                {"start": 1.2, "end": 1.6, "word": "一看"},
                {"start": 2.0, "end": 2.5, "word": "学院"},
                {"start": 2.5, "end": 3.0, "word": "图案"},
            ],
        )

        plan = build_compact_plan(candidate, [segment], padding=0.05, merge_gap=0.3)

        self.assertIsNotNone(plan)
        assert plan is not None
        self.assertTrue(any(item["text"] == "可以来看一看" for item in plan.removed_terms))
        self.assertTrue(any(item.get("kind") == "live_filler_phrase" for item in plan.removed_terms))

    def test_build_compact_plan_removes_cough_words(self):
        candidate = CandidateClip(
            clip_id="clip_003",
            source_video=Path("sample.mp4"),
            start_time=0,
            end_time=2,
            transcript="伞咳咳很好",
            clean_transcript="伞很好",
            segment_indexes=[0],
        )
        segment = TranscriptSegment(
            start=0,
            end=2,
            text="伞咳咳很好",
            words=[
                {"start": 0.1, "end": 0.4, "word": "伞"},
                {"start": 0.5, "end": 0.7, "word": "咳咳"},
                {"start": 0.9, "end": 1.2, "word": "很好"},
            ],
        )

        plan = build_compact_plan(candidate, [segment], padding=0.05, merge_gap=0.3)

        self.assertIsNotNone(plan)
        assert plan is not None
        self.assertTrue(any(item.get("kind") == "cough" for item in plan.removed_terms))

    def test_build_compact_plan_removes_orphan_what_phrase(self):
        candidate = CandidateClip(
            clip_id="clip_004",
            source_video=Path("sample.mp4"),
            start_time=0,
            end_time=4,
            transcript="什么呢这个伞面很好",
            clean_transcript="伞面很好",
            segment_indexes=[0],
        )
        segment = TranscriptSegment(
            start=0,
            end=4,
            text="什么呢这个伞面很好",
            words=[
                {"start": 0.1, "end": 0.7, "word": "什么呢"},
                {"start": 0.8, "end": 1.1, "word": "这个"},
                {"start": 1.2, "end": 1.6, "word": "伞面"},
                {"start": 1.7, "end": 2.0, "word": "很好"},
            ],
        )

        plan = build_compact_plan(candidate, [segment], padding=0.05, merge_gap=0.3)

        self.assertIsNotNone(plan)
        assert plan is not None
        self.assertTrue(any(item["text"] == "什么呢" for item in plan.removed_terms))

    def test_build_compact_plan_keeps_semantic_what_question(self):
        candidate = CandidateClip(
            clip_id="clip_005",
            source_video=Path("sample.mp4"),
            start_time=0,
            end_time=4,
            transcript="为什么呢因为伞骨加固",
            clean_transcript="为什么呢因为伞骨加固",
            segment_indexes=[0],
        )
        segment = TranscriptSegment(
            start=0,
            end=4,
            text="为什么呢因为伞骨加固",
            words=[
                {"start": 0.1, "end": 0.3, "word": "为"},
                {"start": 0.3, "end": 0.8, "word": "什么呢"},
                {"start": 0.9, "end": 1.2, "word": "因为"},
                {"start": 1.3, "end": 1.7, "word": "伞骨"},
                {"start": 1.8, "end": 2.2, "word": "加固"},
            ],
        )

        plan = build_compact_plan(candidate, [segment], padding=0.05, merge_gap=0.3)

        self.assertIsNotNone(plan)
        assert plan is not None
        self.assertFalse(any(item["text"] == "什么呢" for item in plan.removed_terms))


if __name__ == "__main__":
    unittest.main()
