from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from . import media
from .cleaning import clean_text
from .jumpcut import build_compact_plan
from .models import CandidateClip, TranscriptSegment


TEXT_CLEAN_RE = re.compile(r"""[\s,，。.!！?？、~～…:：;；"'“”‘’()\[\]{}<>《》]+""")

NON_PRODUCT_PATTERNS = [
    "马上下播",
    "准备下播",
    "快下播",
    "要下播",
    "下播",
    "排单",
    "排一下单",
    "排队",
    "排到了",
    "排到",
    "截单",
    "停播",
    "不播",
    "后台看一下",
    "辛苦后台",
    "辛苦我们的后台",
    "中控看一下",
    "中控老师",
    "看一下大屏幕",
    "大屏幕",
    "改价格",
    "改库存",
    "改一下库存",
    "上链接",
    "加库存",
    "补库存",
    "库存",
    "拼手速",
    "直播间",
    "公域",
    "私域",
    "粉丝团",
    "点关注",
    "公屏",
    "弹幕",
    "听得到吗",
    "能听到吗",
    "看得到吗",
    "卡了吗",
    "卡了",
]

HOOK_PATTERNS = [
    "福利",
    "价格",
    "拍下",
    "带走",
    "入手",
    "到手",
    "不要",
    "只要",
    "直接",
    "限时",
    "划算",
    "冲",
]

IDENTITY_PATTERNS = [
    "哈利波特",
    "联名",
    "学院",
    "赫奇帕奇",
    "格兰芬多",
    "斯莱特林",
    "拉文克劳",
    "巫师",
    "雨伞",
    "透明伞",
    "伞",
]

SELLING_POINT_PATTERNS = [
    "加固",
    "伞骨",
    "防水",
    "加厚",
    "peo",
    "一键",
    "开启",
    "图案",
    "磨砂",
    "透明窗",
    "材质",
    "抗风",
    "结实",
    "牢固",
]

DEMO_PATTERNS = [
    "打开",
    "展开",
    "撑开",
    "雨天",
    "遮雨",
    "日常",
    "看得到",
    "可以看",
    "体现",
]

SCRIPT_ROLE_ORDER = ["hook", "identity", "selling_point", "demo", "proof", "close"]


def build_remix_source(
    result: dict[str, Any],
    request: dict[str, Any] | None = None,
    *,
    target_duration: float = 25.0,
) -> dict[str, Any]:
    request = request or {}
    product = str(request.get("product") or "")
    selling_points = _text_list(request.get("selling_points"))
    brand_terms = _text_list(request.get("brand_terms"))
    units = build_script_units(result, product=product, selling_points=selling_points, brand_terms=brand_terms)
    default_plan = build_remix_plan(units, target_duration=target_duration)
    return {
        "target_duration": target_duration,
        "product": product,
        "selling_points": selling_points,
        "brand_terms": brand_terms,
        "unit_count": len(units),
        "units": units,
        "default_plan": default_plan,
        "prompt": build_llm_prompt(
            units,
            product,
            selling_points,
            brand_terms=brand_terms,
            target_duration=target_duration,
        ),
    }


def build_script_units(
    result: dict[str, Any],
    *,
    product: str = "",
    selling_points: list[str] | None = None,
    brand_terms: list[str] | None = None,
) -> list[dict[str, Any]]:
    product_terms = _product_terms(product, [*(selling_points or []), *(brand_terms or [])])
    units: list[dict[str, Any]] = []
    for index, segment in enumerate(result.get("transcript", []), 1):
        try:
            start = float(segment.get("start") or 0)
            end = float(segment.get("end") or 0)
        except (TypeError, ValueError):
            continue
        raw_text = str(segment.get("text") or "").strip()
        stored_clean_text = str(segment.get("clean_text") or "").strip()
        refreshed_clean_text = clean_text(raw_text)[0] if raw_text else ""
        clean_text_value = refreshed_clean_text or stored_clean_text
        text = clean_text_value or raw_text
        if end <= start or not text:
            continue
        normalized = _normalize(text)
        raw_normalized = _normalize(raw_text)
        exclude_reason = _exclude_reason(raw_normalized) or _exclude_reason(normalized)
        role, score, matched_terms = _score_unit(normalized, product_terms)
        filler_ratio = _float(segment.get("filler_ratio"))
        invalid_reasons = [str(reason) for reason in segment.get("invalid_reasons") or []]
        if len(normalized) <= 5 and not matched_terms:
            exclude_reason = exclude_reason or "内容过短"
        elif _is_water_only_unit(raw_normalized, normalized, filler_ratio, invalid_reasons, matched_terms):
            exclude_reason = exclude_reason or "直播水词过多"
        if filler_ratio:
            score = max(0, score - min(18, round(filler_ratio * 28)))
        units.append(
            {
                "id": f"s{index:03d}",
                "source_index": index - 1,
                "start": round(start, 3),
                "end": round(end, 3),
                "duration": round(end - start, 3),
                "text": text,
                "raw_text": raw_text,
                "clean_text": clean_text_value,
                "role": role,
                "score": score,
                "matched_terms": matched_terms,
                "filler_ratio": round(filler_ratio, 3),
                "invalid_reasons": invalid_reasons,
                "excluded": bool(exclude_reason),
                "exclude_reason": exclude_reason,
            }
        )
    return units


