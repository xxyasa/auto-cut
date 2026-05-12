"""异步任务调度（提案 §4）。

设计要点：
- 单进程 + 单 worker 线程；FastAPI lifespan 中 start/stop（接入由 PR-2b 完成）。
- 状态机：queued → downloading → running → remixing → done / partial_success / failed。
- Job dataclass 全字段落 `data/jobs/<job_id>/job.json`，每次状态推进一次 atomic_write。
- `job.log` append-only，每个 job 一把 RLock。
- 启动时 `recover_orphaned_jobs()` 扫所有非终态 job 改写为 `failed (recovered)`。
- 提供 `enqueue(payload, runner) -> job_id`；`runner` 是个回调，签名 `runner(job, log) -> dict`，避免 jobs.py 反向依赖 pipeline / oss / remix。

公共入口：
    Job dataclass + 枚举类型
    enqueue(request, runner)
    get_job(job_id) / list_jobs(limit) / read_log(job_id, n)
    start_worker() / stop_worker() / is_worker_running()
    recover_orphaned_jobs()
"""

from __future__ import annotations

import json
import logging
import os
import queue
import secrets
import tempfile
import threading
import time
from collections import defaultdict
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Optional

# ---------- 类型 / 常量 ----------

JobStatus = Literal[
    "queued",
    "downloading",
    "running",
    "remixing",
    "done",
    "partial_success",
    "failed",
]

FailureKind = Literal[
    "download",
    "pipeline",
    "remix",
    "recovered",
    "unknown",
]

TrackName = Literal["enabled", "remix"]

TERMINAL_STATUSES: set[str] = {"done", "partial_success", "failed"}

ENV_JOBS_DIR = "AUTOCUT_JOBS_DIR"
ENV_DISABLE_WORKER = "AUTOCUT_DISABLE_WORKER"
DEFAULT_JOBS_DIR = "data/jobs"

WORKER_STOP_TIMEOUT = 5.0  # 秒

logger = logging.getLogger(__name__)


# ---------- Job dataclass ----------


@dataclass
class Job:
    id: str
    status: JobStatus = "queued"
    stage: str = "已入队"
    progress: float = 0.0
    queued_at: str = ""
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    updated_at: str = ""
    request: dict[str, Any] = field(default_factory=dict)  # 已脱敏 payload
    artifacts: dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None
    failure_kind: Optional[FailureKind] = None
    tracks: list[TrackName] = field(default_factory=list)
    tracks_result: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------- 内部状态 ----------

_JOBS_LOCK = threading.RLock()
_JOB_FILE_LOCKS: dict[str, threading.RLock] = defaultdict(threading.RLock)
_QUEUE: "queue.Queue[Optional[tuple[str, Callable]]]" = queue.Queue()
_WORKER_THREAD: Optional[threading.Thread] = None
_WORKER_STOP_SENTINEL = None  # 入队 None 触发退出
_WORKER_STARTED = threading.Event()


# ---------- 路径 / IO ----------


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _jobs_root() -> Path:
    return Path(os.environ.get(ENV_JOBS_DIR, DEFAULT_JOBS_DIR)).resolve()


def _job_dir(job_id: str) -> Path:
    return _jobs_root() / job_id


def _job_json_path(job_id: str) -> Path:
    return _job_dir(job_id) / "job.json"


def _job_log_path(job_id: str) -> Path:
    return _job_dir(job_id) / "job.log"


def _atomic_write(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=path.name + ".",
        suffix=".tmp",
        dir=str(path.parent),
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2, sort_keys=False)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, path)
    except Exception:
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise


def _save_job(job: Job) -> None:
    job.updated_at = _now_iso()
    with _JOB_FILE_LOCKS[job.id]:
        _atomic_write(_job_json_path(job.id), job.to_dict())


def _load_job_from_disk(job_id: str) -> Optional[Job]:
    path = _job_json_path(job_id)
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("job.json read failed: %s: %s", path, exc)
        return None
    if not isinstance(data, dict):
        return None
    # 容错：未知字段忽略，缺失字段补默认
    fields = {f for f in Job.__dataclass_fields__}  # type: ignore[attr-defined]
    return Job(**{k: v for k, v in data.items() if k in fields})


def _redact_request(request: dict[str, Any]) -> dict[str, Any]:
    """脱敏：去掉 asr.api_key 等敏感字段。"""
    redacted = json.loads(json.dumps(request, ensure_ascii=False, default=str))
    asr = redacted.get("asr")
    if isinstance(asr, dict):
        for sensitive in ("api_key", "token", "secret"):
            if sensitive in asr:
                asr[sensitive] = "***"
    return redacted


# ---------- 日志 ----------


