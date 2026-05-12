"""业务自助成片闭环：FastAPI 路由层。

PR-1 范围（已交付）：
  - 品牌/产品 CRUD（/api/business/brands*）
  - 鉴权统一走 `Depends(require_token)`

PR-2a 范围（已交付）：
  - suggest-associations 接 LLM

PR-2b 范围（本 PR）：
  - POST /api/business/upload     multipart 落盘到 data/uploads/
  - POST /api/business/jobs       创建任务（接 oss.resolve_video_source + jobs.enqueue + 拼 pipeline/remix runner）
  - GET  /api/business/jobs/{id}  状态
  - GET  /api/business/jobs/{id}/log  日志 tail
  - GET  /api/business/jobs       近期任务列表
  - api.py lifespan: recover_orphaned_jobs + start_worker / stop_worker

约定：
  - 路由抛业务异常 → 这里统一转 HTTPException。
  - 所有路由均挂 `dependencies=[Depends(require_token)]`，方便后期审计。
"""

from __future__ import annotations

import json
import os
import re
import secrets
import time
from pathlib import Path
from typing import Any, Optional

try:
    from fastapi import (
        APIRouter,
        Body,
        Depends,
        File,
        HTTPException,
        Query,
        UploadFile,
        status,
    )
    from pydantic import BaseModel, Field
except ImportError as exc:  # pragma: no cover
    raise RuntimeError(
        "Install API dependencies with: pip install -e '.[api]'"
    ) from exc

from . import brand_repo, jobs, oss
from .auth import require_token


# ---------- Pydantic 模型 ----------


class BrandCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    associations: list[str] = Field(default_factory=list)


class BrandPatch(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=128)
    associations: Optional[list[str]] = None


class ProductCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    selling_points: list[str] = Field(default_factory=list)
    associations: list[str] = Field(default_factory=list)


class ProductPatch(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=128)
    selling_points: Optional[list[str]] = None
    associations: Optional[list[str]] = None


# ---------- Router ----------


router = APIRouter(
    prefix="/api/business",
    tags=["business"],
    dependencies=[Depends(require_token)],
)


# ---------- 异常映射 ----------


def _map_repo_exc(exc: Exception) -> HTTPException:
    """把 brand_repo 业务异常映射成 HTTPException。"""
    if isinstance(exc, brand_repo.NotFound):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    if isinstance(exc, brand_repo.Conflict):
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    if isinstance(exc, brand_repo.LLMUnavailable):
        return HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc))
    if isinstance(exc, brand_repo.SchemaVersionError):
        return HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        )
    if isinstance(exc, brand_repo.BrandRepoError):
        return HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        )
    return HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail=f"internal_error: {exc}",
    )


def _call(func, *args, **kwargs):
    try:
        return func(*args, **kwargs)
    except brand_repo.BrandRepoError as exc:
        raise _map_repo_exc(exc) from exc


# ---------- 品牌路由 ----------


@router.get("/brands")
def list_brands_route():
    return {"brands": _call(brand_repo.list_brands)}


@router.post("/brands", status_code=status.HTTP_201_CREATED)
def create_brand_route(payload: BrandCreate = Body(...)):
    return _call(brand_repo.create_brand, payload.name, payload.associations)


@router.get("/brands/{brand_id}")
def get_brand_route(brand_id: str):
    return _call(brand_repo.get_brand, brand_id)


@router.patch("/brands/{brand_id}")
def update_brand_route(brand_id: str, patch: BrandPatch = Body(...)):
    return _call(
        brand_repo.update_brand,
        brand_id,
        patch.model_dump(exclude_unset=True),
    )


@router.delete("/brands/{brand_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_brand_route(
    brand_id: str,
    cascade: bool = Query(default=False),
):
    _call(brand_repo.delete_brand, brand_id, cascade=cascade)
    return None


# ---------- 产品路由 ----------


@router.get("/brands/{brand_id}/products")
def list_products_route(brand_id: str):
    return {"products": _call(brand_repo.list_products, brand_id)}