def build_remix_plan(
    units: list[dict[str, Any]],
    *,
    target_duration: float = 25.0,
    min_duration: float | None = None,
    max_duration: float | None = None,
) -> dict[str, Any]:
    min_duration = min_duration if min_duration is not None else max(12.0, target_duration - 4.0)
    max_duration = max_duration if max_duration is not None else target_duration + 4.0
    available = [unit for unit in units if not unit.get("excluded")]
    selected: list[dict[str, Any]] = []
    used_ids: set[str] = set()

    for role in SCRIPT_ROLE_ORDER:
        candidate = _best_unit_for_role(available, role, used_ids, selected, max_duration)
        if candidate:
            selected.append(candidate)
            used_ids.add(candidate["id"])
        if _duration(selected) >= target_duration:
            break

    for unit in sorted(available, key=lambda item: (item.get("score", 0), -item.get("duration", 0)), reverse=True):
        if unit["id"] in used_ids:
            continue
        if _duration(selected) >= min_duration and _duration(selected) + unit["duration"] > max_duration:
            continue
        selected.append(unit)
        used_ids.add(unit["id"])
        if _duration(selected) >= target_duration:
            break

    return _plan_from_units(selected, target_duration=target_duration, strategy="local_heuristic")


def remix_plan_from_ordered_ids(
    units: list[dict[str, Any]],
    ordered_ids: list[str],
    *,
    target_duration: float = 25.0,
) -> dict[str, Any]:
    by_id = {unit["id"]: unit for unit in units}
    ordered_units = []
    seen_ids: set[str] = set()
    for unit_id in ordered_ids:
        unit = by_id.get(unit_id)
        if unit and not unit.get("excluded") and unit["id"] not in seen_ids:
            ordered_units.append(unit)
            seen_ids.add(unit["id"])
    max_duration = target_duration + 3.0
    selected = _fit_ordered_units_to_duration(
        ordered_units,
        target_duration=target_duration,
        min_duration=max(12.0, target_duration - 4.0),
        max_duration=max_duration,
    )
    plan = _plan_from_units(selected, target_duration=target_duration, strategy="model_order")
    plan["source_ordered_ids"] = [unit["id"] for unit in ordered_units]
    plan["duration_window"] = {
        "min": round(max(12.0, target_duration - 4.0), 3),
        "max": round(max_duration, 3),
    }
    return plan


def remix_plan_from_items(
    items: list[dict[str, Any]],
    *,
    target_duration: float = 25.0,
    strategy: str = "manual_order",
) -> dict[str, Any]:
    units: list[dict[str, Any]] = []
    for index, item in enumerate(items, 1):
        try:
            start = float(item.get("start"))
            end = float(item.get("end"))
        except (TypeError, ValueError):
            continue
        if end <= start:
            continue
        text = str(item.get("text") or item.get("clean_text") or item.get("raw_text") or "").strip()
        if not text:
            text = f"手动片段 {index}"
        source_index = item.get("source_index")
        if not isinstance(source_index, int):
            source_index = None
        units.append(
            {
                "id": str(item.get("id") or f"manual_{index:03d}"),
                "source_index": source_index,
                "start": round(start, 3),
                "end": round(end, 3),
                "duration": round(end - start, 3),
                "role": str(item.get("role") or "manual"),
                "score": int(_float(item.get("score"))),
                "text": text,
                "raw_text": str(item.get("raw_text") or text),
                "clean_text": str(item.get("clean_text") or text),
                "manual_source": bool(item.get("manual_source")),
                "source_piece_id": str(item.get("source_piece_id") or ""),
            }
        )
    return _plan_from_units(units, target_duration=target_duration, strategy=strategy)


