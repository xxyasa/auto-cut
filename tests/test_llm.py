import unittest

from autocut.llm import LLMError, parse_model_json


class LLMTests(unittest.TestCase):
    def test_parse_plain_json(self):
        parsed = parse_model_json('{"ordered_ids":["s002","s001"],"reason":"先钩子"}')

        self.assertEqual(parsed["ordered_ids"], ["s002", "s001"])

    def test_parse_fenced_json(self):
        parsed = parse_model_json('```json\n{"ordered_ids":["s003"],"reason":"卖点"}\n```')

        self.assertEqual(parsed["ordered_ids"], ["s003"])

    def test_parse_embedded_json(self):
        parsed = parse_model_json('好的，方案如下：{"ordered_ids":["s004"],"reason":"转化"}')

        self.assertEqual(parsed["reason"], "转化")

    def test_parse_invalid_json(self):
        with self.assertRaises(LLMError):
            parse_model_json("没有 JSON")


if __name__ == "__main__":
    unittest.main()
