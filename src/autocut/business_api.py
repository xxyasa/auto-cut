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

import asyncio
import json
import os
import re
import secrets
import shutil
import tempfile
import time
import zipfile
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
    from fastapi.responses import FileResponse
    from pydantic import BaseModel, Field
    from starlette.background import BackgroundTask
except ImportError as exc:  # pragma: no cover
    raise RuntimeError(
        "Install API dependencies with: pip install -e '.[api]'"
    ) from exc

from . import brand_repo, jobs, media, oss
from .auth import require_token
from .exporter import export_segments_zip
from .remix import remix_export_segments, score_remix_plan

MAX_REMIX_PLANS = 5


class RemixTrackError(RuntimeError):
    """Raised when the optional remix track fails after the main track ran."""


def _auto_remix_plan_count(video_path: Path) -> tuple[int, float | None]:
    """Return automatic remix plan count from source duration."""
    try:
        duration = media.probe_video(video_path).duration
    except Exception:
        return 1, None
    if duration is None:
        return 1, None
    if duration <= 4 * 60:
        return 1, duration
    if duration <= 6 * 60:
        return 2, duration
    return 3, duration


# ---------- Pydantic 模型 ----------


class BrandCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    associations: list[str] = Field(default_factory=list, max_length=200)


class BrandPatch(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=128)
    associations: Optional[list[str]] = Field(default=None, max_length=200)


class ProductCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    selling_points: list[str] = Field(default_factory=list, max_length=200)
    associations: list[str] = Field(default_factory=list, max_length=200)


class ProductPatch(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=128)
    selling_points: Optional[list[str]] = Field(default=None, max_length=200)
    associations: Optional[list[str]] = Field(default=None, max_length=200)


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
    selling_points: list[str] = Field(default_factory=list, max_length=200)


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
    loop = asyncio.get_event_loop()
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
                # 异步写磁盘，避免阻塞 event loop
                await loop.run_in_executor(None, fh.write, chunk)
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
    selling_points: list[str] = Field(default_factory=list, max_length=200)
    extra_terms: list[str] = Field(default_factory=list, max_length=200)


class RemixSpec(BaseModel):
    target_duration: float = 25.0
    use_llm: bool = True
    stream: bool = False
    model: Optional[str] = None
    plan_count: int = Field(default=1, ge=1, le=5)
    plan_count_auto: bool = False


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


