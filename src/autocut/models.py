from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable


def to_plain_dict(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, list):
        return [to_plain_dict(item) for item in value]
    if isinstance(value, dict):
        return {key: to_plain_dict(item) for key, item in value.items()}
    if hasattr(value, "__dataclass_fields__"):
        return to_plain_dict(asdict(value))
    # 函数 / 方法 / 其他可调用对象不可 JSON 序列化，统一丢弃为 None
    # （典型场景：PipelineRequest.on_progress 进度回调）
    if callable(value):
        return None
    return value


@dataclass
class MediaInfo:
    path: Path
    duration: float | None = None
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    audio_streams: int = 0
    video_streams: int = 0


@dataclass
class TranscriptSegment:
    start: float
    end: float
    text: str
    speaker: str | None = None
    words: list[dict[str, Any]] = field(default_factory=list)
    clean_text: str = ""
    filler_ratio: float = 0.0
    repeat_ratio: float = 0.0
    invalid_reasons: list[str] = field(default_factory=list)

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


@dataclass
class CandidateClip:
    clip_id: str
    source_video: Path
    start_time: float
    end_time: float
    transcript: str
    clean_transcript: str
    segment_indexes: list[int]
    score: int = 0
    dimension_scores: dict[str, int] = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)
    reason: str = ""
    risk: list[str] = field(default_factory=list)
    penalty: list[str] = field(default_factory=list)
    exports: dict[str, str] = field(default_factory=dict)
    edit_decision: dict[str, Any] = field(default_factory=dict)
    compact_duration: float | None = None
    removed_duration: float = 0.0
    status: str = "pending"

    @property
    def duration(self) -> float:
        return max(0.0, self.end_time - self.start_time)


@dataclass
class PipelineRequest:
    video_path: Path
    product: str
    selling_points: list[str] = field(default_factory=list)
    output_dir: Path = Path("data/runs/default")
    transcript_path: Path | None = None
    asr_engine: str = "transcript"
    asr_model: str = "small"
    asr_device: str = "cpu"
    asr_compute_type: str = "int8"
    asr_beam_size: int = 5
    brand_terms: list[str] = field(default_factory=list)
    brand_terms_path: Path | None = None
    use_default_brand_terms: bool = True
    language: str = "zh"
    min_duration: float = 12.0
    target_min_duration: float = 20.0
    target_max_duration: float = 30.0
    max_duration: float = 45.0
    min_start_time: float = 0.0
    max_overlap_ratio: float = 0.45
    max_candidates: int = 8
    export_compact: bool = True
    compact_padding: float = 0.08
    compact_merge_gap: float = 0.45
    # 进度回调：on_progress(stage: str, progress: float 0~1)
    on_progress: Callable[[str, float], None] | None = None


@dataclass
class PipelineResult:
    media: MediaInfo
    transcript: list[TranscriptSegment]
    candidates: list[CandidateClip]
    output_dir: Path
    warnings: list[str] = field(default_factory=list)
