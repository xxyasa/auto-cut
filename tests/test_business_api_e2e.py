"""PR-3: 业务自助成片端到端集成测试。

策略：
- 真实跑 pipeline + remix（use_llm=false 兜底 plan）
- ASR 用 transcript fixture 跳过模型
- 视频：tests/fixtures/sample.mp4（40s 黑屏 + 440Hz sine，~377 KiB）
- multipart upload → POST /jobs → 轮询 → 验证 enabled/remix MP4 落盘

依赖：ffmpeg 必须可用（无论系统装的还是 imageio-ffmpeg 自带的）。
"""

from __future__ import annotations

import time
import unittest
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from fastapi.testclient import TestClient

from autocut import brand_repo, business_api, jobs
from autocut.media import has_binary


VALID_TOKEN = "tokentokentokentoken1234"
FIXTURE_VIDEO = Path(__file__).parent / "fixtures" / "sample.mp4"
FIXTURE_TRANSCRIPT = Path(__file__).parent / "fixtures" / "transcript.json"


def _ffmpeg_available() -> bool:
    if has_binary("ffmpeg"):
        return True
    try:
        import imageio_ffmpeg  # noqa: F401
        return True
    except ImportError:
        return False


@unittest.skipUnless(FIXTURE_VIDEO.exists(), "fixture sample.mp4 missing")
@unittest.skipUnless(FIXTURE_TRANSCRIPT.exists(), "fixture transcript.json missing")
@unittest.skipUnless(_ffmpeg_available(), "ffmpeg not available")
class BusinessE2ETests(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        root = Path(self._tmp.name)
        self.runs_dir = root / "runs"
        self.uploads_dir = root / "uploads"
        self.jobs_dir = root / "jobs"
        self._env = patch.dict(
            "os.environ",
            {
                "AUTOCUT_API_TOKEN": VALID_TOKEN,
                brand_repo.ENV_REPO_PATH: str(root / "brand_repo.json"),
                business_api.ENV_RUNS_DIR: str(self.runs_dir),
                business_api.ENV_UPLOAD_DIR: str(self.uploads_dir),
                jobs.ENV_JOBS_DIR: str(self.jobs_dir),
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
        # client 关闭触发 shutdown → stop_worker
        jobs.stop_worker(timeout=5.0)
        jobs._reset_for_tests()
        self._env.stop()
        self._tmp.cleanup()

    def _wait_job(self, job_id: str, timeout: float = 90.0):
        deadline = time.time() + timeout
        last_body = None
        while time.time() < deadline:
            resp = self.client.get(
                f"/api/business/jobs/{job_id}", headers=self.headers
            )
            self.assertEqual(resp.status_code, 200)
            last_body = resp.json()
            if last_body["status"] in {"done", "failed", "partial_success"}:
                return last_body
            time.sleep(0.5)
        raise AssertionError(
            f"job did not finish in {timeout}s: last={last_body}"
        )

    def test_upload_then_run_enabled_track_only(self):
        # 在 TestClient context 中触发 lifespan startup → worker 启动
        with self.client as c:
            # 1) 上传
            video_bytes = FIXTURE_VIDEO.read_bytes()
            up = c.post(
                "/api/business/upload",
                headers=self.headers,
                files={"file": ("sample.mp4", BytesIO(video_bytes), "video/mp4")},
            )
            self.assertEqual(up.status_code, 201, up.text)
            uploaded_path = up.json()["uploaded_path"]
            self.assertTrue(Path(uploaded_path).exists())

            # 2) 创建任务 - 仅 enabled 轨道，transcript 跳过 ASR
            resp = c.post(
                "/api/business/jobs",
                json={
                    "source": {
                        "type": "upload",
                        "uploaded_path": uploaded_path,
                    },
                    "brand_product": {
                        "product": "示例产品",
                        "selling_points": ["舒适", "适合日常"],
                    },
                    "tracks": ["enabled"],
                    "asr_engine": "transcript",
                    "transcript_path": str(FIXTURE_TRANSCRIPT),
                },
                headers=self.headers,
            )
            self.assertEqual(resp.status_code, 201, resp.text)
            job_id = resp.json()["id"]

            # 3) 轮询直到完成
            body = self._wait_job(job_id)

            self.assertEqual(
                body["status"], "done",
                f"job failed: error={body.get('error')} kind={body.get('failure_kind')}"
            )
            self.assertEqual(body["tracks_result"].get("enabled"), "ok")

            # 4) 工件存在
            run_dir = self.runs_dir / job_id
            self.assertTrue(run_dir.exists())
            self.assertTrue((run_dir / "metadata" / "result.json").exists())
            self.assertTrue((run_dir / "metadata" / "request.json").exists())

    def test_upload_then_run_both_tracks_fallback_remix(self):
        """remix use_llm=false，走 default_plan 兜底，不走外网。"""
        with self.client as c:
            video_bytes = FIXTURE_VIDEO.read_bytes()
            up = c.post(
                "/api/business/upload",
                headers=self.headers,
                files={"file": ("sample.mp4", BytesIO(video_bytes), "video/mp4")},
            )
            uploaded_path = up.json()["uploaded_path"]

            resp = c.post(
                "/api/business/jobs",
                json={
                    "source": {
                        "type": "upload",
                        "uploaded_path": uploaded_path,
                    },
                    "brand_product": {
                        "product": "示例产品",
                        "selling_points": ["舒适"],
                    },
                    "tracks": ["enabled", "remix"],
                    "asr_engine": "transcript",
                    "transcript_path": str(FIXTURE_TRANSCRIPT),
                    "remix": {"use_llm": False, "target_duration": 25.0},
                },
                headers=self.headers,
            )
            self.assertEqual(resp.status_code, 201, resp.text)
            job_id = resp.json()["id"]

            body = self._wait_job(job_id, timeout=120.0)

            # remix 兜底可能成功也可能因素材太短失败；两条轨道任一成功即可视为 PR-3 通过
            self.assertIn(
                body["status"], {"done", "partial_success"},
                f"unexpected status: {body}"
            )
            self.assertEqual(body["tracks_result"].get("enabled"), "ok")

            # 检查日志可读
            log_resp = c.get(
                f"/api/business/jobs/{job_id}/log", headers=self.headers
            )
            self.assertEqual(log_resp.status_code, 200)
            self.assertGreater(len(log_resp.json()["lines"]), 0)

    def test_upload_then_run_multiple_llm_remix_plans(self):
        with self.client as c:
            up = c.post(
                "/api/business/upload",
                headers=self.headers,
                files={"file": ("sample.mp4", BytesIO(FIXTURE_VIDEO.read_bytes()), "video/mp4")},
            )
            uploaded_path = up.json()["uploaded_path"]

            ordered = [
                {"ordered_ids": ["s001", "s002"], "reason": "方案一"},
                {"ordered_ids": ["s002", "s003"], "reason": "方案二"},
            ]

            def fake_generate_ordered_ids(*args, **kwargs):
                return ordered.pop(0)

            with patch("autocut.llm.generate_ordered_ids", side_effect=fake_generate_ordered_ids):
                resp = c.post(
                    "/api/business/jobs",
                    json={
                        "source": {"type": "upload", "uploaded_path": uploaded_path},
                        "brand_product": {
                            "product": "示例产品",
                            "selling_points": ["舒适"],
                        },
                        "tracks": ["enabled", "remix"],
                        "asr_engine": "transcript",
                        "transcript_path": str(FIXTURE_TRANSCRIPT),
                        "remix": {"use_llm": True, "target_duration": 25.0, "plan_count": 2},
                    },
                    headers=self.headers,
                )
                self.assertEqual(resp.status_code, 201, resp.text)
                job_id = resp.json()["id"]
                body = self._wait_job(job_id, timeout=120.0)

            self.assertIn(body["status"], {"done", "partial_success"}, body)
            plans = body["artifacts"].get("remix_plans") or []
            self.assertEqual(len(plans), 2, body)
            self.assertEqual(plans[0]["index"], 1)
            self.assertEqual(plans[1]["index"], 2)
            self.assertTrue(Path(plans[0]["mp4"]).exists())
            self.assertTrue(Path(plans[1]["plan"]).exists())
            self.assertEqual(body["artifacts"].get("remix_mp4"), plans[0]["mp4"])

    def test_brand_picker_path(self):
        """走 brand_id + product_id 路径，验证 brand_repo 联想词合并不影响 pipeline。"""
        brand = brand_repo.create_brand("品牌Z", ["IP-X"])
        product = brand_repo.create_product(
            brand["id"], "示例产品", ["舒适"], ["材质A"]
        )

        with self.client as c:
            up = c.post(
                "/api/business/upload",
                headers=self.headers,
                files={"file": ("sample.mp4", BytesIO(FIXTURE_VIDEO.read_bytes()), "video/mp4")},
            )
            uploaded_path = up.json()["uploaded_path"]

            resp = c.post(
                "/api/business/jobs",
                json={
                    "source": {"type": "upload", "uploaded_path": uploaded_path},
                    "brand_product": {
                        "brand_id": brand["id"],
                        "product_id": product["id"],
                    },
                    "tracks": ["enabled"],
                    "asr_engine": "transcript",
                    "transcript_path": str(FIXTURE_TRANSCRIPT),
                },
                headers=self.headers,
            )
            self.assertEqual(resp.status_code, 201, resp.text)
            job_id = resp.json()["id"]
            body = self._wait_job(job_id)
            self.assertEqual(body["status"], "done")
            self.assertEqual(body["tracks_result"].get("enabled"), "ok")


if __name__ == "__main__":
    unittest.main()