class RemixPlanCreate(BaseModel):
    stream: bool = False
    model: Optional[str] = None


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
                on_progress=lambda stage, pct: jobs.update_status(
                    job,
                    stage=stage,
                    progress=round(0.2 + pct * 0.4, 3),  # pipeline 占总进度 20%~60%
                ),
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
                stage=f"跑 remix {payload.remix.target_duration:.0f}s",
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
                exports_dir = run_dir / "exports"
                exports_dir.mkdir(parents=True, exist_ok=True)
                remix_plans: list[dict[str, Any]] = []
                seen_orders: set[tuple[str, ...]] = set()
                source_duration: float | None = None
                if payload.remix.use_llm and payload.remix.plan_count_auto:
                    plan_count, source_duration = _auto_remix_plan_count(video_path)
                    jobs.log_event(
                        job.id,
                        "INFO",
                        "remix",
                        f"auto_plan_count={plan_count}"
                        + (
                            f" source_duration={source_duration:.1f}s"
                            if source_duration is not None
                            else " source_duration=unknown"
                        ),
                    )
                else:
                    plan_count = (
                        min(MAX_REMIX_PLANS, payload.remix.plan_count)
                        if payload.remix.use_llm
                        else 1
                    )
                artifacts["remix_plan_count"] = plan_count
                artifacts["remix_plan_count_auto"] = bool(
                    payload.remix.use_llm and payload.remix.plan_count_auto
                )
                if source_duration is not None:
                    artifacts["source_duration"] = round(source_duration, 3)
                jobs.update_status(job, artifacts=artifacts)
                for plan_index in range(1, plan_count + 1):
                    jobs.update_status(
                        job,
                        stage=f"生成成片预览方案 {plan_index}/{plan_count}",
                        progress=round(0.7 + (plan_index - 1) / max(plan_count, 1) * 0.2, 3),
                        tracks_result=tracks_result,
                    )
                    ordered_ids: list[str] = []
                    model_result: dict[str, Any] | None = None
                    if payload.remix.use_llm:
                        try:
                            model_result = generate_ordered_ids(
                                _variant_prompt(remix_source["prompt"], plan_index, plan_count, seen_orders),
                                stream=payload.remix.stream,
                                model=payload.remix.model,
                            )
                            ordered_ids = model_result.get("ordered_ids", [])
                        except LLMError as exc:
                            jobs.log_event(job.id, "WARN", "remix", f"llm_failed_v{plan_index}: {exc}")
                    if ordered_ids:
                        plan = remix_plan_from_ordered_ids(
                            remix_source["units"],
                            ordered_ids,
                            target_duration=payload.remix.target_duration,
                        )
                        if model_result:
                            plan["model_reason"] = model_result.get("reason", "")
                            plan["model_raw_content"] = model_result.get("raw_content", "")
                    else:
                        plan = remix_source["default_plan"]
                    if not plan.get("items"):
                        raise RuntimeError("remix_no_items")
                    order_key = tuple(str(item) for item in plan.get("ordered_ids") or [])
                    if order_key in seen_orders:
                        jobs.log_event(
                            job.id,
                            "WARN",
                            "remix",
                            f"duplicate_plan_skipped_v{plan_index}: {','.join(order_key)}",
                        )
                        continue
                    plan["variant_index"] = plan_index
                    plan["variant_count"] = plan_count
                    plan["duplicate"] = False
                    plan["quality"] = score_remix_plan(
                        plan,
                        request,
                        target_duration=payload.remix.target_duration,
                    )
                    seen_orders.add(order_key)
                    output_path = exports_dir / f"{job.id}_remix_{int(round(payload.remix.target_duration))}s_v{plan_index}.mp4"
                    warning = export_remix_plan(result, plan, output_path)
                    if warning:
                        raise RuntimeError(f"remix_export_warning: {warning}")
                    plan_path = run_dir / "metadata" / f"{output_path.stem}_plan.json"
                    write_remix_plan(plan_path, {**plan, "video_path": str(output_path)})
                    remix_plans.append(
                        {
                            "index": plan_index,
                            "label": f"方案 {plan_index}",
                            "mp4": str(output_path),
                            "plan": str(plan_path),
                            "duration": plan.get("duration"),
                            "script_text": plan.get("script_text", ""),
                            "quality": plan.get("quality", {}),
                            "ordered_ids": plan.get("ordered_ids", []),
                            "duplicate": bool(plan.get("duplicate")),
                        }
                    )
                if not remix_plans:
                    raise RuntimeError("remix_no_unique_plans")
                tracks_result["remix"] = "ok"
                artifacts["remix_plans"] = remix_plans
                if remix_plans:
                    artifacts["remix_mp4"] = remix_plans[0]["mp4"]
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
                    raise RemixTrackError(f"remix_failed: {exc_v}") from exc_v
                raise

        jobs.update_status(
            job, artifacts=artifacts, tracks_result=tracks_result, progress=1.0
        )
        return artifacts

    return runner


# ---------- Jobs routes ----------


def _job_to_dict(job: jobs.Job) -> dict[str, Any]:
    artifacts = _artifacts_with_remix_scripts(job)
    display_name = _job_display_name(job, artifacts)
    brand_name, product_name = _job_brand_product_names(job, artifacts)
    return {
        "id": job.id,
        "display_name": display_name,
        "brand_name": brand_name,
        "product_name": product_name,
        "downloadable": _job_has_remix_segments(job),
        "status": job.status,
        "stage": job.stage,
        "progress": job.progress,
        "queued_at": job.queued_at,
        "started_at": job.started_at,
        "finished_at": job.finished_at,
        "updated_at": job.updated_at,
        "tracks": job.tracks,
        "tracks_result": job.tracks_result,
        "artifacts": artifacts,
        "error": job.error,
        "failure_kind": job.failure_kind,
    }


