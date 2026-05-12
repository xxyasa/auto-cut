from __future__ import annotations

import math
import re
from pathlib import Path

from .models import CandidateClip, TranscriptSegment


def tokenize_keywords(product: str, selling_points: list[str]) -> list[str]:
    keywords = [product.strip()] if product.strip() else []
    keywords.extend(point.strip() for point in selling_points if point.strip())
    tokens: list[str] = []
    for keyword in keywords:
        tokens.append(keyword)
        tokens.extend(part for part in re.split(r"[\s,，、/|]+", keyword) if len(part) >= 2)
        tokens.extend(expand_keyword(keyword))
    return sorted(set(tokens), key=len, reverse=True)


def expand_keyword(keyword: str) -> list[str]:
    expanded: list[str] = []
    if "伞" in keyword:
        expanded.extend([keyword.replace("伞", "傘"), "雨伞", "雨傘", "伞面", "傘面"])
    if "傘" in keyword:
        expanded.extend([keyword.replace("傘", "伞"), "雨伞", "雨傘", "伞面", "傘面"])
    upper = keyword.upper()
    if "PEO" in upper:
        expanded.extend(["PEO", "POE", "pEo", "PoE"])
    if "POE" in upper:
        expanded.extend(["PEO", "POE", "pEo", "PoE"])
    if "加固" in keyword:
        expanded.extend(["伞骨", "傘骨", "骨架", "纤维骨", "纖維骨"])
    if "图案" in keyword:
        expanded.extend(["图案", "圖案", "学院", "動物形象", "动物形象", "色块"])
    return expanded


def build_candidates(
    segments: list[TranscriptSegment],
    source_video: Path,
    product: str,
    selling_points: list[str],
    min_duration: float = 12.0,
    target_min_duration: float = 20.0,
    target_max_duration: float = 30.0,
    max_duration: float = 45.0,
    min_start_time: float = 0.0,
    max_overlap_ratio: float = 0.45,
    max_candidates: int = 8,
) -> list[CandidateClip]:
    valid_segments = [
        (index, segment)
        for index, segment in enumerate(segments)
        if (segment.clean_text or segment.text).strip()
    ]
    if not valid_segments:
        return []

    keywords = tokenize_keywords(product, selling_points)
    raw_candidates: list[tuple[float, CandidateClip]] = []

    for start_index in range(len(valid_segments)):
        indexes: list[int] = []
        text_parts: list[str] = []
        clean_parts: list[str] = []
        start_time = valid_segments[start_index][1].start
        if start_time < min_start_time and len(valid_segments) > 1:
            continue
        end_time = start_time
        for end_index in range(start_index, len(valid_segments)):
            original_index, segment = valid_segments[end_index]
            indexes.append(original_index)
            text_parts.append(segment.text)
            clean_parts.append(segment.clean_text or segment.text)
            end_time = segment.end
            duration = end_time - start_time
            if duration > max_duration:
                break
            if duration >= min_duration:
                raw_text = "".join(text_parts).strip()
                clean_text = "".join(clean_parts).strip()
                rank = candidate_pre_rank(clean_text, duration, keywords, target_min_duration, target_max_duration)
                clip_id = f"{source_video.stem}_{len(raw_candidates) + 1:03d}"
                raw_candidates.append(
                    (
                        rank,
                        CandidateClip(
                            clip_id=clip_id,
                            source_video=source_video,
                            start_time=start_time,
                            end_time=end_time,
                            transcript=raw_text,
                            clean_transcript=clean_text,
                            segment_indexes=indexes.copy(),
                        ),
                    )
                )

    if not raw_candidates:
        all_segments = [segment for _, segment in valid_segments]
        start_time = all_segments[0].start
        end_time = all_segments[-1].end
        raw_candidates.append(
            (
                0.0,
                CandidateClip(
                    clip_id=f"{source_video.stem}_001",
                    source_video=source_video,
                    start_time=start_time,
                    end_time=end_time,
                    transcript="".join(segment.text for segment in all_segments).strip(),
                    clean_transcript="".join(segment.clean_text or segment.text for segment in all_segments).strip(),
                    segment_indexes=[index for index, _ in valid_segments],
                ),
            )
        )

    raw_candidates.sort(key=lambda item: item[0], reverse=True)
    selected: list[CandidateClip] = []
    for _, candidate in raw_candidates:
        if any(overlap_ratio(candidate, kept) > max_overlap_ratio for kept in selected):
            continue
        candidate.clip_id = f"{source_video.stem}_{len(selected) + 1:03d}"
        selected.append(candidate)
        if len(selected) >= max_candidates:
            break
    return selected


def candidate_pre_rank(
    clean_text: str,
    duration: float,
    keywords: list[str],
    target_min_duration: float,
    target_max_duration: float,
) -> float:
    if target_min_duration <= duration <= target_max_duration:
        duration_score = 1.0
    else:
        target_center = (target_min_duration + target_max_duration) / 2
        duration_score = max(0.0, 1.0 - abs(duration - target_center) / max(target_center, 1.0))
    match_text = normalize_for_match(clean_text)
    keyword_hits = sum(1 for keyword in keywords if keyword and normalize_for_match(keyword) in match_text)
    keyword_score = min(1.0, keyword_hits / max(1, min(3, len(keywords))))
    density_score = min(1.0, len(clean_text) / max(1.0, duration * 3.0))
    opening_penalty = 0.0
    if clean_text.startswith(("欢迎", "来来来", "看一下", "等一下", "听得到")):
        opening_penalty = 0.12
    return max(0.0, 0.45 * duration_score + 0.35 * keyword_score + 0.20 * density_score - opening_penalty)


def normalize_for_match(text: str) -> str:
    return text.lower().replace("傘", "伞").replace("poe", "peo")


def overlap_ratio(left: CandidateClip, right: CandidateClip) -> float:
    overlap = max(0.0, min(left.end_time, right.end_time) - max(left.start_time, right.start_time))
    union = max(left.end_time, right.end_time) - min(left.start_time, right.start_time)
    return overlap / union if union > 0 else math.inf