@router.post(
    "/brands/{brand_id}/products",
    status_code=status.HTTP_201_CREATED,
)
def create_product_route(brand_id: str, payload: ProductCreate = Body(...)):
    return _call(
        brand_repo.create_product,
        brand_id,
        payload.name,
        payload.selling_points,
        payload.associations,
    )


@router.patch("/brands/{brand_id}/products/{product_id}")
def update_product_route(
    brand_id: str,
    product_id: str,
    patch: ProductPatch = Body(...),
):
    return _call(
        brand_repo.update_product,
        brand_id,
        product_id,
        patch.model_dump(exclude_unset=True),
    )


@router.delete(
    "/brands/{brand_id}/products/{product_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_product_route(brand_id: str, product_id: str):
    _call(brand_repo.delete_product, brand_id, product_id)
    return None


# ---------- 联想词推荐（PR-2 接 LLM） ----------


class SuggestAssociationsPayload(BaseModel):
    product_name: str = Field(..., min_length=1, max_length=128)
    selling_points: list[str] = Field(default_factory=list)


@router.post("/brands/{brand_id}/suggest-associations")
def suggest_associations_route(
    brand_id: str,
    payload: SuggestAssociationsPayload = Body(...),
):
    brand = _call(brand_repo.get_brand, brand_id)
    return {
        "suggestions": _call(
            brand_repo.suggest_associations,
            brand["name"],
            payload.product_name,
            payload.selling_points,
        )
    }


# =================== PR-2b ===================
# Upload / Jobs / Runner 拼装


ENV_UPLOAD_DIR = "AUTOCUT_UPLOAD_DIR"
ENV_RUNS_DIR = "AUTOCUT_RUNS_DIR"
ENV_MAX_UPLOAD_BYTES = "AUTOCUT_MAX_UPLOAD_BYTES"
DEFAULT_MAX_UPLOAD_BYTES = 2 * 1024 * 1024 * 1024  # 2 GiB

_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _upload_dir() -> Path:
    return Path(
        os.environ.get(ENV_UPLOAD_DIR, Path.cwd() / "data" / "uploads")
    ).resolve()


def _runs_dir() -> Path:
    return Path(
        os.environ.get(ENV_RUNS_DIR, Path.cwd() / "data" / "runs")
    ).resolve()


def _max_upload_bytes() -> int:
    raw = os.environ.get(ENV_MAX_UPLOAD_BYTES, "")
    if raw.strip().isdigit():
        return int(raw)
    return DEFAULT_MAX_UPLOAD_BYTES


def _safe_filename(name: str) -> str:
    stem = Path(name).name  # 去目录
    safe = _SAFE_NAME_RE.sub("_", stem).strip("._-") or "upload"
    return safe[:128]


# ---------- Upload ----------


@router.post("/upload", status_code=status.HTTP_201_CREATED)
async def upload_route(file: UploadFile = File(...)):
    """multipart 上传视频文件，落盘到 AUTOCUT_UPLOAD_DIR。

    校验：
      - 扩展名必须落在 oss.ALLOWED_VIDEO_EXTENSIONS
      - 大小 ≤ AUTOCUT_MAX_UPLOAD_BYTES（默认 2GiB）
    返回：{"uploaded_path": str, "filename": str, "size": int}
    """
    raw_name = file.filename or "upload"
    safe = _safe_filename(raw_name)
    ext = Path(safe).suffix.lower()
    if ext not in oss.ALLOWED_VIDEO_EXTENSIONS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"extension_not_allowed: {ext or '(empty)'}",
        )

    upload_dir = _upload_dir()
    upload_dir.mkdir(parents=True, exist_ok=True)
    # 防冲突：加随机后缀
    token = secrets.token_hex(4)
    dest = upload_dir / f"{Path(safe).stem}_{token}{ext}"
    max_bytes = _max_upload_bytes()

    written = 0
    try:
        with dest.open("wb") as fh:
            while True:
                chunk = await file.read(1 << 20)  # 1 MiB
                if not chunk:
                    break
                written += len(chunk)
                if written > max_bytes:
                    raise HTTPException(
                        status_code=413,  # Content Too Large
                        detail=f"too_large: streamed_bytes>{max_bytes}",
                    )
                fh.write(chunk)
    except HTTPException:
        dest.unlink(missing_ok=True)
        raise
    except Exception as exc:
        dest.unlink(missing_ok=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"upload_failed: {exc}",
        ) from exc
    finally:
        await file.close()

    return {
        "uploaded_path": str(dest),
        "filename": safe,
        "size": written,
    }


