import unittest

from autocut.api import _build_full_timeline, _operational_phrase_disabled_sources


class TimelineApiTests(unittest.TestCase):
    def test_leading_sentence_at_zero_is_disabled_when_no_opening_cue(self):
        result = {
            "media": {"duration": 12, "path": "input.mp4"},
            "transcript": [
                {
                    "start": 0.0,
                    "end": 5.62,
                    "text": "赫奇帕奇的雨伞是一个非常明媚非常活泼的红色",
                    "words": [
                        {"start": 0.0, "end": 0.22, "word": "赫"},
                        {"start": 0.22, "end": 0.38, "word": "奇"},
                        {"start": 0.38, "end": 0.54, "word": "帕"},
                        {"start": 0.54, "end": 0.76, "word": "奇"},
                        {"start": 0.76, "end": 0.98, "word": "的"},
                        {"start": 1.04, "end": 2.30, "word": "雨伞"},
                        {"start": 2.54, "end": 2.94, "word": "是"},
                    ],
                }
            ],
            "candidates": [],
        }

        timeline = _build_full_timeline(result, {})
        leading = [piece for piece in timeline["pieces"] if piece["start"] == 0.0]

        self.assertTrue(leading)
        self.assertTrue(all(piece["kind"] == "leading_incomplete" for piece in leading))
        self.assertTrue(all(not piece["enabled"] for piece in leading))
        self.assertTrue(all(piece["reason"] == "片头疑似半句话，默认不播放" for piece in leading))

    def test_leading_sentence_with_opening_cue_is_kept(self):
        result = {
            "media": {"duration": 8, "path": "input.mp4"},
            "transcript": [
                {
                    "start": 0.0,
                    "end": 4.0,
                    "text": "大家好今天给大家介绍这款透明伞",
                    "words": [
                        {"start": 0.0, "end": 0.42, "word": "大家好"},
                        {"start": 0.42, "end": 0.80, "word": "今天"},
                        {"start": 0.80, "end": 1.12, "word": "给大家"},
                        {"start": 1.12, "end": 1.60, "word": "介绍"},
                    ],
                }
            ],
            "candidates": [],
        }

        timeline = _build_full_timeline(result, {})
        first_piece = timeline["pieces"][0]

        self.assertTrue(first_piece["enabled"])
        self.assertEqual(first_piece["kind"], "content")

    def test_operational_phrase_uses_word_timestamps(self):
        transcript = [
            {
                "start": 64.28,
                "end": 90.84,
                "text": "来辛苦我们的后台务实看一下大屏幕啊来我们这样子一个中文雨伞",
                "words": [
                    {"start": 67.52, "end": 67.82, "word": "来"},
                    {"start": 67.82, "end": 68.06, "word": "辛苦"},
                    {"start": 68.06, "end": 68.30, "word": "我们"},
                    {"start": 68.30, "end": 68.40, "word": "的"},
                    {"start": 68.40, "end": 68.46, "word": "后"},
                    {"start": 68.46, "end": 68.56, "word": "台"},
                    {"start": 68.56, "end": 68.66, "word": "务"},
                    {"start": 68.66, "end": 68.74, "word": "实"},
                    {"start": 68.74, "end": 68.92, "word": "看一下"},
                    {"start": 68.92, "end": 69.28, "word": "大"},
                    {"start": 69.28, "end": 69.44, "word": "屏"},
                    {"start": 69.44, "end": 69.68, "word": "幕"},
                    {"start": 69.68, "end": 69.90, "word": "啊"},
                    {"start": 69.90, "end": 70.12, "word": "来"},
                ],
            }
        ]

        sources = _operational_phrase_disabled_sources(transcript)

        self.assertEqual(len(sources), 1)
        self.assertEqual(sources[0]["kind"], "irrelevant_topic")
        self.assertGreaterEqual(sources[0]["start"], 67.75)
        self.assertLessEqual(sources[0]["start"], 67.85)
        self.assertGreaterEqual(sources[0]["end"], 69.70)
        self.assertLessEqual(sources[0]["end"], 69.80)
        self.assertIn("后台", sources[0]["terms"][0]["text"])

    def test_full_timeline_marks_operational_phrase_disabled(self):
        result = {
            "media": {"duration": 75, "path": "input.mp4"},
            "transcript": [
                {
                    "start": 64.28,
                    "end": 70.12,
                    "text": "来辛苦我们的后台务实看一下大屏幕啊来我们这样子一个中文雨伞",
                    "words": [
                        {"start": 67.82, "end": 68.06, "word": "辛苦"},
                        {"start": 68.06, "end": 68.30, "word": "我们"},
                        {"start": 68.30, "end": 68.40, "word": "的"},
                        {"start": 68.40, "end": 68.46, "word": "后"},
                        {"start": 68.46, "end": 68.56, "word": "台"},
                        {"start": 68.56, "end": 68.66, "word": "务"},
                        {"start": 68.66, "end": 68.74, "word": "实"},
                        {"start": 68.74, "end": 68.92, "word": "看一下"},
                        {"start": 68.92, "end": 69.28, "word": "大"},
                        {"start": 69.28, "end": 69.44, "word": "屏"},
                        {"start": 69.44, "end": 69.68, "word": "幕"},
                    ],
                }
            ],
            "candidates": [],
        }

        timeline = _build_full_timeline(result, {})
        disabled = [
            piece
            for piece in timeline["pieces"]
            if piece["start"] < 69.72 and piece["end"] > 67.78 and piece["kind"] == "irrelevant_topic"
        ]

        self.assertTrue(disabled)
        self.assertTrue(all(not piece["enabled"] for piece in disabled))
        self.assertTrue(all(piece["reason"] == "与产品介绍无关，默认不播放" for piece in disabled))


if __name__ == "__main__":
    unittest.main()
