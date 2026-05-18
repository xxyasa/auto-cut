import os
import unittest

from autocut.llm import LLMConfig, LLMError, _api_urls, _post_bytes_with_retries, parse_model_json


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

    def test_http_fallback_requires_explicit_opt_in(self):
        old_fallback = os.environ.get("AUTOCUT_LLM_HTTP_FALLBACK")
        try:
            os.environ.pop("AUTOCUT_LLM_HTTP_FALLBACK", None)
            self.assertEqual(
                _api_urls("https://example.test/v1/chat/completions"),
                [("https", "https://example.test/v1/chat/completions")],
            )

            os.environ["AUTOCUT_LLM_HTTP_FALLBACK"] = "1"
            self.assertEqual(
                _api_urls("https://example.test/v1/chat/completions"),
                [
                    ("https", "https://example.test/v1/chat/completions"),
                    ("http fallback", "http://example.test/v1/chat/completions"),
                ],
            )
        finally:
            if old_fallback is None:
                os.environ.pop("AUTOCUT_LLM_HTTP_FALLBACK", None)
            else:
                os.environ["AUTOCUT_LLM_HTTP_FALLBACK"] = old_fallback

    def test_transport_error_mentions_http_fallback(self):
        import autocut.llm as llm

        old_api_urls = llm._api_urls
        old_post_bytes = llm._post_bytes
        llm._api_urls = lambda api_url: [
            ("https", "https://example.test/v1/chat/completions"),
            ("http fallback", "http://example.test/v1/chat/completions"),
        ]

        def fake_post_bytes(
            config,
            payload,
            *,
            api_url,
            tls12_max=False,
            verify=True,
            use_ssl_context=False,
        ):
            if api_url.startswith("https://"):
                raise LLMError("[SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred")
            raise LLMError("model api http 502: bad gateway")

        llm._post_bytes = fake_post_bytes
        try:
            with self.assertRaises(LLMError) as ctx:
                _post_bytes_with_retries(
                    LLMConfig(
                        api_url="https://example.test/v1/chat/completions",
                        api_key="sk-test",
                    ),
                    {"model": "x"},
                )
            message = str(ctx.exception)
            self.assertIn("https", message)
            self.assertIn("http fallback", message)
            self.assertIn("502", message)
        finally:
            llm._api_urls = old_api_urls
            llm._post_bytes = old_post_bytes

    def test_first_https_attempt_uses_original_urlopen_shape(self):
        import autocut.llm as llm

        old_api_urls = llm._api_urls
        old_post_bytes = llm._post_bytes
        calls = []
        llm._api_urls = lambda api_url: [("https", api_url)]

        def fake_post_bytes(
            config,
            payload,
            *,
            api_url,
            tls12_max=False,
            verify=True,
            use_ssl_context=False,
        ):
            calls.append(
                {
                    "api_url": api_url,
                    "tls12_max": tls12_max,
                    "verify": verify,
                    "use_ssl_context": use_ssl_context,
                }
            )
            return b'{"choices":[{"message":{"content":"ok"}}]}'

        llm._post_bytes = fake_post_bytes
        try:
            data = _post_bytes_with_retries(
                LLMConfig(
                    api_url="https://example.test/v1/chat/completions",
                    api_key="sk-test",
                ),
                {"model": "x"},
            )
        finally:
            llm._api_urls = old_api_urls
            llm._post_bytes = old_post_bytes

        self.assertEqual(data, b'{"choices":[{"message":{"content":"ok"}}]}')
        self.assertEqual(calls[0]["use_ssl_context"], False)
        self.assertEqual(calls[0]["tls12_max"], False)


if __name__ == "__main__":
    unittest.main()
