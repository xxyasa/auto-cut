from __future__ import annotations

from .cleaning import clean_text
from .models import CandidateClip
from .segment import normalize_for_match, tokenize_keywords


CONVERSION_WORDS = [
    "适合",
    "推荐",
    "下单",
    "直接拍",
    "今天",
    "现在",
    "优惠",
    "福利",
    "闭眼入",
    "放心",
    "解决",
    "改善",
]

SCENE_WORDS = ["日常", "通勤", "家里", "办公室", "上班", "出门", "孩子", "妈妈", "敏感", "夏天"]


def score_candidates(
    candidates: list[CandidateClip],
    product: str,
    selling_points: list[str],
) -> list[CandidateClip]:
    for candidate in candidates:
        score_candidate(candidate, product, selling_points)
    candidates.sort(key=lambda item: item.score, reverse=True)
    return candidates


def score_candidate(candidate: CandidateClip, product: str, selling_points: list[str]) -> CandidateClip:
    text = candidate.clean_transcript or candidate.transcript
    _, filler_ratio, repeat_ratio, invalid_reasons = clean_text(candidate.transcript)
    keywords = tokenize_keywords(product, selling_points)

    semantic = semantic_score(text, candidate.duration)
    product_match = product_match_score(text, keywords)
    language = max(0, int(100 - filler_ratio * 120 - repeat_ratio * 80))
    media_quality = 80
    conversion = conversion_score(text)
    diversity = 80

    if invalid_reasons:
        language = max(0, language - 10 * len(invalid_reasons))
        candidate.penalty.extend(invalid_reasons)

    score = round(
        semantic * 0.25
        + product_match * 0.20
        + language * 0.20
        + media_quality * 0.15
        + conversion * 0.10
        + diversity * 0.10
    )

    candidate.dimension_scores = {
        "content_integrity": semantic,
        "product_match": product_match,
        "language_quality": language,
        "media_quality": media_quality,
        "conversion_power": conversion,
        "diversity": diversity,
    }
    candidate.score = max(0, min(100, score))
    candidate.tags = infer_tags(text, keywords)
    candidate.reason = build_reason(candidate)
    candidate.risk = infer_risks(candidate)
    return candidate


def semantic_score(text: str, duration: float) -> int:
    if not text:
        return 0
    length_score = min(100, int(len(text) / 80 * 100))
    if 20 <= duration <= 30:
        duration_score = 100
    elif 12 <= duration <= 45:
        duration_score = 78
    else:
        duration_score = 45
    punctuation_bonus = 10 if any(mark in text for mark in "。！？!?") else 0
    return min(100, round(length_score * 0.45 + duration_score * 0.45 + punctuation_bonus))


def product_match_score(text: str, keywords: list[str]) -> int:
    if not keywords:
        return 60
    match_text = normalize_for_match(text)
    hits = sum(1 for keyword in keywords if normalize_for_match(keyword) in match_text)
    return min(100, 45 + hits * 18)


def conversion_score(text: str) -> int:
    hits = sum(1 for word in CONVERSION_WORDS if word in text)
    return min(100, 50 + hits * 12)


def infer_tags(text: str, keywords: list[str]) -> list[str]:
    tags: list[str] = []
    match_text = normalize_for_match(text)
    if any(normalize_for_match(keyword) in match_text for keyword in keywords):
        tags.append("产品卖点")
    if any(word in text for word in CONVERSION_WORDS):
        tags.append("促购话术")
    if any(word in text for word in SCENE_WORDS):
        tags.append("场景人群")
    if any(word in text for word in ["痛", "问题", "困扰", "不舒服", "麻烦"]):
        tags.append("痛点解释")
    return tags or ["待人工判断"]


def infer_risks(candidate: CandidateClip) -> list[str]:
    risks: list[str] = []
    if candidate.duration < 12:
        risks.append("duration_too_short")
    if candidate.duration > 45:
        risks.append("duration_too_long")
    if candidate.dimension_scores.get("product_match", 0) < 55:
        risks.append("product_match_low")
    if candidate.dimension_scores.get("language_quality", 0) < 60:
        risks.append("language_quality_low")
    return risks


def build_reason(candidate: CandidateClip) -> str:
    strengths = []
    if candidate.dimension_scores.get("product_match", 0) >= 75:
        strengths.append("卖点匹配较好")
    if candidate.dimension_scores.get("content_integrity", 0) >= 75:
        strengths.append("表达相对完整")
    if candidate.dimension_scores.get("conversion_power", 0) >= 70:
        strengths.append("具备转化话术")
    if not strengths:
        strengths.append("可作为候选片段进入人工复核")
    return "，".join(strengths) + "。"