def export_remix_plan(
    result: dict[str, Any],
    plan: dict[str, Any],
    output_path: Path,
) -> str | None:
    source_video = Path(result.get("media", {}).get("path") or "").resolve()
    if not source_video.exists():
        return f"source video not found: {source_video}"
    transcript_segments = _transcript_segments(result.get("transcript", []))
    ranges: list[dict[str, float]] = []
    for item in plan.get("items", []):
        ranges.extend(_export_ranges_for_item(item, transcript_segments, source_video))
    return media.export_clip_segments(source_video, output_path, ranges)


def build_llm_prompt(
    units: list[dict[str, Any]],
    product: str,
    selling_points: list[str],
    *,
    brand_terms: list[str] | None = None,
    target_duration: float = 25.0,
) -> str:
    brand_terms = brand_terms or []
    product_terms = _product_terms(product, [*selling_points, *brand_terms])
    usable_lines = [
        _prompt_line(unit)
        for unit in units
        if not unit.get("excluded")
    ]
    excluded_lines = [
        f"{unit['id']} | {unit['exclude_reason']} | {unit['text']}"
        for unit in units
        if unit.get("excluded")
    ]
    return "\n".join(
        [
            "你现在是一个抖音高级运营主管，你需要通过主播直播的切片来截取产品相关画面来剪辑成片发布到千川平台进行带货。",
            "请根据千川爆款内容的脚本特点，重新组合我给你的直播切片文案。",
            f"硬性要求：不能修改每一句文案中的任何一个字；只能选择句子编号并调整顺序；总时长控制在{target_duration:.0f}秒左右；必须按每条候选句前面的秒数累加；不要选择下播、排单、后台操作、闲聊、纯水词等非产品介绍内容。",
            f"时长边界：优先控制在{max(12.0, target_duration - 4.0):.0f}-{target_duration + 3.0:.0f}秒之间，宁可少选几句，也不要超过{target_duration + 3.0:.0f}秒。",
            "",
            "成片合理性要求：",
            "1. 你必须先判断整条视频是否像一个完整、自然的带货短片，而不是直播中途突然截出来的一段。",
            "2. 第一条必须是正常开头：优先选择能独立成立的钩子、产品身份、价格福利或明确产品介绍句；开头不能让用户感觉前面少了一句话。",
            "3. 第一条不要选择承接句、半句话、缺少主语的句子，例如以“像、也是、然后、包括、所以、这个、那个、它、我们这个、来、如果有喜欢”等开头的句子，除非它本身已经完整说明产品和卖点。",
            "4. 如果某句虽然可用但像直播中途的承接话，只能放在前后语义能接上的位置，不能作为开头。",
            "5. reason 字段需要说明：为什么第一句不突兀，整体排序如何遵循下面的三段式结构，以及结尾促单是怎么落点的。",
            "",
            "成片结构要求（严格遵守的三段式：开头—中间—结尾）：",
            "",
            "【开头】（可选，可省略，可任选 1~3 句；如果直播切片里没有合适开头，可以直接进入中间）：",
            "   - 产品类型/品类介绍（例如“这是一款……雨伞/项链/沙发”）",
            "   - 全部款式展示（例如“我们一共有 X 个款”“今天上的全部款”）",
            "   - 节假日送礼需求场景（例如“马上就要过节了”“送女朋友/送妈妈”等）",
            "   - 产品特点综述（一句话讲清产品最大亮点）",
            "   - 故事情节引入（设计灵感、品牌故事、使用人群故事）",
            "   开头若选用必须语义完整、能独立成立，不能是承接半句。",
            "",
            "【中间】（成片主体，必须有，从下列三类里至少选 1 句）：",
            "   - 产品卖点（功能、效果、差异化、独特优势）",
            "   - 使用场景（什么时候用、给谁用、怎么用、搭配场景）",
            "   - 产品工艺（材质、做工细节、工艺、细节展示）",
            "",
            "【结尾】（必须有，强制以促单收尾，从下列促单要素里选 1~3 句）：",
            "   - 7 天无理由退换",
            "   - 运费险",
            "   - 折扣 / 优惠 / 价格福利 / 直播间专属价",
            "   - 礼盒 / 礼袋 / 精美包装",
            "   - 适合送人 / 送礼场景收口",
            "   结尾必须是促单导向，让用户产生下单冲动。",
            "   兜底规则：如果候选句里完全没有上述促单要素，必须改用最接近的下单理由兜底（例如优惠提示、价格福利、限时活动），不允许用纯卖点或纯场景句结尾。",
            "   reason 字段需要明确说明结尾选择的逻辑，以及是否触发了兜底。",
            "",
            "输出时不需要标注【开头】【中间】【结尾】，只输出 ordered_ids 数组即可，但内部排序必须严格遵循上述三段式结构。",
            "",
            "业务背景：",
            f"产品名称：{product or '未知'}",
            f"品牌/热词：{_join_or_empty(brand_terms)}",
            f"核心卖点：{_join_or_empty(selling_points)}",
            f"产品关键词：{_join_or_empty(product_terms)}",
            "投放场景：千川短视频带货，目标是让用户快速理解产品身份、卖点、材质/功能和下单理由。",
            "素材说明：以下候选句来自直播口播，已尽量清洗“这样子、可以来看一看、嗯啊”等拖节奏水词；排序时优先选择语义完整、贴合产品背景的句子。",
            f"目标时长：{target_duration:.0f}秒左右",
            "",
            "请只输出 JSON，格式如下：",
            '{"ordered_ids":["s001","s008","s003"],"reason":"一句话说明排序逻辑，包括开头是否使用、中间选取了哪些类目、结尾促单怎么落点"}',
            "",
            "可用字幕句子：",
            "\n".join(usable_lines),
            "",
            "已过滤内容，仅供参考，不要选择：",
            "\n".join(excluded_lines) if excluded_lines else "无",
        ]
    )