def log_event(
    job_id: str,
    level: str,
    stage: str,
    msg: str,
    *,
    extra: Optional[dict[str, Any]] = None,
) -> None:
    line = json.dumps(
        {
            "ts": _now_iso(),
            "level": level,
            "job_id": job_id,
            "stage": stage,
            "msg": msg,
            **(extra or {}),
        },
        ensure_ascii=False,
    )
    log_path = _job_log_path(job_id)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with _JOB_FILE_LOCKS[job_id]:
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")


def read_log(job_id: str, *, tail: int = 200) -> list[str]:
    log_path = _job_log_path(job_id)
    if not log_path.exists():
        return []
    with _JOB_FILE_LOCKS[job_id]:
        with log_path.open("r", encoding="utf-8") as fh:
            lines = fh.readlines()
    return [line.rstrip("\n") for line in lines[-tail:]]


# ---------- 公共查询 ----------


def get_job(job_id: str) -> Optional[Job]:
    return _load_job_from_disk(job_id)


def list_jobs(limit: int = 50) -> list[Job]:
    root = _jobs_root()
    if not root.exists():
        return []
    candidates: list[tuple[float, Path]] = []
    for child in root.iterdir():
        if not child.is_dir():
            continue
        path = child / "job.json"
        if path.exists():
            try:
                candidates.append((path.stat().st_mtime, child))
            except OSError:
                continue
    candidates.sort(key=lambda item: item[0], reverse=True)
    out: list[Job] = []
    for _, child in candidates[:limit]:
        job = _load_job_from_disk(child.name)
        if job is not None:
            out.append(job)
    return out


# ---------- 入队 / Worker ----------


JobRunner = Callable[[Job], dict[str, Any]]
"""runner(job) 必须返回 artifacts 字典，并通过 update_status() 推进状态。

约定：
- runner 内部应频繁调 update_status / log_event。
- runner 抛 OssError → failure_kind=download。
- runner 抛 PipelineError(-like) → failure_kind=pipeline。
- runner 抛 RemixError(-like) → failure_kind=remix。
- runner 抛其他 → failure_kind=unknown。
- runner 返回值会 merge 到 job.artifacts；status/finalize 由 jobs.py 完成。
"""


def update_status(
    job: Job,
    *,
    status: Optional[JobStatus] = None,
    stage: Optional[str] = None,
    progress: Optional[float] = None,
    artifacts: Optional[dict[str, Any]] = None,
    tracks_result: Optional[dict[str, str]] = None,
) -> None:
    """runner 内部调用：推进 job 状态并落盘。"""
    if status is not None:
        job.status = status
    if stage is not None:
        job.stage = stage
    if progress is not None:
        job.progress = max(0.0, min(1.0, progress))
    if artifacts:
        job.artifacts.update(artifacts)
    if tracks_result:
        job.tracks_result.update(tracks_result)
    _save_job(job)


def enqueue(
    request: dict[str, Any],
    runner: JobRunner,
    *,
    tracks: Optional[list[TrackName]] = None,
) -> str:
    """提交任务。runner 由调用方提供（路由层把 oss/pipeline/remix 拼好传进来）。"""
    job_id = f"job_{secrets.token_hex(4)}"
    now = _now_iso()
    job = Job(
        id=job_id,
        status="queued",
        stage="已入队",
        progress=0.0,
        queued_at=now,
        updated_at=now,
        request=_redact_request(request),
        tracks=tracks or [],
    )
    with _JOBS_LOCK:
        _save_job(job)
        log_event(job_id, "INFO", "queued", "job enqueued", extra={"tracks": job.tracks})
        _QUEUE.put((job_id, runner))
    return job_id


def _execute_job(job_id: str, runner: JobRunner) -> None:
    """worker 线程内单 job 执行。"""
    job = _load_job_from_disk(job_id)
    if job is None:
        logger.error("job %s missing on disk; skipping", job_id)
        return
    job.started_at = _now_iso()
    update_status(job, status="downloading", stage="开始处理")
    log_event(job_id, "INFO", "started", "worker picked up job")

    try:
        artifacts = runner(job) or {}
    except BaseException as exc:  # 捕一切，避免 worker 线程死掉
        kind: FailureKind = _classify_failure(exc)
        job.error = f"{type(exc).__name__}: {exc}"
        job.failure_kind = kind
        # 部分成功判定：tracks 含 enabled 且 enabled 已 ok
        if (
            kind == "remix"
            and "remix" in (job.tracks or [])
            and "enabled" in (job.tracks or [])
            and job.tracks_result.get("enabled") == "ok"
        ):
            update_status(
                job,
                status="partial_success",
                stage="remix 失败，初剪可用",
                tracks_result={"remix": "failed"},
            )
            log_event(job_id, "WARN", "partial_success", str(exc))
        else:
            update_status(job, status="failed", stage="任务失败")
            log_event(job_id, "ERROR", "failed", str(exc), extra={"failure_kind": kind})
        job.finished_at = _now_iso()
        _save_job(job)
        return

    job.artifacts.update(artifacts)
    job.finished_at = _now_iso()
    update_status(job, status="done", stage="完成", progress=1.0)
    log_event(job_id, "INFO", "done", "job completed")