def _job_display_name(job: jobs.Job, artifacts: dict[str, Any] | None = None) -> str:
    artifacts = artifacts or job.artifacts or {}
    for key in ("display_name", "product_display_name"):
        value = str(artifacts.get(key) or "").strip()
        if value:
            return value
    value = str((job.request or {}).get("display_name") or "").strip()
    if value:
        return value
    brand_product = (job.request or {}).get("brand_product")
    if isinstance(brand_product, dict):
        return _brand_product_display_name(brand_product)
    return ""


def _job_brand_product_names(job: jobs.Job, artifacts: dict[str, Any] | None = None) -> tuple[str, str]:
    artifacts = artifacts or job.artifacts or {}
    brand_name = str(artifacts.get("brand_name") or "").strip()
    product_name = str(artifacts.get("product_name") or "").strip()
    brand_product = (job.request or {}).get("brand_product")
    if isinstance(brand_product, dict):
        resolved_brand, resolved_product = _brand_product_names(brand_product)
        brand_name = brand_name or resolved_brand
        product_name = product_name or resolved_product
    if not product_name:
        display_name = str((job.request or {}).get("display_name") or artifacts.get("display_name") or "").strip()
        if display_name:
            parts = display_name.split("-", 1)
            if len(parts) == 2 and not brand_name:
                brand_name, product_name = parts[0], parts[1]
            else:
                product_name = display_name
    return brand_name, product_name


def _brand_product_display_name(brand_product: dict[str, Any] | BrandProductSpec) -> str:
    brand_name, product_name = _brand_product_names(brand_product)
    parts = [part for part in (brand_name, product_name) if part]
    return "-".join(parts)


def _brand_product_names(brand_product: dict[str, Any] | BrandProductSpec) -> tuple[str, str]:
    if isinstance(brand_product, BrandProductSpec):
        spec = brand_product.model_dump()
    else:
        spec = dict(brand_product)

    brand_name = ""
    product_name = str(spec.get("product") or "").strip()
    brand_id = str(spec.get("brand_id") or "").strip()
    product_id = str(spec.get("product_id") or "").strip()

    if brand_id:
        try:
            brand = brand_repo.get_brand(brand_id)
            brand_name = str(brand.get("name") or "").strip()
            if product_id:
                product = next(
                    (p for p in brand_repo.list_products(brand_id) if p.get("id") == product_id),
                    None,
                )
                if product:
                    product_name = str(product.get("name") or "").strip()
        except brand_repo.BrandRepoError:
            pass

    return brand_name, product_name


def _artifacts_with_remix_scripts(job: jobs.Job) -> dict[str, Any]:
    artifacts = dict(job.artifacts or {})
    plans = artifacts.get("remix_plans")
    if not isinstance(plans, list):
        return artifacts

    metadata_dir = (_runs_dir() / job.id / "metadata").resolve()
    enriched_plans: list[dict[str, Any]] = []
    for plan in plans:
        if not isinstance(plan, dict):
            enriched_plans.append(plan)
            continue
        enriched = dict(plan)
        if not enriched.get("script_text") or not enriched.get("quality"):
            plan_data = _read_remix_plan_data(enriched.get("plan"), metadata_dir)
            if plan_data:
                if not enriched.get("script_text"):
                    script_text = _remix_plan_script_text(plan_data)
                    if script_text:
                        enriched["script_text"] = script_text
                if not enriched.get("quality"):
                    enriched["quality"] = score_remix_plan(
                        plan_data,
                        job.request,
                        target_duration=plan_data.get("target_duration"),
                    )
        enriched_plans.append(enriched)
    artifacts["remix_plans"] = enriched_plans
    return artifacts