# ---------- Jobs payload ----------


class SourceSpec(BaseModel):
    type: str = Field(..., pattern=r"^(upload|url|oss)$")
    value: Optional[str] = None
    uploaded_path: Optional[str] = None


class BrandProductSpec(BaseModel):
    """三选一：(brand_id, product_id) / (brand_id 仅) / 临时 product/selling_points。"""

    brand_id: Optional[str] = None
    product_id: Optional[str] = None
    product: Optional[str] = Field(default=None, max_length=128)
    selling_points: list[str] = Field(default_factory=list)
    extra_terms: list[str] = Field(default_factory=list)


class RemixSpec(BaseModel):
    target_duration: float = 25.0
    use_llm: bool = True
    stream: bool = False
    model: Optional[str] = None


class JobCreate(BaseModel):
    source: SourceSpec
    brand_product: BrandProductSpec
    tracks: list[str] = Field(default_factory=lambda: ["enabled", "remix"])
    remix: RemixSpec = Field(default_factory=RemixSpec)

    # 可选 ASR 覆盖
    asr_engine: str = "transcript"
    asr_model: str = "small"
    asr_device: str = "cpu"
    asr_compute_type: str = "int8"
    asr_beam_size: int = 5
    transcript_path: Optional[str] = None


# ---------- Runner ----------


def _resolve_brand_product(
    spec: BrandProductSpec,
) -> tuple[str, list[str], list[str]]:
    """返回 (product_name, selling_points, brand_terms)。

    优先级：
      1. brand_id + product_id      → repo 读取 + merge_terms
      2. brand_id only              → brand.name 作为产品（兜底），merge_terms 用 brand
      3. 全无                       → 用入参 product/selling_points + extra_terms
    """
    extra = list(spec.extra_terms or [])
    if spec.brand_id and spec.product_id:
        brand_repo.get_brand(spec.brand_id)  # 验证品牌存在；失败抛 NotFound
        products = brand_repo.list_products(spec.brand_id)
        product = next(
            (p for p in products if p["id"] == spec.product_id), None
        )
        if product is None:
            raise brand_repo.NotFound(f"product_not_found: {spec.product_id}")
        terms = brand_repo.merge_terms(spec.brand_id, spec.product_id, extra)
        return product["name"], list(product.get("selling_points", [])), terms

    if spec.brand_id:
        brand_repo.get_brand(spec.brand_id)  # 仅验证存在
        if not spec.product:
            raise brand_repo.BrandRepoError("product_required_when_no_product_id")
        terms = brand_repo.merge_terms(spec.brand_id, None, extra)
        return spec.product, list(spec.selling_points), terms

    if not spec.product:
        raise brand_repo.BrandRepoError("product_required")
    terms = brand_repo.merge_terms(None, None, extra)
    return spec.product, list(spec.selling_points), terms


