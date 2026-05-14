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

# 价格相关正则，命中内容从 clean_text 中抹除并打标
PRICE_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\d+(\.\d+)?\s*[元块]"),               # 99元、59.9块
    re.compile(r"[零一二三四五六七八九十百]+(折|打折)"),  # 八折、七折
    re.compile(r"\d+\s*折"),                             # 8折、7.5折
    re.compile(r"(原价|现价|促销价|特价|到手价|券后|活动价|日常价|底价|秒杀价)[^\s，。！？,!?]{0,20}"),
    re.compile(r"买\s*\d+\s*(件|个|套)?\s*(送|赠|减|立减)\s*\d*"),  # 买X送Y
    re.compile(r"\d+\s*(件|个|套)\s*\d+\s*[元块]"),       # 2件59元
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

    # 价格内容过滤：从 clean_text 中抹除价格描述
    price_chars = 0
    for price_pattern in PRICE_PATTERNS:
        price_matches = list(price_pattern.finditer(cleaned))
        price_chars += sum(len(m.group(0)) for m in price_matches)
        cleaned = price_pattern.sub("", cleaned)

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
    if price_chars > 0:
        invalid_reasons.append("contains_price")
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
    lead_padding: float = 0.05,
) -> list[CandidateClip]:
    """把每个候选片段开头连续的水词单字从时间上裁掉。

    仅调整 start_time，不拆分 segment。若整段都是水词则不裁（保留原样）。
    lead_padding: 第一个实意词前保留的缓冲时间（秒），默认 50ms，避免切到字中间。
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
            # 找到第一个非水词，往前保留 lead_padding 秒缓冲
            new_start = max(0.0, float(word_info["start"]) - lead_padding)
            break

        # new_start 为 None 说明整段都是水词，跳过
        if new_start is None:
            continue
        # 只往后推（或缩短到 lead_padding 前），不往前缩
        if new_start > candidate.start_time + 0.02:
            candidate.start_time = new_start

    return candidates


def trim_trailing_fillers(
    candidates: list[CandidateClip],
    transcript: list[TranscriptSegment],
    trail_padding: float = 0.08,
) -> list[CandidateClip]:
    """把每个候选片段结尾连续的水词单字从时间上裁掉。

    仅调整 end_time，不拆分 segment。若整段都是水词则不裁（保留原样）。
    trail_padding: 最后一个实意词后保留的缓冲时间（秒），默认 80ms，避免切到字中间。
    """
    for candidate in candidates:
        if not candidate.segment_indexes:
            continue
        last_seg = transcript[candidate.segment_indexes[-1]]
        words = last_seg.words  # [{start, end, word}]
        if not words:
            continue

        new_end: float | None = None
        for word_info in reversed(words):
            w = word_info.get("word", "")
            if _is_leading_filler_word(w):
                continue
            # 找到最后一个非水词，往后保留 trail_padding 秒缓冲
            new_end = float(word_info["end"]) + trail_padding
            break

        # new_end 为 None 说明整段都是水词，跳过
        if new_end is None:
            continue
        # 只往前收缩，不往后延伸（不能超过原 end_time）
        if new_end < candidate.end_time - 0.02:
            candidate.end_time = new_end

    return candidates