def _read_remix_plan_data(plan_path_value: Any, metadata_dir: Path) -> dict[str, Any]:
    if not plan_path_value:
        return {}
    try:
        plan_path = Path(str(plan_path_value)).resolve()
        plan_path.relative_to(metadata_dir)
        data = json.loads(plan_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _remix_plan_script_text(plan: dict[str, Any]) -> str:
    script_text = str(plan.get("script_text") or "").strip()
    if script_text:
        return script_text
    items = plan.get("items")
    if isinstance(items, list):
        return "\n".join(
            str(item.get("text") or item.get("clean_text") or item.get("raw_text") or "").strip()
            for item in items
            if isinstance(item, dict)
            and str(item.get("text") or item.get("clean_text") or item.get("raw_text") or "").strip()
        )
    return ""


@router.post("/jobs", status_code=status.HTTP_201_CREATED)
def create_job_route(payload: JobCreate = Body(...)):
    _ensure_worker_running()
    # 先把品牌/产品入参验一下，让 422/404/400 立即返回，而非异步失败
    try:
        _resolve_brand_product(payload.brand_product)
    except brand_repo.BrandRepoError as exc:
        raise _map_repo_exc(exc) from exc

    runs_root = _runs_dir()
    runs_root.mkdir(parents=True, exist_ok=True)
    runner = _make_runner(payload, runs_root)
    request_payload = payload.model_dump()
    request_payload["display_name"] = _brand_product_display_name(payload.brand_product)
    job_id = jobs.enqueue(
        request_payload,
        runner,
        tracks=list(payload.tracks),
    )
    job = jobs.get_job(job_id)
    return _job_to_dict(job)  # type: ignore[arg-type]


@router.get("/jobs")
def list_jobs_route(limit: int = Query(default=50, ge=1, le=200)):
    _ensure_worker_running()
    return {"jobs": [_job_to_dict(j) for j in jobs.list_jobs(limit=limit)]}


@router.post("/jobs/{job_id}/remix-plans")
def create_remix_plan_route(
    job_id: str,
    payload: RemixPlanCreate | None = Body(default=None),
):
    payload = payload or RemixPlanCreate()
    job = jobs.get_job(job_id)
    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"job_not_found: {job_id}",
        )
    if job.status not in {"done", "partial_success"}:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="job_not_finished",
        )
    existing_plans = [
        plan for plan in (job.artifacts.get("remix_plans") or [])
        if isinstance(plan, dict)
    ]
    if len(existing_plans) >= MAX_REMIX_PLANS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="remix_plan_limit_reached",
        )

    try:
        plan_summary = _append_remix_plan(job, payload)
    except HTTPException:
        raise
    except Exception as exc:
        jobs.log_event(job.id, "ERROR", "remix", f"append_remix_plan_failed: {exc}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"append_remix_plan_failed: {exc}",
        ) from exc

    updated_plans = [*existing_plans, plan_summary]
    artifacts = {**job.artifacts, "remix_plans": updated_plans}
    if updated_plans and not artifacts.get("remix_mp4"):
        artifacts["remix_mp4"] = updated_plans[0].get("mp4")
    jobs.update_status(
        job,
        stage=f"已生成新成片方案 {plan_summary['index']}",
        artifacts=artifacts,
        tracks_result={"remix": "ok"},
    )
    jobs.log_event(job.id, "INFO", "remix", f"append remix plan {plan_summary['index']}")
    refreshed = jobs.get_job(job_id)
    return _job_to_dict(refreshed or job)


