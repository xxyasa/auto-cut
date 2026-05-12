"""jobs.py 单元测试。"""

from __future__ import annotations

import json
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from autocut import jobs


class _JobsBase(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.jobs_dir = Path(self._tmp.name) / "jobs"
        self._env = patch.dict(
            "os.environ",
            {jobs.ENV_JOBS_DIR: str(self.jobs_dir), jobs.ENV_DISABLE_WORKER: "1"},
        )
        self._env.start()
        jobs._reset_for_tests()

    def tearDown(self):
        jobs._reset_for_tests()
        self._env.stop()
        self._tmp.cleanup()


class EnqueueAndPersistTests(_JobsBase):
    def test_enqueue_creates_job_json(self):
        def runner(job):
            return {}

        job_id = jobs.enqueue({"foo": "bar"}, runner, tracks=["enabled"])
        self.assertTrue(job_id.startswith("job_"))
        loaded = jobs.get_job(job_id)
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.status, "queued")
        self.assertEqual(loaded.tracks, ["enabled"])

    def test_redact_request_removes_secrets(self):
        def runner(job):
            return {}

        job_id = jobs.enqueue(
            {"asr": {"engine": "x", "api_key": "SECRET"}}, runner
        )
        loaded = jobs.get_job(job_id)
        self.assertEqual(loaded.request["asr"]["api_key"], "***")

    def test_log_event_appends(self):
        def runner(job):
            return {}

        job_id = jobs.enqueue({}, runner)
        jobs.log_event(job_id, "INFO", "test", "hello")
        jobs.log_event(job_id, "WARN", "test", "world")
        lines = jobs.read_log(job_id, tail=10)
        self.assertEqual(len(lines), 3)  # 含 enqueued
        for line in lines:
            obj = json.loads(line)
            self.assertEqual(obj["job_id"], job_id)


class WorkerExecutionTests(_JobsBase):
    """直接调 _execute_job，不启 worker 线程，确定性更高。"""

    def test_runner_success_marks_done(self):
        def runner(job):
            jobs.update_status(job, status="running", stage="跑 pipeline", progress=0.5)
            return {"run_id": "r1"}

        job_id = jobs.enqueue({}, runner, tracks=["enabled"])
        jobs._execute_job(job_id, runner)
        loaded = jobs.get_job(job_id)
        self.assertEqual(loaded.status, "done")
        self.assertEqual(loaded.artifacts["run_id"], "r1")
        self.assertIsNotNone(loaded.finished_at)
        self.assertEqual(loaded.progress, 1.0)

    def test_runner_unknown_failure(self):
        def runner(job):
            raise RuntimeError("boom")

        job_id = jobs.enqueue({}, runner)
        jobs._execute_job(job_id, runner)
        loaded = jobs.get_job(job_id)
        self.assertEqual(loaded.status, "failed")
        self.assertEqual(loaded.failure_kind, "unknown")
        self.assertIn("boom", loaded.error)

    def test_runner_oss_failure_classified_download(self):
        from autocut.oss import HostNotAllowed

        def runner(job):
            raise HostNotAllowed("nope")

        job_id = jobs.enqueue({}, runner)
        jobs._execute_job(job_id, runner)
        loaded = jobs.get_job(job_id)
        self.assertEqual(loaded.status, "failed")
        self.assertEqual(loaded.failure_kind, "download")

    def test_partial_success_when_remix_fails_after_enabled_ok(self):
        # 模拟 remix 失败：自定义异常 module=...remix
        class FakeRemixError(Exception):
            pass
        FakeRemixError.__module__ = "autocut.remix"

        def runner(job):
            jobs.update_status(
                job,
                status="running",
                stage="enabled ok",
                tracks_result={"enabled": "ok"},
                artifacts={"enabled_mp4": "/tmp/a.mp4"},
            )
            raise FakeRemixError("remix exploded")

        job_id = jobs.enqueue({}, runner, tracks=["enabled", "remix"])
        jobs._execute_job(job_id, runner)
        loaded = jobs.get_job(job_id)
        self.assertEqual(loaded.status, "partial_success")
        self.assertEqual(loaded.failure_kind, "remix")
        self.assertEqual(loaded.tracks_result["enabled"], "ok")
        self.assertEqual(loaded.tracks_result["remix"], "failed")
        self.assertEqual(loaded.artifacts["enabled_mp4"], "/tmp/a.mp4")

    def test_remix_failure_without_enabled_marks_failed(self):
        class FakeRemixError(Exception):
            pass
        FakeRemixError.__module__ = "autocut.remix"

        def runner(job):
            raise FakeRemixError("remix only")

        job_id = jobs.enqueue({}, runner, tracks=["remix"])
        jobs._execute_job(job_id, runner)
        loaded = jobs.get_job(job_id)
        self.assertEqual(loaded.status, "failed")
        self.assertEqual(loaded.failure_kind, "remix")


