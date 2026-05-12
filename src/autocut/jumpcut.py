from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .models import CandidateClip, TranscriptSegment


SHORT_FILLER_WORDS = {
    "嗯",
    "啊",
    "呃",
    "额",
    "呐",
    "唔",
    "诶",
    "咳",
    "咳嗽",
    "咳咳",
    "咳嗽声",
    "呃呃",
    "嗯嗯",
    "啊啊",
    "额额",
    "um",
    "uh",
    "er",
    "ah",
}

COUGH_WORDS = {
    "咳",
    "咳嗽",
    "咳咳",
    "咳嗽声",
    "咳一下",
}

LIVE_FILLER_PHRASES = [
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
    "咱们什么呢",
    "我们什么呢",
    "那么什么呢",
    "那什么呢",
    "什么呢",
    "这样子的",
    "这样子",
    "这样子的呢",
    "这样子呢",
    "这样的",
    "这么一个",
    "这么一款",
    "这么一种",
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
    "整个的",
    "给大家去",
    "去进行一个",
    "进行一个",
    "我们的一个",
    "它的一个",
    "整体的一个",
    "就是说",
    "所以说",
    "或者说",
    "对不对",
    "是吧",
    "等等",
]

CONTEXT_FILLER_PHRASES = {"什么呢"}
SEMANTIC_QUESTION_PREFIXES = {"为", "是", "叫", "指", "有", "像"}
WORD_CLEAN_RE = re.compile(r"[\s,，。.!！?？、~～…:：;；\"'“”‘’()\[\]{}<>《》]+")


@dataclass
class CompactPlan:
    keep_ranges: list[dict[str, float]]
    removed_ranges: list[dict[str, float]]
    removed_terms: list[dict[str, Any]]
    original_duration: float
    compact_duration: float
    removed_duration: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": "word_timestamp_jumpcut",
            "keep_ranges": self.keep_ranges,
            "removed_ranges": self.removed_ranges,
            "removed_terms": self.removed_terms,
            "original_duration": round(self.original_duration, 3),
            "compact_duration": round(self.compact_duration, 3),
            "removed_duration": round(self.removed_duration, 3),
        }