@router.get("/jobs/batch-remix-segments.zip")
def batch_remix_segments_zip_route(ids: str = Query(..., min_length=1)):
    job_ids = [item.strip() for item in ids.split(",") if item.strip()]
    if not job_ids:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="job_ids_required",
        )
    if len(job_ids) > 50:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="too_many_jobs: max 50",
        )
    if any(not re.fullmatch(r"[A-Za-z0-9_\-]+", job_id) for job_id in job_ids):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="invalid_job_id",
        )

    tmp = tempfile.NamedTemporaryFile(
        prefix="autocut_batch_remix_",
        suffix=".zip",
        delete=False,
    )
    tmp_path = Path(tmp.name)
    tmp.close()

    try:
        written_jobs = _generate_batch_remix_segments_zip(job_ids, tmp_path)
    except HTTPException:
        tmp_path.unlink(missing_ok=True)
        raise
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise

    if written_jobs <= 0:
        tmp_path.unlink(missing_ok=True)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="no_downloadable_jobs",
        )

    filename = f"autocut_batch_remix_segments_{int(time.time())}.zip"
    return FileResponse(
        tmp_path,
        media_type="application/zip",
        filename=filename,
        background=BackgroundTask(lambda: tmp_path.unlink(missing_ok=True)),
    )


@router.get("/jobs/{job_id}")
def get_job_route(job_id: str):
    _ensure_worker_running()
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


# ---------- Artifacts (MP4 预览/下载) ----------


_SAFE_ARTIFACT_RE = re.compile(r"^[A-Za-z0-9._\-]+$")


@router.get("/jobs/{job_id}/artifacts/{name}")
def get_job_artifact_route(job_id: str, name: str):
    """从 runs_root/<job_id>/exports/<name> 返回工件文件。

    安全：
      - name 必须匹配 [A-Za-z0-9._-]+，禁止 / .. 路径穿越
      - 仅允许 .mp4 / .json 后缀
      - 必须 resolve 后仍位于 exports/ 目录内
    """
    job = jobs.get_job(job_id)
    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"job_not_found: {job_id}"
        )
    if not _SAFE_ARTIFACT_RE.match(name):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"invalid_artifact_name: {name}",
        )
    ext = Path(name).suffix.lower()
    if ext not in {".mp4", ".json", ".zip"}:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"artifact_extension_not_allowed: {ext}",
        )
    runs_root = _runs_dir()
    exports_dir = (runs_root / job_id / "exports").resolve()
    target = (exports_dir / name).resolve()
    try:
        target.relative_to(exports_dir)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="path_traversal_blocked",
        ) from exc
    if not target.exists() or not target.is_file():
        warning = _maybe_generate_segments_zip(job_id, name, exports_dir)
        if warning:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=warning,
            )
    if not target.exists() or not target.is_file():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"artifact_not_found: {name}",
        )

    media_type = {
        ".mp4": "video/mp4",
        ".json": "application/json",
        ".zip": "application/zip",
    }.get(ext, "application/octet-stream")
    return FileResponse(target, media_type=media_type, filename=name)


def _maybe_generate_segments_zip(job_id: str, name: str, exports_dir: Path) -> str | None:
    if name == f"{job_id}_enabled_segments.zip":
        return _generate_enabled_segments_zip(job_id, exports_dir / name)
    if name == f"{job_id}_all_plans_segments.zip":
        return _generate_all_plans_segments_zip(job_id, exports_dir / name)
    if name == f"{job_id}_remix_segments.zip":
        return _generate_remix_segments_zip(job_id, exports_dir / name, variant_index=1)
    match = re.fullmatch(rf"{re.escape(job_id)}_remix_v([1-5])_segments\.zip", name)
    if match:
        return _generate_remix_segments_zip(
            job_id,
            exports_dir / name,
            variant_index=int(match.group(1)),
        )
    return None


