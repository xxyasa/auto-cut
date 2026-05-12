"""业务自助成片闭环：FastAPI 路由层。

PR-1 范围：
  - 品牌/产品 CRUD（/api/business/brands*）
  - 联想词推荐占位（/api/business/brands/{id}/suggest-associations，502）
  - 鉴权统一走 `Depends(require_token)`

后续 PR：
  - PR-2: suggest-associations 接入 LLM
  - PR-3: POST /api/business/jobs（视频提交 + 异步任务）
  - PR-4: GET /api/business/jobs/{id} + 静态资源 + /business.html

约定：
  - 路由抛业务异常 → 这里统一转 HTTPException。
  - 所有路由均挂 `dependencies=[Depends(require_token)]`，方便后期审计。
"""

from __future__ import annotations

from typing import Optional

try:
    from fastapi import APIRouter, Body, Depends, HTTPException, Query, status
    from pydantic import BaseModel, Field
except ImportError as exc:  # pragma: no cover
    raise RuntimeError(
        "Install API dependencies with: pip install -e '.[api]'"
    ) from exc

from . import brand_repo
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
