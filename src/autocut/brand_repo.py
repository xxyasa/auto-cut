"""品牌/产品仓库（JSON 文件实现）。

设计要点（见 .openspec/proposals/0001-business-self-service-clip.md §3）：

- 单文件 JSON 落盘，路径由 `AUTOCUT_BRAND_REPO` 环境变量覆盖（默认 `data/brand_repo.json`）。
- 进程内 `threading.RLock` 保护读写。
- 落盘走 `atomic_write`：tmp + `os.replace`，POSIX/Windows 均原子。
- `schema_version` 兼容策略：缺失或 <1 视为空仓库重新初始化；>1 抛 `SchemaVersionError` 拒绝启动。
- `id` 服务端生成：`brand_` / `prod_` + 8 字节 hex。
- `name` 同级唯一（brand 全局唯一；product 在 brand 内唯一）。

接口：
    list_brands / get_brand / create_brand / update_brand / delete_brand
    list_products / create_product / update_product / delete_product
    merge_terms (品牌联想 + 产品联想 + 业务表单 extra)
    suggest_associations (LLM 调用，骨架，由 PR-2 接入真实 LLM)
"""

from __future__ import annotations

import json
import os
import secrets
import tempfile
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Optional

SUPPORTED_SCHEMA_VERSION = 1
DEFAULT_REPO_PATH = "data/brand_repo.json"
ENV_REPO_PATH = "AUTOCUT_BRAND_REPO"


# ---------- 异常 ----------


class BrandRepoError(RuntimeError):
    """品牌仓库基础异常。"""


class NotFound(BrandRepoError):
    """实体未找到（→ 404）。"""


class Conflict(BrandRepoError):
    """同级名称冲突或级联冲突（→ 409）。"""


class SchemaVersionError(BrandRepoError):
    """schema_version 高于本进程支持，拒绝加载。"""


class LLMUnavailable(BrandRepoError):
    """LLM 服务不可用（→ 502）。"""


# ---------- 内部状态 ----------

_REPO_LOCK = threading.RLock()


def _repo_path() -> Path:
    """返回当前仓库文件路径。每次调用都重新读环境变量，便于测试 monkeypatch。"""
    raw = os.environ.get(ENV_REPO_PATH, DEFAULT_REPO_PATH)
    return Path(raw).resolve()


# ---------- 原子写 ----------


