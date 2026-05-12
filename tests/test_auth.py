"""auth.py 单元测试（提案 §C 验收子集）。"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from autocut import auth


class _FakeRequest:
    """最小化模拟 FastAPI Request：仅用到 .headers 与 .cookies。"""

    def __init__(
        self,
        headers: dict[str, str] | None = None,
        cookies: dict[str, str] | None = None,
    ):
        # 模拟 fastapi 的 headers 行为：大小写不敏感
        self._raw_headers = headers or {}
        self.cookies = cookies or {}

    @property
    def headers(self):
        # 提供 .get(name) 接口；用 lower-case 表做不敏感比对
        lookup = {k.lower(): v for k, v in self._raw_headers.items()}

        class _H:
            def get(_self, key, default=None):
                return lookup.get(key.lower(), default)

        return _H()


class LoadExpectedTokenTests(unittest.TestCase):
    def test_missing_env_raises(self):
        with patch.dict("os.environ", {}, clear=False):
            import os
            os.environ.pop(auth.ENV_VAR, None)
            with self.assertRaises(auth.AuthConfigError):
                auth.load_expected_token()

    def test_empty_env_raises(self):
        with patch.dict("os.environ", {auth.ENV_VAR: "   "}):
            with self.assertRaises(auth.AuthConfigError):
                auth.load_expected_token()

    def test_valid_env_returns_stripped(self):
        with patch.dict("os.environ", {auth.ENV_VAR: "  secret  "}):
            self.assertEqual(auth.load_expected_token(), "secret")


class ExtractTokenTests(unittest.TestCase):
    def test_bearer_priority(self):
        token = auth.extract_token("Bearer abc", "xyz", "cookie-tok")
        self.assertEqual(token, "abc")

    def test_bearer_case_insensitive_scheme(self):
        self.assertEqual(auth.extract_token("bearer abc", None, None), "abc")

    def test_bearer_empty_value_falls_through(self):
        self.assertEqual(auth.extract_token("Bearer ", "xyz", None), "xyz")

    def test_header_when_no_bearer(self):
        self.assertEqual(auth.extract_token(None, "xyz", "cookie-tok"), "xyz")

    def test_cookie_last(self):
        self.assertEqual(auth.extract_token(None, None, "cookie-tok"), "cookie-tok")

    def test_all_none(self):
        self.assertIsNone(auth.extract_token(None, None, None))

    def test_basic_scheme_ignored(self):
        # Basic xxx 不应被当成 Bearer 取值
        self.assertIsNone(auth.extract_token("Basic abc", None, None))


class VerifyTokenTests(unittest.TestCase):
    def test_equal_true(self):
        self.assertTrue(auth.verify_token("abc", "abc"))

    def test_diff_false(self):
        self.assertFalse(auth.verify_token("abc", "abd"))

    def test_none_false(self):
        self.assertFalse(auth.verify_token(None, "abc"))
        self.assertFalse(auth.verify_token("abc", ""))

    def test_diff_length(self):
        self.assertFalse(auth.verify_token("abc", "abcdef"))


class RequireTokenTests(unittest.TestCase):
    """模拟 require_token 在不同 token 通道下的行为。"""

    def _call(self, headers=None, cookies=None, env_token="secret"):
        with patch.dict("os.environ", {auth.ENV_VAR: env_token}):
            request = _FakeRequest(headers=headers, cookies=cookies)
            return auth.require_token(request)

    def test_bearer_ok(self):
        # 不抛即通过
        self._call(headers={"Authorization": "Bearer secret"})

    def test_x_header_ok(self):
        self._call(headers={"X-Autocut-Token": "secret"})

    def test_cookie_ok(self):
        self._call(cookies={"autocut_token": "secret"})

    def test_wrong_bearer_401(self):
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as ctx:
            self._call(headers={"Authorization": "Bearer wrong"})
        self.assertEqual(ctx.exception.status_code, 401)

    def test_missing_all_401(self):
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as ctx:
            self._call()
        self.assertEqual(ctx.exception.status_code, 401)

    def test_misconfigured_500(self):
        # 环境变量被清掉时是 500（非 401），让运维明确感知配置漏
        from fastapi import HTTPException
        import os
        os.environ.pop(auth.ENV_VAR, None)
        request = _FakeRequest(headers={"Authorization": "Bearer secret"})
        with self.assertRaises(HTTPException) as ctx:
            auth.require_token(request)
        self.assertEqual(ctx.exception.status_code, 500)


if __name__ == "__main__":
    unittest.main()
