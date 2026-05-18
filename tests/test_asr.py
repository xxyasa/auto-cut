import os
from pathlib import Path
import unittest

from autocut.asr import WhisperApiEngine, create_asr_engine


class WhisperApiEngineTests(unittest.TestCase):
    def test_parse_verbose_json_segments_and_words(self):
        segments = WhisperApiEngine._segments_from_verbose_json(
            {
                "text": "你好世界",
                "segments": [
                    {"start": 0.0, "end": 1.0, "text": "你好"},
                    {"start": 1.0, "end": 2.0, "text": "世界"},
                ],
                "words": [
                    {"start": 0.1, "end": 0.4, "word": "你"},
                    {"start": 0.4, "end": 0.8, "word": "好"},
                    {"start": 1.1, "end": 1.5, "word": "世界"},
                ],
            }
        )

        self.assertEqual(len(segments), 2)
        self.assertEqual(segments[0].text, "你好")
        self.assertEqual(segments[0].words[0]["word"], "你")
        self.assertEqual(segments[1].start, 1.0)

    def test_create_remote_whisper_engine(self):
        engine = create_asr_engine("whisper-api", model="whisper-large-v3")

        self.assertIsInstance(engine, WhisperApiEngine)
        self.assertEqual(engine.model, "whisper-large-v3")

    def test_remote_whisper_maps_local_small_default_to_large(self):
        engine = create_asr_engine("whisper-api", model="small")

        self.assertEqual(engine.model, "whisper-large-v3")

    def test_transcribe_sends_unquoted_multipart_fields(self):
        captured = {}
        engine = WhisperApiEngine(model="whisper-large-v3")

        def fake_post_file(audio_path, fields):
            captured.update(fields)
            return {"text": "ok"}

        engine._post_file = fake_post_file
        old_upload_format = os.environ.get("AUTOCUT_ASR_UPLOAD_FORMAT")
        old_send_language = os.environ.get("AUTOCUT_ASR_SEND_LANGUAGE")
        old_send_prompt = os.environ.get("AUTOCUT_ASR_SEND_PROMPT")
        os.environ["AUTOCUT_ASR_UPLOAD_FORMAT"] = "source"
        os.environ.pop("AUTOCUT_ASR_SEND_LANGUAGE", None)
        os.environ.pop("AUTOCUT_ASR_SEND_PROMPT", None)
        try:
            engine.transcribe(Path("tests/fixtures/sample.mp4"))
        finally:
            if old_upload_format is None:
                os.environ.pop("AUTOCUT_ASR_UPLOAD_FORMAT", None)
            else:
                os.environ["AUTOCUT_ASR_UPLOAD_FORMAT"] = old_upload_format
            if old_send_language is None:
                os.environ.pop("AUTOCUT_ASR_SEND_LANGUAGE", None)
            else:
                os.environ["AUTOCUT_ASR_SEND_LANGUAGE"] = old_send_language
            if old_send_prompt is None:
                os.environ.pop("AUTOCUT_ASR_SEND_PROMPT", None)
            else:
                os.environ["AUTOCUT_ASR_SEND_PROMPT"] = old_send_prompt

        self.assertEqual(captured["model"], "whisper-large-v3")
        self.assertEqual(captured["response_format"], "verbose_json")
        self.assertEqual(captured["timestamp_granularities"], '["word","segment"]')
        self.assertNotIn("language", captured)
        self.assertNotIn("prompt", captured)

    def test_curl_form_values_match_verified_command_shape(self):
        self.assertEqual(
            WhisperApiEngine._curl_form_value("model", "whisper-large-v3"),
            '"whisper-large-v3"',
        )
        self.assertEqual(
            WhisperApiEngine._curl_form_value("response_format", "verbose_json"),
            '"verbose_json"',
        )
        self.assertEqual(
            WhisperApiEngine._curl_form_value("timestamp_granularities", '["word","segment"]'),
            '"[\\"word\\",\\"segment\\"]"',
        )

    def test_curl_tls_failure_falls_back_to_urllib(self):
        engine = WhisperApiEngine(model="whisper-large-v3")
        engine._api_urls = lambda: [("https", "https://example.test/v1/audio/transcriptions")]
        calls = []

        def fake_httpx(audio_path, fields, api_url, tls12_max=False):
            raise ImportError()

        def fake_curl(audio_path, fields, api_url=None):
            calls.append("curl")
            raise RuntimeError(
                "Whisper API transcription failed via curl: exit 35 "
                "curl: (35) schannel: failed to receive handshake, "
                "SSL/TLS connection failed"
            )

        def fake_urllib(audio_path, fields, api_url=None, verify=None, tls12_max=False):
            calls.append("urllib")
            return {"text": "ok"}

        engine._post_file_httpx = fake_httpx
        engine._post_file_curl = fake_curl
        engine._post_file_requests_with_retries = (
            lambda audio_path, fields, api_url: (_ for _ in ()).throw(
                ImportError("requests unavailable")
            )
        )
        engine._post_file_urllib = fake_urllib

        self.assertEqual(engine._post_file(Path("sample.mp3"), {})["text"], "ok")
        self.assertEqual(calls, ["curl", "urllib"])

    def test_urllib_retries_tls12_then_insecure_on_eof(self):
        engine = WhisperApiEngine(model="whisper-large-v3")
        calls = []

        def fake_urllib(audio_path, fields, api_url=None, verify=None, tls12_max=False):
            calls.append((verify, tls12_max))
            if len(calls) < 3:
                raise RuntimeError(
                    "Whisper API transcription failed: <urlopen error "
                    "[SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred in "
                    "violation of protocol>"
                )
            return {"text": "ok"}

        engine._post_file_urllib = fake_urllib

        self.assertEqual(
            engine._post_file_urllib_with_tls_retries(
                Path("sample.mp3"),
                {},
                api_url="https://example.test/v1/audio/transcriptions",
            )["text"],
            "ok",
        )
        self.assertEqual(calls, [(True, False), (True, True), (False, True)])

    def test_urllib_retries_python313_tls_closed_eof(self):
        engine = WhisperApiEngine(model="whisper-large-v3")
        calls = []

        def fake_urllib(audio_path, fields, api_url=None, verify=None, tls12_max=False):
            calls.append((verify, tls12_max))
            if len(calls) < 2:
                raise RuntimeError(
                    "Whisper API transcription failed: <urlopen error "
                    "TLS/SSL connection has been closed (EOF) (_ssl.c:1028)>"
                )
            return {"text": "ok"}

        engine._post_file_urllib = fake_urllib

        self.assertEqual(
            engine._post_file_urllib_with_tls_retries(
                Path("sample.mp3"),
                {},
                api_url="https://example.test/v1/audio/transcriptions",
            )["text"],
            "ok",
        )
        self.assertEqual(calls, [(True, False), (True, True)])

    def test_http_fallback_requires_explicit_opt_in(self):
        engine = WhisperApiEngine(model="whisper-large-v3")
        old_url = os.environ.get("AUTOCUT_ASR_API_URL")
        old_fallback = os.environ.get("AUTOCUT_ASR_HTTP_FALLBACK")
        os.environ["AUTOCUT_ASR_API_URL"] = "https://example.test/v1/audio/transcriptions"
        try:
            os.environ.pop("AUTOCUT_ASR_HTTP_FALLBACK", None)
            self.assertEqual(
                engine._api_urls(),
                [("https", "https://example.test/v1/audio/transcriptions")],
            )

            os.environ["AUTOCUT_ASR_HTTP_FALLBACK"] = "1"
            self.assertEqual(
                engine._api_urls(),
                [
                    ("https", "https://example.test/v1/audio/transcriptions"),
                    ("http fallback", "http://example.test/v1/audio/transcriptions"),
                ],
            )
        finally:
            if old_url is None:
                os.environ.pop("AUTOCUT_ASR_API_URL", None)
            else:
                os.environ["AUTOCUT_ASR_API_URL"] = old_url
            if old_fallback is None:
                os.environ.pop("AUTOCUT_ASR_HTTP_FALLBACK", None)
            else:
                os.environ["AUTOCUT_ASR_HTTP_FALLBACK"] = old_fallback

    def test_http_api_url_is_labeled_http(self):
        engine = WhisperApiEngine(model="whisper-large-v3")
        old_url = os.environ.get("AUTOCUT_ASR_API_URL")
        old_fallback = os.environ.get("AUTOCUT_ASR_HTTP_FALLBACK")
        os.environ["AUTOCUT_ASR_API_URL"] = "http://example.test/v1/audio/transcriptions"
        os.environ["AUTOCUT_ASR_HTTP_FALLBACK"] = "1"
        try:
            self.assertEqual(
                engine._api_urls(),
                [("http", "http://example.test/v1/audio/transcriptions")],
            )
        finally:
            if old_url is None:
                os.environ.pop("AUTOCUT_ASR_API_URL", None)
            else:
                os.environ["AUTOCUT_ASR_API_URL"] = old_url
            if old_fallback is None:
                os.environ.pop("AUTOCUT_ASR_HTTP_FALLBACK", None)
            else:
                os.environ["AUTOCUT_ASR_HTTP_FALLBACK"] = old_fallback

    def test_http_fallback_502_error_mentions_fallback(self):
        engine = WhisperApiEngine(model="whisper-large-v3")
        engine._api_urls = lambda: [
            ("https", "https://example.test/v1/audio/transcriptions"),
            ("http fallback", "http://example.test/v1/audio/transcriptions"),
        ]

        def fake_post_to_url(audio_path, fields, api_url, url_label):
            if url_label == "https":
                raise RuntimeError(
                    "Whisper API transcription failed after transport retries: "
                    "httpx: [SSL: UNEXPECTED_EOF_WHILE_READING]"
                )
            raise RuntimeError("Whisper API transcription failed: HTTP 502 ")

        engine._post_file_to_url = fake_post_to_url

        with self.assertRaises(RuntimeError) as ctx:
            engine._post_file(Path("sample.mp3"), {})

        message = str(ctx.exception)
        self.assertIn("https", message)
        self.assertIn("http fallback", message)
        self.assertIn("HTTP 502", message)


if __name__ == "__main__":
    unittest.main()