def _atomic_write(path: Path, data: dict[str, Any]) -> None:
    """原子写 JSON 到 path：先写 tmp 同目录，再 os.replace。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    # 同目录 tmp 才能保证 replace 原子（跨卷不行）
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
        # 清理半截 tmp
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise


# ---------- 加载 / 落盘 ----------


def _empty_repo() -> dict[str, Any]:
    return {
        "schema_version": SUPPORTED_SCHEMA_VERSION,
        "brands": [],
        "updated_at": None,
    }


def _load_unlocked() -> dict[str, Any]:
    """读 JSON 仓库；不存在则返回空仓库。校验 schema_version。"""
    path = _repo_path()
    if not path.exists():
        return _empty_repo()
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except json.JSONDecodeError as exc:
        raise BrandRepoError(f"brand_repo.json malformed: {exc}") from exc

    if not isinstance(data, dict):
        raise BrandRepoError("brand_repo.json root must be an object")

    version = data.get("schema_version")
    if not isinstance(version, int) or version < SUPPORTED_SCHEMA_VERSION:
        # 缺失或 <1：视为空仓库重新初始化
        return _empty_repo()
    if version > SUPPORTED_SCHEMA_VERSION:
        raise SchemaVersionError(
            f"brand_repo.json schema_version={version} > supported "
            f"{SUPPORTED_SCHEMA_VERSION}, refusing to start"
        )

    brands = data.get("brands")
    if not isinstance(brands, list):
        raise BrandRepoError("brand_repo.json `brands` must be a list")

    return data


def _save_unlocked(data: dict[str, Any]) -> None:
    data["schema_version"] = SUPPORTED_SCHEMA_VERSION
    data["updated_at"] = datetime.now(UTC).isoformat()
    _atomic_write(_repo_path(), data)


# ---------- ID / 工具 ----------


def _gen_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(4)}"


def _find_brand(data: dict[str, Any], brand_id: str) -> dict[str, Any]:
    for brand in data["brands"]:
        if brand["id"] == brand_id:
            return brand
    raise NotFound(f"brand_not_found: {brand_id}")


def _find_product(brand: dict[str, Any], product_id: str) -> dict[str, Any]:
    for product in brand.get("products", []):
        if product["id"] == product_id:
            return product
    raise NotFound(f"product_not_found: {product_id}")


def _dedupe_keep_order(items: list[str]) -> list[str]:
    """按首次出现顺序去重，大小写敏感。空串/None 过滤。"""
    seen: set[str] = set()
    out: list[str] = []
    for raw in items:
        if not raw:
            continue
        if raw in seen:
            continue
        seen.add(raw)
        out.append(raw)
    return out


def _normalize_str_list(value: Any) -> list[str]:
    """把任意输入归一化成 list[str]；非字符串或空串过滤。"""
    if not isinstance(value, list):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


# ---------- 品牌 CRUD ----------


def list_brands() -> list[dict[str, Any]]:
    with _REPO_LOCK:
        data = _load_unlocked()
        return [dict(brand) for brand in data["brands"]]


def get_brand(brand_id: str) -> dict[str, Any]:
    with _REPO_LOCK:
        data = _load_unlocked()
        return dict(_find_brand(data, brand_id))


def create_brand(name: str, associations: Optional[list[str]] = None) -> dict[str, Any]:
    name = (name or "").strip()
    if not name:
        raise BrandRepoError("brand_name_required")
    with _REPO_LOCK:
        data = _load_unlocked()
        for brand in data["brands"]:
            if brand["name"] == name:
                raise Conflict(f"brand_name_conflict: {name}")
        brand = {
            "id": _gen_id("brand"),
            "name": name,
            "associations": _dedupe_keep_order(_normalize_str_list(associations or [])),
            "products": [],
        }
        data["brands"].append(brand)
        _save_unlocked(data)
        return dict(brand)


def update_brand(brand_id: str, patch: dict[str, Any]) -> dict[str, Any]:
    with _REPO_LOCK:
        data = _load_unlocked()
        brand = _find_brand(data, brand_id)
        if "name" in patch:
            new_name = (patch["name"] or "").strip()
            if not new_name:
                raise BrandRepoError("brand_name_required")
            for other in data["brands"]:
                if other["id"] != brand_id and other["name"] == new_name:
                    raise Conflict(f"brand_name_conflict: {new_name}")
            brand["name"] = new_name
        if "associations" in patch:
            brand["associations"] = _dedupe_keep_order(
                _normalize_str_list(patch["associations"])
            )
        _save_unlocked(data)
        return dict(brand)


def delete_brand(brand_id: str, *, cascade: bool = False) -> None:
    with _REPO_LOCK:
        data = _load_unlocked()
        brand = _find_brand(data, brand_id)
        if brand.get("products") and not cascade:
            raise Conflict(
                f"cascade_required: brand {brand_id} has "
                f"{len(brand['products'])} product(s)"
            )
        data["brands"] = [b for b in data["brands"] if b["id"] != brand_id]
        _save_unlocked(data)


# ---------- 产品 CRUD ----------


def list_products(brand_id: str) -> list[dict[str, Any]]:
    with _REPO_LOCK:
        data = _load_unlocked()
        brand = _find_brand(data, brand_id)
        return [dict(p) for p in brand.get("products", [])]


def create_product(
    brand_id: str,
    name: str,
    selling_points: Optional[list[str]] = None,
    associations: Optional[list[str]] = None,
) -> dict[str, Any]:
    name = (name or "").strip()
    if not name:
        raise BrandRepoError("product_name_required")
    with _REPO_LOCK:
        data = _load_unlocked()
        brand = _find_brand(data, brand_id)
        for product in brand.get("products", []):
            if product["name"] == name:
                raise Conflict(f"product_name_conflict: {name}")
        product = {
            "id": _gen_id("prod"),
            "name": name,
            "selling_points": _dedupe_keep_order(_normalize_str_list(selling_points or [])),
            "associations": _dedupe_keep_order(_normalize_str_list(associations or [])),
        }
        brand.setdefault("products", []).append(product)
        _save_unlocked(data)
        return dict(product)


def update_product(
    brand_id: str, product_id: str, patch: dict[str, Any]
) -> dict[str, Any]:
    with _REPO_LOCK:
        data = _load_unlocked()
        brand = _find_brand(data, brand_id)
        product = _find_product(brand, product_id)
        if "name" in patch:
            new_name = (patch["name"] or "").strip()
            if not new_name:
                raise BrandRepoError("product_name_required")
            for other in brand.get("products", []):
                if other["id"] != product_id and other["name"] == new_name:
                    raise Conflict(f"product_name_conflict: {new_name}")
            product["name"] = new_name
        if "selling_points" in patch:
            product["selling_points"] = _dedupe_keep_order(
                _normalize_str_list(patch["selling_points"])
            )
        if "associations" in patch:
            product["associations"] = _dedupe_keep_order(
                _normalize_str_list(patch["associations"])
            )
        _save_unlocked(data)
        return dict(product)


def delete_product(brand_id: str, product_id: str) -> None:
    with _REPO_LOCK:
        data = _load_unlocked()
        brand = _find_brand(data, brand_id)
        # 触发 NotFound 校验
        _find_product(brand, product_id)
        brand["products"] = [
            p for p in brand.get("products", []) if p["id"] != product_id
        ]
        _save_unlocked(data)


# ---------- 联想词合并 ----------


def merge_terms(
    brand_id: Optional[str],
    product_id: Optional[str],
    extra: list[str],
) -> list[str]:
    """合并品牌联想 + 产品联想 + 业务追加 extra，去重保序。

    语义表（见 §3.4）：
      - (None, None)         -> dedupe(extra)
      - (set,  None)         -> brand.associations + extra
      - (set,  set)          -> brand.associations + product.associations + extra
      - (None, set)          -> ValueError
    """
    if brand_id is None and product_id is not None:
        raise ValueError("product_id requires brand_id")

    extra_norm = _normalize_str_list(extra or [])

    if brand_id is None:
        return _dedupe_keep_order(extra_norm)

    with _REPO_LOCK:
        data = _load_unlocked()
        brand = _find_brand(data, brand_id)
        merged: list[str] = list(brand.get("associations", []))
        if product_id is not None:
            product = _find_product(brand, product_id)
            merged.extend(product.get("associations", []))
        merged.extend(extra_norm)
        return _dedupe_keep_order(merged)


# ---------- LLM 联想词推荐（PR-2 完整接入） ----------


def suggest_associations(
    brand_name: str,
    product_name: str,
    selling_points: list[str],
) -> list[str]:
    """调用 LLM 推荐联想词。

    Args:
        brand_name: 品牌名（必填非空）。
        product_name: 产品名（必填非空）。
        selling_points: 卖点列表，可空。

    Returns:
        去重保序的联想词列表，长度不限（业务侧自行截断）。

    Raises:
        BrandRepoError: 入参非法（空品牌/产品）。
        LLMUnavailable: AUTOCUT_LLM_API_KEY 缺失或 LLM 调用失败 / 返回 JSON 不合法 / suggestions 字段缺失。
    """
    brand_name = (brand_name or "").strip()
    product_name = (product_name or "").strip()
    if not brand_name:
        raise BrandRepoError("brand_name_required")
    if not product_name:
        raise BrandRepoError("product_name_required")

    # 延迟导入避免循环依赖
    from . import llm as llm_mod

    sp_lines = "\n".join(f"- {sp}" for sp in selling_points if sp) or "（无）"
    prompt = (
        "你是直播带货 ASR 联想词专家，目的是为下面的品牌+产品生成有助于语音识别的联想词。\n"
        "联想词包含：品牌别名、IP/角色名、材质名、卖点关键短语等。\n"
        "请只返回 JSON：{\"suggestions\": [\"词1\", \"词2\", ...]}，不要解释。\n\n"
        f"品牌：{brand_name}\n"
        f"产品：{product_name}\n"
        f"卖点：\n{sp_lines}\n"
    )
    try:
        content = llm_mod.chat_completion(prompt)
        parsed = llm_mod.parse_model_json(content)
    except llm_mod.LLMError as exc:
        raise LLMUnavailable(f"llm_failed: {exc}") from exc

    raw = parsed.get("suggestions")
    if not isinstance(raw, list):
        raise LLMUnavailable("llm_response_missing_suggestions")
    items = [str(item).strip() for item in raw if str(item).strip()]
    return _dedupe_keep_order(items)