def write_remix_plan(path: Path, plan: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")


def _plan_from_units(
    selected: list[dict[str, Any]],
    *,
    target_duration: float,
    strategy: str,
) -> dict[str, Any]:
    items = []
    for order, unit in enumerate(selected, 1):
        items.append(
            {
                "order": order,
                "id": unit["id"],
                "source_index": unit["source_index"],
                "start": unit["start"],
                "end": unit["end"],
                "duration": unit["duration"],
                "role": unit["role"],
                "score": unit["score"],
                "text": unit["text"],
                "raw_text": unit.get("raw_text", unit["text"]),
                "clean_text": unit.get("clean_text", unit["text"]),
                "manual_source": bool(unit.get("manual_source")),
                "source_piece_id": unit.get("source_piece_id", ""),
            }
        )
    return {
        "strategy": strategy,
        "target_duration": target_duration,
        "duration": round(_duration(items), 3),
        "ordered_ids": [item["id"] for item in items],
        "script_text": "\n".join(item["text"] for item in items),
        "items": items,
    }


def _best_unit_for_role(
    units: list[dict[str, Any]],
    role: str,
    used_ids: set[str],
    selected: list[dict[str, Any]],
    max_duration: float,
) -> dict[str, Any] | None:
    candidates = [
        unit
        for unit in units
        if unit["id"] not in used_ids
        and unit.get("role") == role
        and (_duration(selected) + unit["duration"] <= max_duration or not selected)
    ]
    if not candidates and role == "proof":
        candidates = [
            unit
            for unit in units
            if unit["id"] not in used_ids
            and unit.get("role") in {"selling_point", "demo"}
            and _duration(selected) + unit["duration"] <= max_duration
        ]
    if not candidates:
        return None
    return max(candidates, key=lambda item: (item.get("score", 0), item.get("duration", 0)))


def _fit_ordered_units_to_duration(
    ordered_units: list[dict[str, Any]],
    *,
    target_duration: float,
    min_duration: float,
    max_duration: float,
) -> list[dict[str, Any]]:
    fitting_units = [
        unit
        for unit in ordered_units
        if 0 < float(unit.get("duration") or 0) <= max_duration
    ]
    if not fitting_units:
        return ordered_units[:1]

    opening = fitting_units[0]
    opening_duration = float(opening.get("duration") or 0)
    if opening_duration >= target_duration:
        return [opening]

    remaining = fitting_units[1:]
    filler = _best_duration_subset(
        remaining,
        min_duration=max(0.0, min_duration - opening_duration),
        target_duration=max(0.0, target_duration - opening_duration),
        max_duration=max(0.0, max_duration - opening_duration),
    )
    selected = [opening, *filler]
    if _duration(selected) >= min_duration or selected:
        return selected
    return [opening]


def _best_duration_subset(
    units: list[dict[str, Any]],
    *,
    min_duration: float,
    target_duration: float,
    max_duration: float,
) -> list[dict[str, Any]]:
    scale = 10
    min_ticks = max(0, int(round(min_duration * scale)))
    target_ticks = max(0, int(round(target_duration * scale)))
    max_ticks = max(0, int(round(max_duration * scale)))
    if max_ticks <= 0:
        return []

    states: dict[int, list[dict[str, Any]]] = {0: []}
    for unit in units:
        unit_ticks = int(round(float(unit.get("duration") or 0) * scale))
        if unit_ticks <= 0 or unit_ticks > max_ticks:
            continue
        for total, subset in list(states.items()):
            next_total = total + unit_ticks
            if next_total > max_ticks:
                continue
            current_subset = states.get(next_total)
            candidate_subset = [*subset, unit]
            if current_subset is None or _subset_rank(candidate_subset) > _subset_rank(current_subset):
                states[next_total] = candidate_subset

    candidates = [(total, subset) for total, subset in states.items() if total > 0 and subset]
    if not candidates:
        return []
    enough = [(total, subset) for total, subset in candidates if total >= min_ticks]
    pool = enough or candidates
    _, best_subset = min(
        pool,
        key=lambda item: (
            abs(item[0] - target_ticks),
            item[0] < min_ticks,
            abs(max(0, target_ticks - item[0])),
            -_subset_rank(item[1]),
        ),
    )
    return best_subset


def _subset_rank(subset: list[dict[str, Any]]) -> int:
    return sum(int(item.get("score") or 0) for item in subset)


def _duration(items: list[dict[str, Any]]) -> float:
    return round(sum(float(item.get("duration") or 0) for item in items), 3)


def _export_ranges_for_item(
    item: dict[str, Any],
    transcript_segments: list[TranscriptSegment],
    source_video: Path,
) -> list[dict[str, float]]:
    try:
        start = float(item["start"])
        end = float(item["end"])
    except (KeyError, TypeError, ValueError):
        return []
    if end <= start:
        return []

    fallback = [{"start": round(start, 3), "end": round(end, 3)}]
    source_index = item.get("source_index")
    if not isinstance(source_index, int) or source_index < 0 or source_index >= len(transcript_segments):
        return fallback

    segment = transcript_segments[source_index]
    if not segment.words:
        return fallback

    candidate = CandidateClip(
        clip_id=str(item.get("id") or "remix"),
        source_video=source_video,
        start_time=start,
        end_time=end,
        transcript=str(item.get("raw_text") or item.get("text") or ""),
        clean_transcript=str(item.get("clean_text") or item.get("text") or ""),
        segment_indexes=[source_index],
    )
    compact = build_compact_plan(
        candidate,
        [segment],
        padding=0.06,
        merge_gap=0.35,
        min_removed_duration=0.12,
    )
    return compact.keep_ranges if compact else fallback


def _transcript_segments(items: list[dict[str, Any]]) -> list[TranscriptSegment]:
    segments: list[TranscriptSegment] = []
    for item in items:
        try:
            start = float(item.get("start") or 0)
            end = float(item.get("end") or 0)
        except (TypeError, ValueError):
            continue
        if end <= start:
            continue
        segments.append(
            TranscriptSegment(
                start=start,
                end=end,
                text=str(item.get("text") or ""),
                speaker=item.get("speaker"),
                words=list(item.get("words") or []),
                clean_text=str(item.get("clean_text") or ""),
                filler_ratio=_float(item.get("filler_ratio")),
                repeat_ratio=_float(item.get("repeat_ratio")),
                invalid_reasons=[str(reason) for reason in item.get("invalid_reasons") or []],
            )
        )
    return segments


def _prompt_line(unit: dict[str, Any]) -> str:
    parts = [
        unit["id"],
        f"{unit['duration']:.1f}s",
        f"角色:{unit.get('role', 'unknown')}",
    ]
    opening_risk = _opening_risk(unit)
    if opening_risk:
        parts.append(f"开头慎用:{opening_risk}")
    matched_terms = unit.get("matched_terms") or []
    if matched_terms:
        parts.append(f"命中:{'、'.join(str(term) for term in matched_terms[:6])}")
    if unit.get("filler_ratio"):
        parts.append(f"水词占比:{float(unit['filler_ratio']):.2f}")
    parts.append(str(unit.get("text") or ""))
    return " | ".join(parts)


def _opening_risk(unit: dict[str, Any]) -> str:
    text = str(unit.get("text") or "")
    normalized = _normalize(text)
    if not normalized:
        return "空句"
    risky_prefixes = [
        "像",
        "也是",
        "然后",
        "包括",
        "所以",
        "所以说",
        "这个",
        "那个",
        "它",
        "我们这个",
        "来",
        "如果有喜欢",
        "买下之后",
        "是我们",
    ]
    if any(normalized.startswith(_normalize(prefix)) for prefix in risky_prefixes):
        matched_terms = unit.get("matched_terms") or []
        role = str(unit.get("role") or "")
        if role in {"identity", "selling_point", "hook"} and len(matched_terms) >= 2 and len(normalized) >= 14:
            return ""
        return "承接句/可能缺少前文"
    if normalized.endswith(("做到的", "当中", "一个", "的话", "可以", "什么呢")):
        return "半句话/收尾不完整"
    return ""


def _is_water_only_unit(
    raw_normalized: str,
    normalized: str,
    filler_ratio: float,
    invalid_reasons: list[str],
    matched_terms: list[str],
) -> bool:
    if matched_terms and len(normalized) >= 8:
        return False
    if "too_short_after_cleaning" in invalid_reasons:
        return True
    if "filler_ratio_high" in invalid_reasons and len(normalized) <= 14:
        return True
    if filler_ratio >= 0.32 and len(normalized) <= 16:
        return True
    weak_patterns = ["这样子", "这样子的", "来看一下", "看一看", "给大家看一下", "等等"]
    return len(normalized) <= 10 and any(pattern in raw_normalized for pattern in weak_patterns)


def _text_list(value: Any) -> list[str]:
    if not value:
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    return [str(item).strip() for item in value if str(item).strip()]


def _join_or_empty(items: list[str]) -> str:
    return "、".join(items) if items else "未提供"


def _float(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _score_unit(normalized: str, product_terms: list[str]) -> tuple[str, int, list[str]]:
    matched_terms: list[str] = []
    score = 0
    for term in product_terms:
        if term and term in normalized:
            matched_terms.append(term)
            score += 8
    hook_hits = _hits(normalized, HOOK_PATTERNS)
    identity_hits = _hits(normalized, IDENTITY_PATTERNS)
    selling_hits = _hits(normalized, SELLING_POINT_PATTERNS)
    demo_hits = _hits(normalized, DEMO_PATTERNS)
    score += len(hook_hits) * 5 + len(identity_hits) * 6 + len(selling_hits) * 7 + len(demo_hits) * 4
    matched_terms.extend(hook_hits + identity_hits + selling_hits + demo_hits)
    if hook_hits:
        role = "hook" if not selling_hits else "close"
    elif selling_hits:
        role = "selling_point"
    elif demo_hits:
        role = "demo"
    elif identity_hits:
        role = "identity"
    else:
        role = "proof"
    return role, score, list(dict.fromkeys(matched_terms))


def _product_terms(product: str, selling_points: list[str]) -> list[str]:
    raw_terms = [product, *selling_points, *IDENTITY_PATTERNS, *SELLING_POINT_PATTERNS]
    terms = []
    for item in raw_terms:
        normalized = _normalize(item)
        if len(normalized) >= 2:
            terms.append(normalized)
    return list(dict.fromkeys(terms))


def _exclude_reason(normalized: str) -> str:
    for pattern in NON_PRODUCT_PATTERNS:
        if _normalize(pattern) in normalized:
            return "非产品介绍/直播操作"
    return ""


def _hits(normalized: str, patterns: list[str]) -> list[str]:
    return [pattern for pattern in patterns if _normalize(pattern) in normalized]


def _normalize(text: str) -> str:
    return TEXT_CLEAN_RE.sub("", str(text or "").lower())