def _append_remix_plan(job: jobs.Job, payload: RemixPlanCreate) -> dict[str, Any]:
    from .llm import LLMError, generate_ordered_ids
    from .remix import (
        build_remix_source,
        export_remix_plan,
        remix_plan_from_ordered_ids,
        write_remix_plan,
    )

    run_dir = _runs_dir() / job.id
    result_path = run_dir / "metadata" / "result.json"
    request_path = run_dir / "metadata" / "request.json"
    if not result_path.exists() or not request_path.exists():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="remix_missing_metadata",
        )

    result = json.loads(result_path.read_text(encoding="utf-8"))
    request = json.loads(request_path.read_text(encoding="utf-8"))
    existing_plans = [
        plan for plan in (job.artifacts.get("remix_plans") or [])
        if isinstance(plan, dict)
    ]
    used_indices = {
        int(plan.get("index"))
        for plan in existing_plans
        if str(plan.get("index") or "").isdigit()
    }
    next_index = next(
        (index for index in range(1, MAX_REMIX_PLANS + 1) if index not in used_indices),
        len(existing_plans) + 1,
    )
    if next_index > MAX_REMIX_PLANS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="remix_plan_limit_reached",
        )

    remix_request = job.request.get("remix") if isinstance(job.request, dict) else {}
    target_duration = _float_from_request(
        remix_request.get("target_duration") if isinstance(remix_request, dict) else None,
        fallback=25.0,
    )
    remix_source = build_remix_source(result, request, target_duration=target_duration)
    seen_orders = {
        tuple(str(item) for item in plan.get("ordered_ids") or [])
        for plan in existing_plans
        if plan.get("ordered_ids")
    }
    model_result = generate_ordered_ids(
        _variant_prompt(remix_source["prompt"], next_index, MAX_REMIX_PLANS, seen_orders),
        stream=payload.stream,
        model=payload.model,
    )
    ordered_ids = model_result.get("ordered_ids", [])
    if not ordered_ids:
        raise LLMError("model response ordered_ids is empty")

    plan = remix_plan_from_ordered_ids(
        remix_source["units"],
        ordered_ids,
        target_duration=target_duration,
    )
    if not plan.get("items"):
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="remix_no_items",
        )
    plan["model_reason"] = model_result.get("reason", "")
    plan["model_raw_content"] = model_result.get("raw_content", "")
    plan["variant_index"] = next_index
    plan["variant_count"] = MAX_REMIX_PLANS
    plan["duplicate"] = tuple(str(item) for item in plan.get("ordered_ids") or []) in seen_orders
    plan["quality"] = score_remix_plan(plan, request, target_duration=target_duration)

    exports_dir = run_dir / "exports"
    exports_dir.mkdir(parents=True, exist_ok=True)
    output_path = exports_dir / f"{job.id}_remix_{int(round(target_duration))}s_v{next_index}.mp4"
    warning = export_remix_plan(result, plan, output_path)
    if warning:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"remix_export_warning: {warning}",
        )
    plan_path = run_dir / "metadata" / f"{output_path.stem}_plan.json"
    write_remix_plan(plan_path, {**plan, "video_path": str(output_path)})
    return {
        "index": next_index,
        "label": f"方案 {next_index}",
        "mp4": str(output_path),
        "plan": str(plan_path),
        "duration": plan.get("duration"),
        "script_text": plan.get("script_text", ""),
        "quality": plan.get("quality", {}),
        "ordered_ids": plan.get("ordered_ids", []),
        "duplicate": bool(plan.get("duplicate")),
    }


