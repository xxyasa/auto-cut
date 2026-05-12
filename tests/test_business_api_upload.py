"""PR-2b: /api/business/upload multipart 测试。"""

from __future__ import annotations

import unittest
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from fastapi.testclient import TestClient

from autocut import brand_repo, business_api, jobs


VALID_TOKEN = "tokentokentokentoken1234"


class _Base(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        root = Path(self._tmp.name)
        self.uploads_dir = root / "uploads"
        self._env = patch.dict(
            "os.environ",
            {
                "AUTOCUT_API_TOKEN": VALID_TOKEN,
                brand_repo.ENV_REPO_PATH: str(root / "brand_repo.json"),
                business_api.ENV_UPLOAD_DIR: str(self.uploads_dir),
                business_api.ENV_RUNS_DIR: str(root / "runs"),
                jobs.ENV_JOBS_DIR: str(root / "jobs"),
                jobs.ENV_DISABLE_WORKER: "1",
                business_api.ENV_MAX_UPLOAD_BYTES: "1024",  # 1 KiB for testing
            },
        )
        self._env.start()
        jobs._reset_for_tests()

        from autocut.api import create_app

        self.app = create_app()
        self.client = TestClient(self.app)
        self.headers = {"Authorization": f"Bearer {VALID_TOKEN}"}

    def tearDown(self):
        jobs._reset_for_tests()
        self._env.stop()
        self._tmp.cleanup()


class UploadTests(_Base):
    def test_upload_success(self):
        payload = b"VIDEO" * 50
        resp = self.client.post(
            "/api/business/upload",
            headers=self.headers,
            files={"file": ("clip.mp4", BytesIO(payload), "video/mp4")},
        )
        self.assertEqual(resp.status_code, 201, resp.text)
        body = resp.json()
        self.assertEqual(body["size"], len(payload))
        self.assertTrue(body["filename"].endswith(".mp4"))
        self.assertTrue(Path(body["uploaded_path"]).exists())

    def test_upload_rejects_bad_extension(self):
        resp = self.client.post(
            "/api/business/upload",
            headers=self.headers,
            files={"file": ("evil.exe", BytesIO(b"x"), "application/octet-stream")},
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("extension_not_allowed", resp.text)

    def test_upload_rejects_too_large(self):
        big = b"X" * 2048  # > 1 KiB limit
        resp = self.client.post(
            "/api/business/upload",
            headers=self.headers,
            files={"file": ("big.mp4", BytesIO(big), "video/mp4")},
        )
        self.assertEqual(resp.status_code, 413)
        # 确认半截文件已清理
        survivors = list(self.uploads_dir.glob("*.mp4"))
        self.assertEqual(survivors, [], f"leftover files: {survivors}")

    def test_upload_requires_token(self):
        resp = self.client.post(
            "/api/business/upload",
            files={"file": ("clip.mp4", BytesIO(b"x"), "video/mp4")},
        )
        self.assertEqual(resp.status_code, 401)

    def test_upload_sanitizes_filename(self):
        resp = self.client.post(
            "/api/business/upload",
            headers=self.headers,
            files={
                "file": (
                    "../../weird name; with $hell.mp4",
                    BytesIO(b"data"),
                    "video/mp4",
                )
            },
        )
        self.assertEqual(resp.status_code, 201)
        body = resp.json()
        path = Path(body["uploaded_path"])
        self.assertEqual(path.parent, self.uploads_dir)
        self.assertNotIn(";", path.name)
        self.assertNotIn("$", path.name)


if __name__ == "__main__":
    unittest.main()