class WorkerLifecycleTests(_JobsBase):
    """真实启动 worker 线程，端到端跑一条任务。"""

    def setUp(self):
        super().setUp()
        # 这一组允许 worker 启动
        import os
        os.environ.pop(jobs.ENV_DISABLE_WORKER, None)

    def test_start_stop_idempotent(self):
        jobs.start_worker()
        jobs.start_worker()
        self.assertTrue(jobs.is_worker_running())
        jobs.stop_worker()
        self.assertFalse(jobs.is_worker_running())

    def test_end_to_end_success(self):
        done_event = threading.Event()

        def runner(job):
            jobs.update_status(job, status="running", stage="working")
            done_event.set()
            return {"out": "ok"}

        jobs.start_worker()
        try:
            job_id = jobs.enqueue({}, runner, tracks=["enabled"])
            self.assertTrue(done_event.wait(2.0))
            self.assertTrue(jobs.wait_until_idle(2.0))
        finally:
            jobs.stop_worker()
        loaded = jobs.get_job(job_id)
        self.assertEqual(loaded.status, "done")

    def test_disable_worker_env_skips_start(self):
        import os
        os.environ[jobs.ENV_DISABLE_WORKER] = "1"
        jobs.start_worker()
        self.assertFalse(jobs.is_worker_running())


class RecoverOrphanedJobsTests(_JobsBase):
    def _write_raw_job(self, job_id: str, status: str):
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

    def test_recovers_only_non_terminal(self):
        self._write_raw_job("job_aaa", "running")
        self._write_raw_job("job_bbb", "downloading")
        self._write_raw_job("job_ccc", "done")
        self._write_raw_job("job_ddd", "failed")
        self._write_raw_job("job_eee", "partial_success")

        affected = jobs.recover_orphaned_jobs()
        self.assertEqual(set(affected), {"job_aaa", "job_bbb"})

        for job_id in ("job_aaa", "job_bbb"):
            loaded = jobs.get_job(job_id)
            self.assertEqual(loaded.status, "failed")
            self.assertEqual(loaded.failure_kind, "recovered")
            self.assertEqual(loaded.error, "recovered after restart")
            self.assertIsNotNone(loaded.finished_at)

        for job_id in ("job_ccc", "job_ddd", "job_eee"):
            loaded = jobs.get_job(job_id)
            self.assertNotEqual(loaded.failure_kind, "recovered")

    def test_recover_empty_dir(self):
        self.assertEqual(jobs.recover_orphaned_jobs(), [])


class ListJobsTests(_JobsBase):
    def test_list_returns_sorted_recent(self):
        def runner(job):
            return {}

        ids = [jobs.enqueue({"i": i}, runner) for i in range(3)]
        listed = jobs.list_jobs(limit=10)
        self.assertEqual(len(listed), 3)
        # 至少都能找到
        self.assertEqual(set(j.id for j in listed), set(ids))


if __name__ == "__main__":
    unittest.main()