def _float_from_request(value: Any, *, fallback: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return fallback
    return parsed if parsed > 0 else fallback


def _job_has_remix_segments(job: jobs.Job) -> bool:
    if job.status not in {"done", "partial_success"}:
        return False
    if job.tracks_result.get("remix") == "failed":
        return False
    run_dir = _runs_dir() / job.id
    if not (run_dir / "metadata" / "result.json").exists():
        return False
    return any(_remix_plan_paths(run_dir, job.id, index) for index in range(1, 6))


def _generate_batch_remix_segments_zip(job_ids: list[str], output_path: Path) -> int:
    from . import media
    from .exporter import _normalize_zip_segments, _segment_entry_name

    output_path.unlink(missing_ok=True)
    written_jobs = 0
    used_folders: set[str] = set()

    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_root = Path(tmp_dir)
        with zipfile.ZipFile(output_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for job_id in job_ids:
                job = jobs.get_job(job_id)
                if job is None or not _job_has_remix_segments(job):
                    continue
                run_dir = _runs_dir() / job_id
                result_path = run_dir / "metadata" / "result.json"
                result = json.loads(result_path.read_text(encoding="utf-8"))
                source_video = Path(result.get("media", {}).get("path") or "").resolve()
                if not source_video.exists():
                    continue

                folder = _unique_zip_folder_name(
                    _safe_zip_folder_name(_job_display_name(job) or job_id),
                    used_folders,
                    suffix=job_id[:8],
                )
                job_written = False
                for plan_idx, variant_index in enumerate(_job_remix_variant_indices(run_dir, job_id), 1):
                    plan_paths = _remix_plan_paths(run_dir, job_id, variant_index)
                    if not plan_paths:
                        continue
                    plan = json.loads(plan_paths[0].read_text(encoding="utf-8"))
                    segments = _normalize_zip_segments(remix_export_segments(result, plan))
                    if not segments:
                        continue
                    plan_tmp_dir = tmp_root / folder / f"方案{plan_idx}"
                    plan_tmp_dir.mkdir(parents=True, exist_ok=True)
                    for segment_index, segment in enumerate(segments, 1):
                        entry_name = _segment_entry_name(segment_index, segment)
                        segment_path = plan_tmp_dir / entry_name
                        warning = media.export_clip(
                            source_video,
                            segment_path,
                            float(segment["start"]),
                            float(segment["end"]),
                        )
                        if warning:
                            raise HTTPException(
                                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                                detail=warning,
                            )
                        archive.write(segment_path, f"{folder}/方案{plan_idx}/{entry_name}")
                        job_written = True
                if job_written:
                    written_jobs += 1

    return written_jobs


def _job_remix_variant_indices(run_dir: Path, job_id: str) -> list[int]:
    return [
        index
        for index in range(1, 6)
        if _remix_plan_paths(run_dir, job_id, index)
    ]


def _safe_zip_folder_name(value: str) -> str:
    safe = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff _.-]+", "", value)
    safe = re.sub(r"\s+", "_", safe).strip("._- ")
    return (safe or "任务")[:80]


def _unique_zip_folder_name(base: str, used: set[str], *, suffix: str) -> str:
    candidate = f"{base}-{suffix}" if suffix else base
    original = candidate
    counter = 2
    while candidate in used:
        candidate = f"{original}-{counter}"
        counter += 1
    used.add(candidate)
    return candidate


def _variant_prompt(
    base_prompt: str,
    plan_index: int,
    plan_count: int,
    seen_orders: set[tuple[str, ...]],
) -> str:
    if plan_count <= 1:
        return base_prompt
    styles = {
        1: "常规爆款结构，兼顾开头吸引、中段卖点和结尾促单。",
        2: "更强调卖点密度，尽量覆盖更多功能、材质、场景信息。",
        3: "更强调产品外观、款式、使用场景和礼赠氛围。",
        4: "更强调优惠、保障、促单理由和成交转化。",
        5: "更强调差异化表达，避免和前面方案使用完全相同的句子顺序。",
    }
    used = [list(order) for order in seen_orders if order]
    extra = [
        "",
        f"多方案生成补充要求：这是第 {plan_index}/{plan_count} 个混剪方案。",
        styles.get(plan_index, styles[5]),
        "请尽量避免与前面方案完全相同的 ordered_ids 顺序；如果素材确实有限，也必须保证语义自然。",
    ]
    if used:
        extra.append(f"前面已经生成过的 ordered_ids 顺序：{json.dumps(used, ensure_ascii=False)}")
    return base_prompt + "\n" + "\n".join(extra)


def _generate_enabled_segments_zip(job_id: str, output_path: Path) -> str | None:
    run_dir = output_path.parent.parent
    result_path = run_dir / "metadata" / "result.json"
    if not result_path.exists():
        return None
    result = json.loads(result_path.read_text(encoding="utf-8"))
    source_video = Path(result.get("media", {}).get("path") or "").resolve()
    segments = [
        {
            "id": candidate.get("clip_id", ""),
            "start": candidate.get("start_time"),
            "end": candidate.get("end_time"),
            "summary": candidate.get("clean_transcript") or candidate.get("transcript") or candidate.get("reason") or "",
            "text": candidate.get("clean_transcript") or candidate.get("transcript") or "",
        }
        for candidate in result.get("candidates", [])
    ]
    warning = export_segments_zip(source_video, output_path, segments)
    if warning:
        output_path.unlink(missing_ok=True)
    return warning


def _generate_remix_segments_zip(
    job_id: str,
    output_path: Path,
    *,
    variant_index: int,
) -> str | None:
    run_dir = output_path.parent.parent
    result_path = run_dir / "metadata" / "result.json"
    if not result_path.exists():
        return None
    plan_paths = _remix_plan_paths(run_dir, job_id, variant_index)
    if not plan_paths:
        return None
    result = json.loads(result_path.read_text(encoding="utf-8"))
    plan = json.loads(plan_paths[0].read_text(encoding="utf-8"))
    source_video = Path(result.get("media", {}).get("path") or "").resolve()
    segments = remix_export_segments(result, plan)
    warning = export_segments_zip(source_video, output_path, segments)
    if warning:
        output_path.unlink(missing_ok=True)
    return warning


def _remix_plan_paths(run_dir: Path, job_id: str, variant_index: int) -> list[Path]:
    metadata_dir = run_dir / "metadata"
    exact = sorted(metadata_dir.glob(f"{job_id}_remix_*s_v{variant_index}_plan.json"))
    if exact:
        return exact
    if variant_index == 1:
        return sorted(
            metadata_dir.glob(f"{job_id}_remix_*s_plan.json"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
    return []


def _generate_all_plans_segments_zip(job_id: str, output_path: Path) -> str | None:
    """把所有混剪方案的片段合并到一个 ZIP，每个方案放在独立子目录 方案1/ 方案2/ ..."""
    run_dir = output_path.parent.parent
    result_path = run_dir / "metadata" / "result.json"
    if not result_path.exists():
        return "result.json not found"
    result = json.loads(result_path.read_text(encoding="utf-8"))
    source_video = Path(result.get("media", {}).get("path") or "").resolve()
    if not source_video.exists():
        return f"source video not found: {source_video}"

    # 收集所有方案的 variant_index（1~5）
    plan_indices: list[int] = []
    for i in range(1, 6):
        if _remix_plan_paths(run_dir, job_id, i):
            plan_indices.append(i)
    if not plan_indices:
        return "no remix plans found"

    temp_zip = output_path.with_suffix(output_path.suffix + ".tmp")
    temp_zip.unlink(missing_ok=True)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_root = Path(tmp_dir)
        try:
            with zipfile.ZipFile(temp_zip, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for plan_idx, variant_index in enumerate(plan_indices, 1):
                    folder_name = f"方案{plan_idx}"
                    plan_paths = _remix_plan_paths(run_dir, job_id, variant_index)
                    if not plan_paths:
                        continue
                    plan = json.loads(plan_paths[0].read_text(encoding="utf-8"))
                    segments = remix_export_segments(result, plan)

                    from .exporter import _normalize_zip_segments, _segment_entry_name
                    normalized = _normalize_zip_segments(segments)
                    plan_tmp_dir = tmp_root / folder_name
                    plan_tmp_dir.mkdir(parents=True, exist_ok=True)

                    from . import media
                    for index, segment in enumerate(normalized, 1):
                        entry_name = _segment_entry_name(index, segment)
                        segment_path = plan_tmp_dir / entry_name
                        warning = media.export_clip(
                            source_video,
                            segment_path,
                            float(segment["start"]),
                            float(segment["end"]),
                        )
                        if warning:
                            return warning
                        archive.write(segment_path, f"{folder_name}/{entry_name}")
        except Exception:
            temp_zip.unlink(missing_ok=True)
            raise

    temp_zip.replace(output_path)
    return None


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


def _ensure_worker_running() -> None:
    if not jobs.is_worker_running():
        jobs.start_worker()