def _classify_failure(exc: BaseException) -> FailureKind:
    """根据异常 module+class name 粗分类，避免硬依赖 oss / pipeline 模块。"""
    module = type(exc).__module__
    name = type(exc).__name__
    if module.endswith(".oss") or "Oss" in name or name in (
        "PrivateAddressBlocked",
        "HostNotAllowed",
        "RedirectNotAllowed",
        "TooLarge",
        "UpstreamError",
        "SchemeNotAllowed",
        "ExtensionNotAllowed",
        "InvalidSource",
    ):
        return "download"
    if "remix" in module or "Remix" in name:
        return "remix"
    if "pipeline" in module or "Pipeline" in name:
        return "pipeline"
    return "unknown"


# ---------- worker 生命周期 ----------


def _worker_loop() -> None:
    while True:
        item = _QUEUE.get()
        if item is None:
            _QUEUE.task_done()
            return
        job_id, runner = item
        try:
            _execute_job(job_id, runner)
        except BaseException:  # noqa: BLE001
            logger.exception("worker outer loop caught unexpected error")
        finally:
            _QUEUE.task_done()


def start_worker() -> None:
    """幂等启动单 worker 线程。dev 测试可用 AUTOCUT_DISABLE_WORKER=1 跳过。"""
    if os.environ.get(ENV_DISABLE_WORKER, "").strip() == "1":
        return
    global _WORKER_THREAD
    with _JOBS_LOCK:
        if _WORKER_STARTED.is_set() and _WORKER_THREAD and _WORKER_THREAD.is_alive():
            return
        _WORKER_STARTED.set()
        _WORKER_THREAD = threading.Thread(
            target=_worker_loop,
            name="autocut-jobs-worker",
            daemon=True,
        )
        _WORKER_THREAD.start()


def stop_worker(timeout: float = WORKER_STOP_TIMEOUT) -> None:
    global _WORKER_THREAD
    with _JOBS_LOCK:
        thread = _WORKER_THREAD
        if not _WORKER_STARTED.is_set():
            return
        _QUEUE.put(_WORKER_STOP_SENTINEL)
    if thread is not None:
        thread.join(timeout)
    with _JOBS_LOCK:
        _WORKER_STARTED.clear()
        _WORKER_THREAD = None


def is_worker_running() -> bool:
    with _JOBS_LOCK:
        return (
            _WORKER_STARTED.is_set()
            and _WORKER_THREAD is not None
            and _WORKER_THREAD.is_alive()
        )


def queue_size() -> int:
    return _QUEUE.qsize()


# ---------- 启动恢复 ----------


def recover_orphaned_jobs() -> list[str]:
    """扫所有非终态 job 改写成 failed (recovered)。返回受影响 job_id。

    在 lifespan startup 中、start_worker 之前调用。
    """
    root = _jobs_root()
    if not root.exists():
        return []
    affected: list[str] = []
    for child in root.iterdir():
        if not child.is_dir():
            continue
        job = _load_job_from_disk(child.name)
        if job is None:
            continue
        if job.status in TERMINAL_STATUSES:
            continue
        job.status = "failed"
        job.failure_kind = "recovered"
        job.error = "recovered after restart"
        job.finished_at = _now_iso()
        job.stage = "进程重启已恢复"
        _save_job(job)
        log_event(
            job.id,
            "WARN",
            "recovered",
            "[RECOVERED] worker did not finish before restart",
        )
        affected.append(job.id)
    return affected


# ---------- 测试辅助 ----------


def _reset_for_tests() -> None:
    """单测之间清空模块状态。生产代码勿调。"""
    global _WORKER_THREAD
    with _JOBS_LOCK:
        # 等当前 worker 退出
        if _WORKER_STARTED.is_set():
            try:
                _QUEUE.put_nowait(_WORKER_STOP_SENTINEL)
            except queue.Full:  # pragma: no cover
                pass
        thread = _WORKER_THREAD
    if thread is not None:
        thread.join(WORKER_STOP_TIMEOUT)
    with _JOBS_LOCK:
        _WORKER_STARTED.clear()
        _WORKER_THREAD = None
        # 排空队列
        try:
            while True:
                _QUEUE.get_nowait()
                _QUEUE.task_done()
        except queue.Empty:
            pass
        _JOB_FILE_LOCKS.clear()


def wait_until_idle(timeout: float = 5.0) -> bool:
    """阻塞到队列处理完为止。仅供测试。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _QUEUE.unfinished_tasks == 0:
            return True
        time.sleep(0.02)
    return False
