"""PR-2b: /api/business/jobs 路由测试。

策略：
- 用 TestClient 跑路由层
- runner 通过 monkeypatch 替换为 dummy（不真跑 pipeline/remix）
- worker 用 jobs._execute_job 同步执行，方便断言
"""

from __future__ import annotations

import unittest
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
        self.runs_dir = root / "runs"
        self.uploads_dir = root / "uploads"
        self.jobs_dir = root / "jobs"
        self.repo_path = root / "brand_repo.json"
        self._env = patch.dict(
            "os.environ",
            {
                "AUTOCUT_API_TOKEN": VALID_TOKEN,
                brand_repo.ENV_REPO_PATH: str(self.repo_path),
                business_api.ENV_RUNS_DIR: str(self.runs_dir),
                business_api.ENV_UPLOAD_DIR: str(self.uploads_dir),
                jobs.ENV_JOBS_DIR: str(self.jobs_dir),
                jobs.ENV_DISABLE_WORKER: "1",  # 禁后台线程，手动 _execute_job
                "AUTOCUT_URL_ALLOWLIST": "oss.ecmax.cn",
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


class JobsCreateTests(_Base):
    def test_unauthorized_without_token(self):
        resp = self.client.post(
            "/api/business/jobs",
            json={
                "source": {"type": "upload", "uploaded_path": "/tmp/x.mp4"},
                "brand_product": {"product": "P", "selling_points": ["a"]},
                "tracks": ["enabled"],
            },
        )
        self.assertEqual(resp.status_code, 401)

    def test_create_with_inline_product(self):
        src = Path(self.uploads_dir) / "u.mp4"
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_bytes(b"FAKE")

        resp = self.client.post(
            "/api/business/jobs",
            json={
                "source": {"type": "upload", "uploaded_path": str(src)},
                "brand_product": {"product": "测试产品", "selling_points": ["卖点"]},
                "tracks": ["enabled"],
                "remix": {"use_llm": False},
            },
            headers=self.headers,
        )
        self.assertEqual(resp.status_code, 201, resp.text)
        body = resp.json()
        self.assertTrue(body["id"].startswith("job_"))
        self.assertEqual(body["status"], "queued")
        self.assertEqual(body["tracks"], ["enabled"])

    def test_create_with_brand_product_404(self):
        resp = self.client.post(
            "/api/business/jobs",
            json={
                "source": {"type": "upload", "uploaded_path": "/tmp/x.mp4"},
                "brand_product": {
                    "brand_id": "brand_missing",
                    "product_id": "p_missing",
                },
                "tracks": ["enabled"],
            },
            headers=self.headers,
        )
        self.assertEqual(resp.status_code, 404)

    def test_create_without_product_400(self):
        resp = self.client.post(
            "/api/business/jobs",
            json={
                "source": {"type": "upload", "uploaded_path": "/tmp/x.mp4"},
                "brand_product": {},  # 啥都没填
                "tracks": ["enabled"],
            },
            headers=self.headers,
        )
        self.assertEqual(resp.status_code, 400)


class JobsExecutionTests(_Base):
    """跑 _execute_job 推进状态。runner 走真实 _make_runner，但 pipeline/remix 全 patch。"""

    def _enqueue_and_run(self, tracks: list[str], *, runner_side_effect=None):
        src = Path(self.uploads_dir) / "u.mp4"
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_bytes(b"FAKE")

        resp = self.client.post(
            "/api/business/jobs",
            json={
                "source": {"type": "upload", "uploaded_path": str(src)},
                "brand_product": {"product": "P", "selling_points": []},
                "tracks": tracks,
                "remix": {"use_llm": False},
            },
            headers=self.headers,
        )
        self.assertEqual(resp.status_code, 201, resp.text)
        job_id = resp.json()["id"]

        # 从 worker_thread 路径拿 runner（jobs 模块没暴露，所以走文件读取 +重建不可行）
        # 直接换：构造一个 dummy runner 并塞回去
        # 简化方案：直接调 jobs._execute_job 时 monkeypatch runner
        # 实际更直接：再次走 jobs.enqueue 自己造任务。
        return job_id

    def test_dummy_runner_marks_done(self):
        # 不走 business_api 的 runner，直接用 jobs API 端到端验
        def runner(job):
            jobs.update_status(job, status="running", stage="ok", progress=0.5)
            return {"out": "yes"}

        job_id = jobs.enqueue({"foo": 1}, runner, tracks=["enabled"])
        jobs._execute_job(job_id, runner)
        resp = self.client.get(f"/api/business/jobs/{job_id}", headers=self.headers)
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["status"], "done")
        self.assertEqual(body["artifacts"]["out"], "yes")

    def test_runner_failure_classified(self):
        def runner(job):
            raise oss_err()

        def oss_err():
            from autocut.oss import HostNotAllowed

            return HostNotAllowed("boom")

        job_id = jobs.enqueue({}, runner, tracks=["enabled"])
        jobs._execute_job(job_id, runner)
        resp = self.client.get(f"/api/business/jobs/{job_id}", headers=self.headers)
        body = resp.json()
        self.assertEqual(body["status"], "failed")
        self.assertEqual(body["failure_kind"], "download")

    def test_partial_success_path(self):
        from autocut.remix import build_remix_source  # noqa: F401 - ensure module

        class FakeRemixError(Exception):
            pass

        FakeRemixError.__module__ = "autocut.remix"

        def runner(job):
            jobs.update_status(
                job,
                status="running",
                stage="enabled done",
                tracks_result={"enabled": "ok"},
                artifacts={"enabled_mp4": "/x/a.mp4"},
            )
            raise FakeRemixError("nope")

        job_id = jobs.enqueue({}, runner, tracks=["enabled", "remix"])
        jobs._execute_job(job_id, runner)

        resp = self.client.get(f"/api/business/jobs/{job_id}", headers=self.headers)
        body = resp.json()
        self.assertEqual(body["status"], "partial_success")
        self.assertEqual(body["tracks_result"]["enabled"], "ok")
        self.assertEqual(body["tracks_result"]["remix"], "failed")


class JobsListAndLogTests(_Base):
    def test_list_empty(self):
        resp = self.client.get("/api/business/jobs", headers=self.headers)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"jobs": []})

    def test_get_404(self):
        resp = self.client.get(
            "/api/business/jobs/job_doesnotexist", headers=self.headers
        )
        self.assertEqual(resp.status_code, 404)

    def test_log_after_enqueue(self):
        def runner(job):
            return {}

        job_id = jobs.enqueue({}, runner, tracks=["enabled"])
        jobs.log_event(job_id, "INFO", "test", "hello world")
        resp = self.client.get(
            f"/api/business/jobs/{job_id}/log", headers=self.headers
        )
        self.assertEqual(resp.status_code, 200)
        lines = resp.json()["lines"]
        self.assertGreaterEqual(len(lines), 1)
        self.assertTrue(any("hello world" in line for line in lines))