def _make_runner(payload: JobCreate, runs_root: Path):
    """构造闭包 runner。延迟导入 pipeline/remix 避免业务路由模块在启动期吃 ASR 依赖。"""

    def runner(job: jobs.Job) -> dict[str, Any]:
        from .llm import LLMError, generate_ordered_ids
        from .models import PipelineRequest
        from .pipeline import LiveClipPipeline
        from .remix import (
            build_remix_source,
            export_remix_plan,
            remix_plan_from_ordered_ids,
            write_remix_plan,
        )

        run_dir = runs_root / job.id
        run_dir.mkdir(parents=True, exist_ok=True)
        artifacts: dict[str, Any] = {"run_id": job.id, "run_dir": str(run_dir)}

        # 1) 解析视频源
        jobs.update_status(
            job, status="downloading", stage="解析视频源", progress=0.05
        )
        source_dict = payload.source.model_dump(exclude_none=True)
        try:
            video_path = oss.resolve_video_source(
                source_dict,
                run_dir / "source",
                on_progress=lambda done, total: jobs.update_status(
                    job,
                    stage=f"下载中 {done}/{total or '?'} bytes",
                    progress=0.05 + min(0.15, (done / total) * 0.15) if total else 0.1,
                ),
            )
        except oss.OssError as exc:
            jobs.log_event(job.id, "ERROR", "download", str(exc))
            raise
        artifacts["source_path"] = str(video_path)
        jobs.update_status(job, artifacts=artifacts)

        # 2) 解析品牌/产品 + 联想词
        try:
            product_name, selling_points, brand_terms = _resolve_brand_product(
                payload.brand_product
            )
        except brand_repo.BrandRepoError as exc:
            jobs.log_event(job.id, "ERROR", "brand", str(exc))
            raise

        # 3) 跑 pipeline（enabled 轨道）
        tracks_result: dict[str, str] = {}
        if "enabled" in payload.tracks:
            jobs.update_status(
                job, status="running", stage="跑 pipeline（enabled）", progress=0.2
            )
            req = PipelineRequest(
                video_path=video_path,
                product=product_name,
                selling_points=selling_points,
                output_dir=run_dir,
                transcript_path=(
                    Path(payload.transcript_path) if payload.transcript_path else None
                ),
                asr_engine=payload.asr_engine,
                asr_model=payload.asr_model,
                asr_device=payload.asr_device,
                asr_compute_type=payload.asr_compute_type,
                asr_beam_size=payload.asr_beam_size,
                brand_terms=brand_terms,
            )
            try:
                LiveClipPipeline().run(req)
                tracks_result["enabled"] = "ok"
                exports_dir = run_dir / "exports"
                if exports_dir.exists():
                    mp4s = sorted(exports_dir.glob("*.mp4"))
                    if mp4s:
                        artifacts["enabled_mp4"] = str(mp4s[0])
                jobs.update_status(
                    job,
                    progress=0.6,
                    stage="enabled 轨道完成",
                    tracks_result=tracks_result,
                    artifacts=artifacts,
                )
            except Exception:
                tracks_result["enabled"] = "failed"
                jobs.update_status(job, tracks_result=tracks_result)
                raise

        # 4) 跑 remix 轨道
        if "remix" in payload.tracks:
            jobs.update_status(
                job,
                status="running",
                stage="跑 remix 25s",
                progress=0.7,
                tracks_result=tracks_result,
            )
            result_path = run_dir / "metadata" / "result.json"
            request_path = run_dir / "metadata" / "request.json"
            if not result_path.exists() or not request_path.exists():
                tracks_result["remix"] = "failed"
                jobs.update_status(job, tracks_result=tracks_result)
                raise RuntimeError("remix_missing_metadata")
            result = json.loads(result_path.read_text(encoding="utf-8"))
            request = json.loads(request_path.read_text(encoding="utf-8"))
            try:
                remix_source = build_remix_source(
                    result, request, target_duration=payload.remix.target_duration
                )
                ordered_ids: list[str] = []
                if payload.remix.use_llm:
                    try:
                        model_result = generate_ordered_ids(
                            remix_source["prompt"],
                            stream=payload.remix.stream,
                            model=payload.remix.model,
                        )
                        ordered_ids = model_result.get("ordered_ids", [])
                    except LLMError as exc:
                        jobs.log_event(job.id, "WARN", "remix", f"llm_failed: {exc}")
                if ordered_ids:
                    plan = remix_plan_from_ordered_ids(
                        remix_source["units"],
                        ordered_ids,
                        target_duration=payload.remix.target_duration,
                    )
                else:
                    plan = remix_source["default_plan"]
                if not plan.get("items"):
                    raise RuntimeError("remix_no_items")
                exports_dir = run_dir / "exports"
                exports_dir.mkdir(parents=True, exist_ok=True)
                output_path = exports_dir / f"{job.id}_remix_25s.mp4"
                warning = export_remix_plan(result, plan, output_path)
                if warning:
                    raise RuntimeError(f"remix_export_warning: {warning}")
                plan_path = run_dir / "metadata" / f"{output_path.stem}_plan.json"
                write_remix_plan(plan_path, {**plan, "video_path": str(output_path)})
                tracks_result["remix"] = "ok"
                artifacts["remix_mp4"] = str(output_path)
                jobs.update_status(
                    job,
                    progress=0.95,
                    stage="remix 完成",
                    tracks_result=tracks_result,
                    artifacts=artifacts,
                )
            except Exception:
                tracks_result["remix"] = "failed"
                jobs.update_status(job, tracks_result=tracks_result, artifacts=artifacts)
                # 让 jobs._classify_failure 通过模块名识别为 remix
                exc_t, exc_v, _ = __import__("sys").exc_info()
                if exc_t is not None:
                    # 包一层 RuntimeError 但保留 __module__ 提示
                    new_exc = RuntimeError(f"remix_failed: {exc_v}")
                    new_exc.__class__.__module__ = "autocut.remix"
                    raise new_exc from exc_v
                raise

        jobs.update_status(
            job, artifacts=artifacts, tracks_result=tracks_result, progress=1.0
        )
        return artifacts

    return runner


