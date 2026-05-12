from __future__ import annotations

import json
from pathlib import Path

from . import media
from .asr import create_asr_engine
from .cleaning import clean_segments, trim_leading_fillers
from .exporter import export_pipeline_result
from .lexicon import build_asr_terms, load_terms_file
from .models import PipelineRequest, PipelineResult, to_plain_dict
from .scoring import score_candidates
from .segment import build_candidates


class LiveClipPipeline:
    def run(self, request: PipelineRequest) -> PipelineResult:
        def progress(stage: str, pct: float) -> None:
            if request.on_progress:
                request.on_progress(stage, pct)

        warnings: list[str] = []
        video_path = request.video_path.resolve()
        if not video_path.exists():
            raise FileNotFoundError(video_path)

        progress("探测视频信息", 0.05)
        info = media.probe_video(video_path)
        if info.duration is None:
            warnings.append("ffprobe not found or duration unavailable")

        progress("提取音频", 0.10)
        audio_path = request.output_dir / "audio" / f"{video_path.stem}.wav"
        audio_warning = media.extract_audio(video_path, audio_path)
        if audio_warning:
            warnings.append(audio_warning)

        progress("ASR 语音识别", 0.15)
        asr_engine = create_asr_engine(
            request.asr_engine,
            request.transcript_path,
            request.asr_model,
            request.asr_device,
            request.asr_compute_type,
            request.asr_beam_size,
        )
        asr_terms = build_asr_terms(
            request.product,
            request.selling_points,
            extra_terms=request.brand_terms + load_terms_file(request.brand_terms_path),
            include_defaults=request.use_default_brand_terms,
        )
        transcript = asr_engine.transcribe(audio_path, language=request.language, asr_terms=asr_terms)
        transcript = clean_segments(transcript)

        progress("构建候选片段", 0.65)
        candidates = build_candidates(
            transcript,
            source_video=video_path,
            product=request.product,
            selling_points=request.selling_points,
            min_duration=request.min_duration,
            target_min_duration=request.target_min_duration,
            target_max_duration=request.target_max_duration,
            max_duration=request.max_duration,
            min_start_time=request.min_start_time,
            max_overlap_ratio=request.max_overlap_ratio,
            max_candidates=request.max_candidates,
        )

        progress("评分与裁剪", 0.75)
        candidates = score_candidates(candidates, request.product, request.selling_points)
        candidates = trim_leading_fillers(candidates, transcript)

        progress("导出轨道文件", 0.80)
        result = PipelineResult(
            media=info,
            transcript=transcript,
            candidates=candidates,
            output_dir=request.output_dir,
            warnings=warnings,
        )
        export_warnings = export_pipeline_result(
            result,
            audio_path=audio_path,
            export_compact=request.export_compact,
            compact_padding=request.compact_padding,
            compact_merge_gap=request.compact_merge_gap,
        )
        result.warnings.extend(export_warnings)
        self._write_request(request)
        progress("pipeline 完成", 0.95)
        return result

    def _write_request(self, request: PipelineRequest) -> None:
        metadata_dir = request.output_dir / "metadata"
        metadata_dir.mkdir(parents=True, exist_ok=True)
        (metadata_dir / "request.json").write_text(
            json.dumps(to_plain_dict(request), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