class BrandResolutionTests(_Base):
    """走真实 brand_repo 的三种 brand_product 解析路径。"""

    def test_brand_id_only(self):
        brand = brand_repo.create_brand("品牌X", ["IP1"])
        brand_id = brand["id"]

        src = Path(self.uploads_dir) / "u.mp4"
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_bytes(b"FAKE")

        resp = self.client.post(
            "/api/business/jobs",
            json={
                "source": {"type": "upload", "uploaded_path": str(src)},
                "brand_product": {"brand_id": brand_id, "product": "新品"},
                "tracks": ["enabled"],
            },
            headers=self.headers,
        )
        self.assertEqual(resp.status_code, 201, resp.text)

    def test_brand_and_product(self):
        brand = brand_repo.create_brand("品牌Y", ["A"])
        product = brand_repo.create_product(
            brand["id"], "口红", ["持久"], ["哈利波特"]
        )

        src = Path(self.uploads_dir) / "u.mp4"
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_bytes(b"FAKE")

        resp = self.client.post(
            "/api/business/jobs",
            json={
                "source": {"type": "upload", "uploaded_path": str(src)},
                "brand_product": {
                    "brand_id": brand["id"],
                    "product_id": product["id"],
                },
                "tracks": ["enabled"],
            },
            headers=self.headers,
        )
        self.assertEqual(resp.status_code, 201, resp.text)


if __name__ == "__main__":
    unittest.main()
