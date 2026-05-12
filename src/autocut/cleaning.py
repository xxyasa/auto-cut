from __future__ import annotations

import re
from collections import Counter

from .models import CandidateClip, TranscriptSegment


FILLER_WORDS = [
    "咱们这样子什么呢",
    "我们这样子什么呢",
    "咱们这样子的",
    "我们这样子的",
    "这样子一个",
    "这样子的一个",
    "这样的一个",
    "这样一个",
    "这样子什么呢",
    "这样子的什么呢",
    "这样子的",
    "这样子呢",
    "可以来看一看",
    "可以看一看",
    "可以来看一下",
    "可以看一下",
    "给大家去看一下",
    "给大家去看一看",
    "给大家来看一下",
    "给大家来看一看",
    "给大家看一下",
    "给大家看一看",
    "大家去看一下",
    "大家去看一看",
    "来看一看",
    "来看一下",
    "大家可以来看一看",
    "大家可以来看一下",
    "我们可以来看一看",
    "我们可以来看一下",
    "咱们来看一看",
    "咱们来看一下",
    "这边可以看一下",
    "这边来看一下",
    "嗯",
    "啊",
    "呃",
    "额",
    "咳嗽声",
    "咳嗽",
    "咳咳",
    "咳",
    "然后",
    "就是",
    "这个",
    "那个",
    "这样子",
    "这样的",
    "这么一个",
    "这么一款",
    "这么一种",
    "整个的",
    "给大家去",
    "给大家",
    "去进行一个",
    "进行一个",
    "我们的一个",
    "它的一个",
    "整体的一个",
    "其实",
    "对吧",
    "是不是",
    "是吧",
    "对不对",
    "家人们",
    "宝宝们",
    "姐妹们",
    "就是说",
    "所以说",
    "或者说",
    "等等",
]

FILLER_PATTERNS = [
    re.compile(r"(?<![为是叫指有像])什么呢"),
]

INVALID_PATTERNS = [
    "等一下",
    "听得到吗",
    "能听到吗",
    "卡了吗",
    "看得到吗",
    "欢迎进入直播间",
    "点点关注",
]


def normalize_text(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"([，。！？、,.!?])\1+", r"\1", text)
    return text


def clean_text(text: str) -> tuple[str, float, float, list[str]]:
    normalized = normalize_text(text)
    if not normalized:
        return "", 0.0, 0.0, ["empty_text"]

    filler_chars = 0
    cleaned = normalized
    for word in FILLER_WORDS:
        count = cleaned.count(word)
        filler_chars += count * len(word)
        cleaned = cleaned.replace(word, "")
    for pattern in FILLER_PATTERNS:
        matches = list(pattern.finditer(cleaned))
        filler_chars += sum(len(match.group(0)) for match in matches)
        cleaned = pattern.sub("", cleaned)

    cleaned = normalize_text(cleaned)
    filler_ratio = min(1.0, filler_chars / max(1, len(normalized)))
    repeat_ratio = estimate_repeat_ratio(normalized)
    invalid_reasons: list[str] = []

    if filler_ratio >= 0.25:
        invalid_reasons.append("filler_ratio_high")
    if repeat_ratio >= 0.25:
        invalid_reasons.append("repeat_ratio_high")
    for pattern in INVALID_PATTERNS:
        if pattern in normalized:
            invalid_reasons.append(f"invalid_pattern:{pattern}")
    if len(cleaned) < 8:
        invalid_reasons.append("too_short_after_cleaning")

    return cleaned, filler_ratio, repeat_ratio, invalid_reasons


def estimate_repeat_ratio(text: str, n: int = 4) -> float:
    chars = [char for char in text if not char.isspace()]
    if len(chars) < n * 2:
        return 0.0
    grams = ["".join(chars[index : index + n]) for index in range(len(chars) - n + 1)]
    counts = Counter(grams)
    repeated = sum(count - 1 for count in counts.values() if count > 1)
    return min(1.0, repeated / max(1, len(grams)))


def clean_segments(segments: list[TranscriptSegment]) -> list[TranscriptSegment]:
    cleaned_segments: list[TranscriptSegment] = []
    for segment in segments:
        cleaned, filler_ratio, repeat_ratio, invalid_reasons = clean_text(segment.text)
        segment.clean_text = cleaned
        segment.filler_ratio = filler_ratio
        segment.repeat_ratio = repeat_ratio
        segment.invalid_reasons = invalid_reasons
        cleaned_segments.append(segment)
    return cleaned_segments


# 只用于"开头裁剪"的单字水词集合（短音节、语气词）
_LEADING_FILLER_CHARS: frozenset[str] = frozenset(
    "啊阿哦哟呀嗯呢哈哎唉哇喂哼呵"
    "啊阿哦哟呀嗯呢哈哎唉哇喂哼呵"  # 全角兜底
)


def _is_leading_filler_word(word: str) -> bool:
    """判断一个 ASR word 是否是纯开头水词（单个语气字或咳嗽标记）。"""
    w = word.strip().lstrip("[（(【")  # 去掉 faster-whisper 可能加的括号标记
    w = w.rstrip("]）)】.,，。")
    if not w:
        return True
    # 单字语气词
    if len(w) == 1 and w in _LEADING_FILLER_CHARS:
        return True
    # ASR 咳嗽标记，如 "[咳嗽]" / "(cough)" 等
    if re.fullmatch(r"[\[(（【]?(咳嗽?|咳咳|cough|noise|throat)[\]）)】]?", w, re.IGNORECASE):
        return True
    return False


def trim_leading_fillers(
    candidates: list[CandidateClip],
    transcript: list[TranscriptSegment],
) -> list[CandidateClip]:
    """把每个候选片段开头连续的水词单字从时间上裁掉。

    仅调整 start_time，不拆分 segment。若整段都是水词则不裁（保留原样）。
    """
    for candidate in candidates:
        if not candidate.segment_indexes:
            continue
        first_seg = transcript[candidate.segment_indexes[0]]
        words = first_seg.words  # [{start, end, word}]
        if not words:
            continue

        new_start: float | None = None
        for word_info in words:
            w = word_info.get("word", "")
            if _is_leading_filler_word(w):
                continue
            # 找到第一个非水词
            new_start = float(word_info["start"])
            break

        # new_start 为 None 说明整段都是水词，跳过
        if new_start is None:
            continue
        # 只往后推，不往前缩
        if new_start > candidate.start_time + 0.05:
            candidate.start_time = new_start

    return candidates
