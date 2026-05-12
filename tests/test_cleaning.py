import unittest

from autocut.cleaning import clean_text


class CleaningTest(unittest.TestCase):
    def test_clean_text_marks_fillers(self):
        clean, filler_ratio, repeat_ratio, reasons = clean_text("嗯这个这个产品啊真的适合日常使用")
        self.assertIn("产品", clean)
        self.assertGreater(filler_ratio, 0)
        self.assertGreaterEqual(repeat_ratio, 0)
        self.assertIsInstance(reasons, list)

    def test_clean_text_removes_live_filler_phrases(self):
        clean, filler_ratio, _, _ = clean_text("这样的一个伞面给大家去看一下它的学院图案这样子的等等")
        self.assertIn("伞面", clean)
        self.assertIn("学院图案", clean)
        self.assertNotIn("这样的一个", clean)
        self.assertNotIn("给大家去看一下", clean)
        self.assertNotIn("这样子的", clean)
        self.assertNotIn("等等", clean)
        self.assertGreater(filler_ratio, 0)

    def test_clean_text_removes_orphan_what_phrase_but_keeps_question(self):
        clean, _, _, _ = clean_text("什么呢这个伞面是磨砂透明的")
        self.assertNotIn("什么呢", clean)

        question, _, _, _ = clean_text("为什么呢因为它是加厚伞布")
        self.assertIn("为什么呢", question)


if __name__ == "__main__":
    unittest.main()
