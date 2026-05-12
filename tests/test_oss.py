"""oss.py 单元测试（提案 §A 验收）。

不接真实网络。所有 httpx 行为通过 MockTransport 注入。
SSRF DNS 行为通过 monkeypatch `_resolve_ips` 注入。
"""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import httpx

from autocut import oss


# ---------- 工具 ----------


def _public_ip_resolver(host: str) -> list[str]:
    # 1.1.1.1 是真正的 is_global IP；TEST-NET-3 在 Python 里被 is_reserved=True 拦截。
    return ["1.1.1.1"]


def _private_ip_resolver(host: str) -> list[str]:
    return ["127.0.0.1"]


def _build_client(handler, real_client_cls):
    return real_client_cls(
        transport=httpx.MockTransport(handler),
        timeout=httpx.Timeout(10.0),
        follow_redirects=False,
    )


class _ClientFactory:
    """patch _httpx() 返回的 module 的 Client 类，让它用 MockTransport。"""

    def __init__(self, handler):
        self.handler = handler

    def __enter__(self):
        real_httpx = oss._httpx()
        real_client_cls = real_httpx.Client  # 保留真实构造，避免递归

        def fake_client(*args, **kwargs):
            return _build_client(self.handler, real_client_cls)

        self._patch = patch.object(real_httpx, "Client", fake_client)
        self._patch.start()
        return self

    def __exit__(self, *exc):
        self._patch.stop()


# ---------- URL validate ----------


class UrlValidationTests(unittest.TestCase):
    def setUp(self):
        self._env = patch.dict("os.environ", {oss.ENV_URL_ALLOWLIST: "oss.ecmax.cn"})
        self._env.start()
        self._dns = patch.object(oss, "_resolve_ips", _public_ip_resolver)
        self._dns.start()

    def tearDown(self):
        self._dns.stop()
        self._env.stop()

    def test_valid_https_passes(self):
        host, url = oss._validate_https_url("https://oss.ecmax.cn/rpa/x.mp4")
        self.assertEqual(host, "oss.ecmax.cn")
        self.assertEqual(url, "https://oss.ecmax.cn/rpa/x.mp4")

    def test_http_scheme_rejected(self):
        with self.assertRaises(oss.SchemeNotAllowed):
            oss._validate_https_url("http://oss.ecmax.cn/rpa/x.mp4")

    def test_ftp_scheme_rejected(self):
        with self.assertRaises(oss.SchemeNotAllowed):
            oss._validate_https_url("ftp://oss.ecmax.cn/rpa/x.mp4")

    def test_bad_extension_rejected(self):
        with self.assertRaises(oss.ExtensionNotAllowed):
            oss._validate_https_url("https://oss.ecmax.cn/rpa/x.exe")

    def test_no_extension_rejected(self):
        with self.assertRaises(oss.ExtensionNotAllowed):
            oss._validate_https_url("https://oss.ecmax.cn/rpa/x")

    def test_unknown_host_rejected(self):
        with self.assertRaises(oss.HostNotAllowed):
            oss._validate_https_url("https://evil.example.com/x.mp4")

    def test_all_video_extensions_pass(self):
        for ext in [".mp4", ".mov", ".mkv", ".webm", ".flv", ".ts"]:
            host, url = oss._validate_https_url(f"https://oss.ecmax.cn/rpa/x{ext}")
            self.assertEqual(host, "oss.ecmax.cn")


class OssUriValidationTests(unittest.TestCase):
    def setUp(self):
        self._env = patch.dict(
            "os.environ",
            {oss.ENV_OSS_BUCKET: "rpa", oss.ENV_OSS_ENDPOINT: "oss.ecmax.cn"},
        )
        self._env.start()

    def tearDown(self):
        self._env.stop()

    def test_valid_oss(self):
        bucket, key = oss._validate_oss_uri("oss://rpa/folder/x.mp4")
        self.assertEqual(bucket, "rpa")
        self.assertEqual(key, "folder/x.mp4")

    def test_wrong_scheme(self):
        with self.assertRaises(oss.SchemeNotAllowed):
            oss._validate_oss_uri("s3://rpa/x.mp4")

    def test_wrong_bucket(self):
        with self.assertRaises(oss.HostNotAllowed):
            oss._validate_oss_uri("oss://other-bucket/x.mp4")

    def test_missing_key(self):
        with self.assertRaises(oss.InvalidSource):
            oss._validate_oss_uri("oss://rpa/")


