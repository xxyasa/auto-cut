"""PR-2b: lifespan 启动/关闭测试 + orphan 恢复。"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from fastapi.testclient import TestClient

from autocut import brand_repo, business_api, jobs


VALID_TOKEN = "tokentokentokentoken1234"


class LifespanTests(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        root = Path(self._tmp.name)
        self.jobs_dir = root / "jobs"
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        # 预先写一个 running 的 orphan
        self._write_orphan("job_orphan1", "running")
        self._env = patch.dict(
            "os.environ",
            {
                "AUTOCUT_API_TOKEN": VALID_TOKEN,
                brand_repo.ENV_REPO_PATH: str(root / "brand_repo.json"),
                business_api.ENV_UPLOAD_DIR: str(root / "uploads"),
                business_api.ENV_RUNS_DIR: str(root / "runs"),
                jobs.ENV_JOBS_DIR: str(self.jobs_dir),
                # 注意：本组允许 worker 启动
            },
        )
        self._env.start()
        jobs._reset_for_tests()

    def tearDown(self):
        jobs.stop_worker(timeout=2.0)
        jobs._reset_for_tests()
        self._env.stop()
        self._tmp.cleanup()

    def _write_orphan(self, job_id: str, status: str):
        path = self.jobs_dir / job_id / "job.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "id": job_id,
                    "status": status,
                    "stage": "...",
                    "progress": 0.5,
                    "queued_at": "2026-05-12T00:00:00+00:00",
                    "started_at": "2026-05-12T00:00:01+00:00",
                    "finished_at": None,
                    "updated_at": "2026-05-12T00:00:02+00:00",
                    "request": {},
                    "artifacts": {},
                    "error": None,
                    "failure_kind": None,
                    "tracks": ["enabled"],
                    "tracks_result": {},
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    def test_startup_recovers_orphan_and_starts_worker(self):
        from autocut.api import create_app

        app = create_app()
        # TestClient context manager 触发 startup/shutdown
        with TestClient(app) as client:
            self.assertTrue(jobs.is_worker_running())
            resp = client.get(
                "/api/business/jobs/job_orphan1",
                headers={"Authorization": f"Bearer {VALID_TOKEN}"},
            )
            self.assertEqual(resp.status_code, 200)
            body = resp.json()
            self.assertEqual(body["status"], "failed")
            self.assertEqual(body["failure_kind"], "recovered")
        # 退出后 worker 应已停止
        self.assertFalse(jobs.is_worker_running())


if __name__ == "__main__":
    unittest.main()
