from __future__ import annotations

import json
from pathlib import Path

from . import media
from .audio_events import detect_cough_like_events
from .jumpcut import build_compact_plan, build_compact_subtitles
from .models import CandidateClip, PipelineResult, TranscriptSegment, to_plain_dict
from .srt import write_srt


def export_pipeline_result(
    result: PipelineResult,
    *,
    audio_path: Path | None = None,
    export_compact: bool = True,
    compact_padding: float = 0.08,
    compact_merge_gap: float = 0.45,
) -> list[str]:
    warnings: list[str] = []
    result.output_dir.mkdir(parents=True, exist_ok=True)
    metadata_dir = result.output_dir / "metadata"
    subtitle_dir = result.output_dir / "subtitles"
    metadata_dir.mkdir(parents=True, exist_ok=True)
    subtitle_dir.mkdir(parents=True, exist_ok=True)

    write_srt(result.transcript, subtitle_dir / "source.srt")

    for candidate in result.candidates:
        candidate_segments = [
            segment
            for index, segment in enumerate(result.transcript)
            if index in set(candidate.segment_indexes)
        ]
        warnings.extend(
            export_candidate(
                candidate,
                candidate_segments,
                result.output_dir,
                audio_path=audio_path,
                export_compact=export_compact,
                compact_padding=compact_padding,
                compact_merge_gap=compact_merge_gap,
            )
        )

    summary_path = metadata_dir / "result.json"
    summary_path.write_text(
        json.dumps(to_plain_dict(result), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return warnings


def export_candidate(
    candidate: CandidateClip,
    transcript_segments: list[TranscriptSegment],
    output_dir: Path,
    *,
    audio_path: Path | None = None,
    export_compact: bool = True,
    compact_padding: float = 0.08,
    compact_merge_gap: float = 0.45,
) -> list[str]:
    warnings: list[str] = []
    clip_dir = output_dir / "clips"
    subtitle_dir = output_dir / "subtitles"
    frame_dir = output_dir / "frames"
    metadata_dir = output_dir / "metadata"
    clip_dir.mkdir(parents=True, exist_ok=True)
    subtitle_dir.mkdir(parents=True, exist_ok=True)
    frame_dir.mkdir(parents=True, exist_ok=True)
    metadata_dir.mkdir(parents=True, exist_ok=True)

    srt_path = subtitle_dir / f"{candidate.clip_id}.srt"
    write_srt(transcript_segments, srt_path, offset=candidate.start_time)
    candidate.exports["subtitle"] = str(srt_path)

    clip_path = clip_dir / f"{candidate.clip_id}.mp4"
    warning = media.export_clip(candidate.source_video, clip_path, candidate.start_time, candidate.end_time)
    if warning:
        warnings.append(f"{candidate.clip_id}: {warning}")
    else:
        candidate.exports["video"] = str(clip_path)

    if export_compact:
        compact_warning = export_compact_candidate(
            candidate,
            transcript_segments,
            output_dir,
            audio_path=audio_path,
            padding=compact_padding,
            merge_gap=compact_merge_gap,
        )
        if compact_warning:
            warnings.append(f"{candidate.clip_id}: {compact_warning}")

    cover_path = frame_dir / f"{candidate.clip_id}.jpg"
    warning = media.extract_cover(
        candidate.source_video,
        cover_path,
        candidate.start_time + candidate.duration / 2,
    )
    if warning:
        warnings.append(f"{candidate.clip_id}: {warning}")
    else:
        candidate.exports["cover"] = str(cover_path)

    metadata_path = metadata_dir / f"{candidate.clip_id}.json"
    candidate.exports["metadata"] = str(metadata_path)
    metadata_path.write_text(
        json.dumps(to_plain_dict(candidate), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return warnings


def export_compact_candidate(
    candidate: CandidateClip,
    transcript_segments: list[TranscriptSegment],
    output_dir: Path,
    *,
    audio_path: Path | None,
    padding: float,
    merge_gap: float,
) -> str | None:
    audio_removed_terms = (
        detect_cough_like_events(audio_path, candidate, transcript_segments) if audio_path is not None else []
    )
    plan = build_compact_plan(
        candidate,
        transcript_segments,
        extra_removed_terms=audio_removed_terms,
        padding=padding,
        merge_gap=merge_gap,
    )
    if not plan:
        return None

    clip_dir = output_dir / "clips_compact"
    subtitle_dir = output_dir / "subtitles_compact"
    metadata_dir = output_dir / "metadata"
    clip_dir.mkdir(parents=True, exist_ok=True)
    subtitle_dir.mkdir(parents=True, exist_ok=True)
    metadata_dir.mkdir(parents=True, exist_ok=True)

    compact_path = clip_dir / f"{candidate.clip_id}_compact.mp4"
    warning = media.export_clip_segments(candidate.source_video, compact_path, plan.keep_ranges)
    if warning:
        return warning

    compact_segments = build_compact_subtitles(transcript_segments, plan.keep_ranges)
    compact_srt_path = subtitle_dir / f"{candidate.clip_id}_compact.srt"
    write_srt(compact_segments, compact_srt_path)

    edit_path = metadata_dir / f"{candidate.clip_id}_edit.json"
    edit_decision = plan.to_dict()
    edit_path.write_text(json.dumps(edit_decision, ensure_ascii=False, indent=2), encoding="utf-8")

    candidate.exports["compact_video"] = str(compact_path)
    candidate.exports["compact_subtitle"] = str(compact_srt_path)
    candidate.exports["edit_decision"] = str(edit_path)
    candidate.edit_decision = edit_decision
    candidate.compact_duration = round(plan.compact_duration, 3)
    candidate.removed_duration = round(plan.removed_duration, 3)
    return None