# ---------- SSRF ----------


class SsrfTests(unittest.TestCase):
    def setUp(self):
        self._env = patch.dict("os.environ", {oss.ENV_URL_ALLOWLIST: "oss.ecmax.cn"})
        self._env.start()

    def tearDown(self):
        self._env.stop()

    def test_loopback_ipv4_blocked(self):
        with patch.object(oss, "_resolve_ips", lambda h: ["127.0.0.1"]):
            with self.assertRaises(oss.PrivateAddressBlocked):
                oss._validate_https_url("https://oss.ecmax.cn/rpa/x.mp4")

    def test_rfc1918_blocked(self):
        for ip in ["10.0.0.1", "172.16.0.1", "192.168.1.1"]:
            with patch.object(oss, "_resolve_ips", lambda h, _ip=ip: [_ip]):
                with self.assertRaises(oss.PrivateAddressBlocked):
                    oss._validate_https_url("https://oss.ecmax.cn/rpa/x.mp4")

    def test_link_local_blocked(self):
        with patch.object(oss, "_resolve_ips", lambda h: ["169.254.169.254"]):
            with self.assertRaises(oss.PrivateAddressBlocked):
                oss._validate_https_url("https://oss.ecmax.cn/rpa/x.mp4")

    def test_ipv6_loopback_blocked(self):
        with patch.object(oss, "_resolve_ips", lambda h: ["::1"]):
            with self.assertRaises(oss.PrivateAddressBlocked):
                oss._validate_https_url("https://oss.ecmax.cn/rpa/x.mp4")

    def test_public_ip_passes(self):
        with patch.object(oss, "_resolve_ips", _public_ip_resolver):
            oss._validate_https_url("https://oss.ecmax.cn/rpa/x.mp4")


# ---------- HEAD 预检 ----------


class HeadCheckTests(unittest.TestCase):
    def setUp(self):
        self._env = patch.dict("os.environ", {oss.ENV_URL_ALLOWLIST: "oss.ecmax.cn"})
        self._env.start()
        self._dns = patch.object(oss, "_resolve_ips", _public_ip_resolver)
        self._dns.start()

    def tearDown(self):
        self._dns.stop()
        self._env.stop()

    def _run_head(self, handler):
        with _ClientFactory(handler):
            return oss._head_check("https://oss.ecmax.cn/rpa/x.mp4")

    def test_200_with_size_under_limit(self):
        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, headers={"content-length": "1024"})

        self.assertEqual(self._run_head(handler), 1024)

    def test_200_without_content_length(self):
        def handler(req):
            return httpx.Response(200)

        self.assertIsNone(self._run_head(handler))

    def test_302_rejected(self):
        def handler(req):
            return httpx.Response(302, headers={"location": "https://x"})

        with self.assertRaises(oss.RedirectNotAllowed):
            self._run_head(handler)

    def test_404_upstream(self):
        def handler(req):
            return httpx.Response(404)

        with self.assertRaises(oss.UpstreamError):
            self._run_head(handler)

    def test_too_large_declared(self):
        def handler(req):
            return httpx.Response(
                200, headers={"content-length": str(3 * 1024 * 1024 * 1024)}
            )

        with self.assertRaises(oss.TooLarge):
            self._run_head(handler)


# ---------- 流式下载 ----------