# ---------- Jobs routes ----------


def _job_to_dict(job: jobs.Job) -> dict[str, Any]:
    return {
        "id": job.id,
        "status": job.status,
        "stage": job.stage,
        "progress": job.progress,
        "queued_at": job.queued_at,
        "started_at": job.started_at,
        "finished_at": job.finished_at,
        "updated_at": job.updated_at,
        "tracks": job.tracks,
        "tracks_result": job.tracks_result,
        "artifacts": job.artifacts,
        "error": job.error,
        "failure_kind": job.failure_kind,
    }


@router.post("/jobs", status_code=status.HTTP_201_CREATED)
def create_job_route(payload: JobCreate = Body(...)):
    # 先把品牌/产品入参验一下，让 422/404/400 立即返回，而非异步失败
    try:
        _resolve_brand_product(payload.brand_product)
    except brand_repo.BrandRepoError as exc:
        raise _map_repo_exc(exc) from exc

    runs_root = _runs_dir()
    runs_root.mkdir(parents=True, exist_ok=True)
    runner = _make_runner(payload, runs_root)
    job_id = jobs.enqueue(
        payload.model_dump(),
        runner,
        tracks=list(payload.tracks),
    )
    job = jobs.get_job(job_id)
    return _job_to_dict(job)  # type: ignore[arg-type]


@router.get("/jobs")
def list_jobs_route(limit: int = Query(default=50, ge=1, le=200)):
    return {"jobs": [_job_to_dict(j) for j in jobs.list_jobs(limit=limit)]}


@router.get("/jobs/{job_id}")
def get_job_route(job_id: str):
    job = jobs.get_job(job_id)
    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"job_not_found: {job_id}"
        )
    return _job_to_dict(job)


@router.get("/jobs/{job_id}/log")
def get_job_log_route(
    job_id: str, tail: int = Query(default=200, ge=1, le=2000)
):
    job = jobs.get_job(job_id)
    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"job_not_found: {job_id}"
        )
    return {"lines": jobs.read_log(job_id, tail=tail)}


# ---------- Lifespan hooks（api.py 调用） ----------


def lifespan_startup() -> dict[str, Any]:
    """供 api.py lifespan 调用：恢复 orphan + 启 worker。返回汇报字典。"""
    recovered = jobs.recover_orphaned_jobs()
    jobs.start_worker()
    return {
        "recovered_jobs": recovered,
        "worker_running": jobs.is_worker_running(),
        "started_at": time.time(),
    }


def lifespan_shutdown(timeout: float = 5.0) -> None:
    jobs.stop_worker(timeout=timeout)