def build_compact_plan(
    candidate: CandidateClip,
    transcript_segments: list[TranscriptSegment],
    *,
    extra_removed_terms: list[dict[str, Any]] | None = None,
    padding: float = 0.08,
    merge_gap: float = 0.45,
    min_removed_duration: float = 0.35,
) -> CompactPlan | None:
    """Build a jump-cut plan from word timestamps.

    The original clip remains continuous. This plan is for an optional compact
    export that removes isolated filler sounds and longer unspoken gaps.
    """

    speech_ranges: list[tuple[float, float]] = []
    filler_ranges: list[tuple[float, float]] = []
    removed_terms: list[dict[str, Any]] = []

    for item in extra_removed_terms or []:
        try:
            start = float(item["start"])
            end = float(item["end"])
        except (KeyError, TypeError, ValueError):
            continue
        clipped = _clip_range(
            start - min(0.05, padding),
            end + min(0.05, padding),
            candidate.start_time,
            candidate.end_time,
        )
        if clipped[1] <= clipped[0]:
            continue
        filler_ranges.append(clipped)
        removed_terms.append(
            {
                "text": str(item.get("text") or "audio_event"),
                "start": round(max(candidate.start_time, start), 3),
                "end": round(min(candidate.end_time, end), 3),
                "kind": str(item.get("kind") or "audio_event"),
            }
        )

    for segment in transcript_segments:
        if segment.end <= candidate.start_time or segment.start >= candidate.end_time:
            continue
        segment_words = _valid_words(segment)
        if not segment_words:
            speech_ranges.append(
                _clip_range(segment.start - padding, segment.end + padding, candidate.start_time, candidate.end_time)
            )
            continue

        for phrase_range in _find_phrase_ranges(segment_words, candidate):
            clipped = _clip_range(
                phrase_range["start"] - min(0.06, padding),
                phrase_range["end"] + min(0.06, padding),
                candidate.start_time,
                candidate.end_time,
            )
            filler_ranges.append(clipped)
            removed_terms.append(
                {
                    "text": phrase_range["text"],
                    "start": round(phrase_range["start"], 3),
                    "end": round(phrase_range["end"], 3),
                    "kind": "live_filler_phrase",
                }
            )

        for word in segment_words:
            start = float(word["start"])
            end = float(word["end"])
            if end <= candidate.start_time or start >= candidate.end_time:
                continue
            text = str(word.get("word") or "")
            clipped = _clip_range(start, end, candidate.start_time, candidate.end_time)
            if clipped[1] <= clipped[0]:
                continue
            if is_short_filler(text):
                filler_range = _clip_range(
                    clipped[0] - min(0.04, padding),
                    clipped[1] + min(0.04, padding),
                    candidate.start_time,
                    candidate.end_time,
                )
                filler_ranges.append(filler_range)
                kind = "cough" if normalize_word(text) in COUGH_WORDS else "short_filler"
                removed_terms.append(
                    {"text": text.strip(), "start": round(clipped[0], 3), "end": round(clipped[1], 3), "kind": kind}
                )
            else:
                speech_ranges.append(
                    _clip_range(clipped[0] - padding, clipped[1] + padding, candidate.start_time, candidate.end_time)
                )

    keep_ranges = _merge_ranges(speech_ranges, max_gap=merge_gap)
    keep_ranges = _subtract_ranges(keep_ranges, _merge_ranges(filler_ranges, max_gap=0.02))
    keep_ranges = [(start, end) for start, end in keep_ranges if end - start >= 0.12]

    if not keep_ranges:
        return None

    original_duration = candidate.duration
    compact_duration = sum(end - start for start, end in keep_ranges)
    removed_duration = max(0.0, original_duration - compact_duration)
    effective_min_removed_duration = _effective_min_removed_duration(removed_terms, min_removed_duration)
    if removed_duration < effective_min_removed_duration:
        return None

    rounded_keep = [{"start": round(start, 3), "end": round(end, 3)} for start, end in keep_ranges]
    removed_ranges = [
        {"start": round(start, 3), "end": round(end, 3)}
        for start, end in _complement_ranges(candidate.start_time, candidate.end_time, keep_ranges)
        if end - start >= 0.05
    ]
    return CompactPlan(
        keep_ranges=rounded_keep,
        removed_ranges=removed_ranges,
        removed_terms=removed_terms,
        original_duration=original_duration,
        compact_duration=compact_duration,
        removed_duration=removed_duration,
    )


def build_compact_subtitles(
    transcript_segments: list[TranscriptSegment],
    keep_ranges: list[dict[str, float]],
) -> list[TranscriptSegment]:
    ranges = [(float(item["start"]), float(item["end"])) for item in keep_ranges]
    subtitles: list[TranscriptSegment] = []
    for segment in transcript_segments:
        for keep_start, keep_end in ranges:
            overlap_start = max(segment.start, keep_start)
            overlap_end = min(segment.end, keep_end)
            if overlap_end - overlap_start < 0.12:
                continue
            subtitles.append(
                TranscriptSegment(
                    start=_map_time(overlap_start, ranges),
                    end=_map_time(overlap_end, ranges),
                    text=segment.text,
                    speaker=segment.speaker,
                    clean_text=segment.clean_text,
                )
            )
    return subtitles


def is_short_filler(text: str) -> bool:
    normalized = normalize_word(text)
    if not normalized:
        return False
    if normalized in SHORT_FILLER_WORDS:
        return True
    if len(normalized) <= 4 and len(set(normalized)) == 1 and normalized[0] in {"嗯", "啊", "呃", "额", "咳"}:
        return True
    return False


def normalize_word(text: str) -> str:
    return WORD_CLEAN_RE.sub("", text).strip().lower()