class StreamDownloadTests(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.dest = Path(self._tmp.name) / "source.mp4"
        self._env = patch.dict("os.environ", {oss.ENV_URL_ALLOWLIST: "oss.ecmax.cn"})
        self._env.start()
        self._dns = patch.object(oss, "_resolve_ips", _public_ip_resolver)
        self._dns.start()

    def tearDown(self):
        self._dns.stop()
        self._env.stop()
        self._tmp.cleanup()

    def _run(self, handler):
        with _ClientFactory(handler):
            return oss._stream_download(
                "https://oss.ecmax.cn/rpa/x.mp4", self.dest
            )

    def test_simple_200(self):
        payload = b"FAKEMP4" * 10

        def handler(req):
            return httpx.Response(200, content=payload)

        n = self._run(handler)
        self.assertEqual(n, len(payload))
        self.assertEqual(self.dest.read_bytes(), payload)

    def test_redirect_during_get_rejected(self):
        def handler(req):
            return httpx.Response(301, headers={"location": "https://x"})

        with self.assertRaises(oss.RedirectNotAllowed):
            self._run(handler)
        self.assertFalse(self.dest.exists(), "残留半截文件")

    def test_streamed_overflow_aborts(self):
        # 模拟分段流；每片 1 MiB，写到第 2049 片就该触发 2GB
        # 实际我们直接构造 > 2GB 的单条响应不现实，改成把 MAX 调小
        with patch.object(oss, "MAX_VIDEO_BYTES", 16):
            payload = b"x" * 64

            def handler(req):
                return httpx.Response(200, content=payload)

            with self.assertRaises(oss.TooLarge):
                self._run(handler)
            self.assertFalse(self.dest.exists())

    def test_4xx_during_get(self):
        def handler(req):
            return httpx.Response(404)

        with self.assertRaises(oss.UpstreamError):
            self._run(handler)
        self.assertFalse(self.dest.exists())


# ---------- resolve_video_source ----------


class ResolveSourceTests(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self._env = patch.dict(
            "os.environ",
            {
                oss.ENV_URL_ALLOWLIST: "oss.ecmax.cn",
                oss.ENV_OSS_BUCKET: "rpa",
                oss.ENV_OSS_ENDPOINT: "oss.ecmax.cn",
            },
        )
        self._env.start()
        self._dns = patch.object(oss, "_resolve_ips", _public_ip_resolver)
        self._dns.start()

    def tearDown(self):
        self._dns.stop()
        self._env.stop()
        self._tmp.cleanup()

    def test_upload_passthrough(self):
        src = self.tmp / "uploaded.mp4"
        src.write_bytes(b"FAKE")
        result = oss.resolve_video_source(
            {"type": "upload", "uploaded_path": str(src)},
            self.tmp / "dest",
        )
        self.assertEqual(result, src)

    def test_upload_missing_path(self):
        with self.assertRaises(oss.InvalidSource):
            oss.resolve_video_source({"type": "upload"}, self.tmp / "dest")

    def test_upload_extension_rejected(self):
        src = self.tmp / "x.exe"
        src.write_bytes(b"FAKE")
        with self.assertRaises(oss.ExtensionNotAllowed):
            oss.resolve_video_source(
                {"type": "upload", "uploaded_path": str(src)}, self.tmp / "dest"
            )

    def test_url_full_flow(self):
        payload = b"VIDEO-DATA"

        def handler(req):
            if req.method == "HEAD":
                return httpx.Response(200, headers={"content-length": str(len(payload))})
            return httpx.Response(200, content=payload)

        with _ClientFactory(handler):
            result = oss.resolve_video_source(
                {"type": "url", "value": "https://oss.ecmax.cn/rpa/x.mp4"},
                self.tmp / "dest",
            )
        self.assertEqual(result, self.tmp / "dest" / "source.mp4")
        self.assertEqual(result.read_bytes(), payload)

    def test_oss_full_flow(self):
        payload = b"OSS-VIDEO"

        def handler(req):
            if req.method == "HEAD":
                return httpx.Response(200, headers={"content-length": str(len(payload))})
            return httpx.Response(200, content=payload)

        with _ClientFactory(handler):
            result = oss.resolve_video_source(
                {"type": "oss", "value": "oss://rpa/path/x.mp4"},
                self.tmp / "dest",
            )
        self.assertTrue(result.exists())
        self.assertEqual(result.read_bytes(), payload)

    def test_unknown_type(self):
        with self.assertRaises(oss.InvalidSource):
            oss.resolve_video_source({"type": "ftp", "value": "x"}, self.tmp / "dest")


if __name__ == "__main__":
    unittest.main()