def _find_phrase_ranges(
    words: list[dict[str, Any]],
    candidate: CandidateClip,
) -> list[dict[str, Any]]:
    combined = ""
    tokens: list[dict[str, Any]] = []
    for word in words:
        token = normalize_word(str(word.get("word") or ""))
        if not token:
            continue
        start_offset = len(combined)
        combined += token
        tokens.append(
            {
                "text": token,
                "start_offset": start_offset,
                "end_offset": len(combined),
                "start": float(word["start"]),
                "end": float(word["end"]),
            }
        )

    if not combined:
        return []

    ranges: list[dict[str, Any]] = []
    for phrase in LIVE_FILLER_PHRASES:
        normalized_phrase = normalize_word(phrase)
        if not normalized_phrase:
            continue
        search_index = 0
        while True:
            match_start = combined.find(normalized_phrase, search_index)
            if match_start < 0:
                break
            match_end = match_start + len(normalized_phrase)
            if _is_semantic_question_match(normalized_phrase, combined, match_start):
                search_index = match_start + 1
                continue
            matched_tokens = [
                token
                for token in tokens
                if token["end_offset"] > match_start and token["start_offset"] < match_end
            ]
            if matched_tokens and _has_precise_phrase_timing(matched_tokens, match_start, match_end):
                start = max(candidate.start_time, matched_tokens[0]["start"])
                end = min(candidate.end_time, matched_tokens[-1]["end"])
                if end > start:
                    ranges.append({"text": phrase, "start": start, "end": end})
            search_index = match_start + 1
    return _dedupe_phrase_ranges(ranges)


def _is_semantic_question_match(phrase: str, combined: str, match_start: int) -> bool:
    if phrase not in CONTEXT_FILLER_PHRASES or match_start <= 0:
        return False
    return combined[match_start - 1] in SEMANTIC_QUESTION_PREFIXES


def _has_precise_phrase_timing(
    tokens: list[dict[str, Any]],
    match_start: int,
    match_end: int,
) -> bool:
    if len(tokens) != 1:
        return True
    token = tokens[0]
    return token["start_offset"] == match_start and token["end_offset"] == match_end


def _dedupe_phrase_ranges(ranges: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped: list[dict[str, Any]] = []
    for item in sorted(ranges, key=lambda value: (value["start"], -(value["end"] - value["start"]))):
        if any(item["start"] >= kept["start"] and item["end"] <= kept["end"] for kept in deduped):
            continue
        deduped.append(item)
    return deduped


def _effective_min_removed_duration(removed_terms: list[dict[str, Any]], fallback: float) -> float:
    if any(item.get("kind") in {"cough", "live_filler_phrase"} for item in removed_terms):
        return 0.12
    return fallback


def _valid_words(segment: TranscriptSegment) -> list[dict[str, Any]]:
    words: list[dict[str, Any]] = []
    for word in segment.words:
        try:
            start = float(word.get("start"))
            end = float(word.get("end"))
        except (TypeError, ValueError):
            continue
        if end <= start:
            continue
        words.append({**word, "start": start, "end": end})
    return words


def _clip_range(start: float, end: float, lower: float, upper: float) -> tuple[float, float]:
    return max(lower, start), min(upper, end)


def _merge_ranges(ranges: list[tuple[float, float]], max_gap: float) -> list[tuple[float, float]]:
    normalized = sorted((start, end) for start, end in ranges if end > start)
    if not normalized:
        return []
    merged: list[tuple[float, float]] = [normalized[0]]
    for start, end in normalized[1:]:
        last_start, last_end = merged[-1]
        if start - last_end <= max_gap:
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))
    return merged


def _subtract_ranges(
    ranges: list[tuple[float, float]],
    cuts: list[tuple[float, float]],
) -> list[tuple[float, float]]:
    result = ranges
    for cut_start, cut_end in cuts:
        next_ranges: list[tuple[float, float]] = []
        for start, end in result:
            if cut_end <= start or cut_start >= end:
                next_ranges.append((start, end))
                continue
            if cut_start > start:
                next_ranges.append((start, cut_start))
            if cut_end < end:
                next_ranges.append((cut_end, end))
        result = next_ranges
    return [(start, end) for start, end in result if end > start]


def _complement_ranges(
    start: float,
    end: float,
    keep_ranges: list[tuple[float, float]],
) -> list[tuple[float, float]]:
    removed: list[tuple[float, float]] = []
    cursor = start
    for keep_start, keep_end in keep_ranges:
        if keep_start > cursor:
            removed.append((cursor, keep_start))
        cursor = max(cursor, keep_end)
    if cursor < end:
        removed.append((cursor, end))
    return removed


def _map_time(timestamp: float, keep_ranges: list[tuple[float, float]]) -> float:
    compact_time = 0.0
    for start, end in keep_ranges:
        if timestamp <= start:
            return compact_time
        if start <= timestamp <= end:
            return compact_time + (timestamp - start)
        compact_time += end - start
    return compact_time
