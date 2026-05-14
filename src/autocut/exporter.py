from __future__ import annotations

import json
import re
import shutil
import zipfile
from pathlib import Path

from . import media
from .audio_events import detect_cough_like_events
from .jumpcut import build_compact_plan, build_compact_subtitles
from .models import CandidateClip, PipelineResult, TranscriptSegment, to_plain_dict
from .srt import write_srt


SEGMENT_FILENAME_RE = re.compile(r"[^0-9A-Za-z\u4e00-\u9fff _-]+")


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


def export_segments_zip(
    source_video: Path,
    output_zip: Path,
    segments: list[dict],
) -> str | None:
    """把一组源视频时间段分别裁切为 MP4，并打包为 ZIP。

    `segments` 中至少需要 `start` 和 `end`。可选字段：`text`、`summary`、`id`。
    ZIP 内固定包含 `manifest.json`，用于让业务侧核对顺序和来源。
    """
    if not source_video.exists():
        return f"source video not found: {source_video}"
    normalized = _normalize_zip_segments(segments)
    if not normalized:
        return "no segments for zip export"

    output_zip.parent.mkdir(parents=True, exist_ok=True)
    temp_root = output_zip.parent / f".{output_zip.stem}_tmp"
    temp_zip = output_zip.with_suffix(output_zip.suffix + ".tmp")
    if temp_root.exists():
        shutil.rmtree(temp_root, ignore_errors=True)
    temp_zip.unlink(missing_ok=True)
    temp_root.mkdir(parents=True, exist_ok=True)

    manifest_segments: list[dict] = []
    warning: str | None = None
    try:
        with zipfile.ZipFile(temp_zip, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for index, segment in enumerate(normalized, 1):
                entry_name = _segment_entry_name(index, segment)
                segment_path = temp_root / entry_name
                warning = media.export_clip(
                    source_video,
                    segment_path,
                    float(segment["start"]),
                    float(segment["end"]),
                )
                if warning:
                    break
                archive.write(segment_path, entry_name)
                manifest_segments.append(
                    {
                        "index": index,
                        "file": entry_name,
                        "id": segment.get("id", ""),
                        "source_start": round(float(segment["start"]), 3),
                        "source_end": round(float(segment["end"]), 3),
                        "duration": round(float(segment["end"]) - float(segment["start"]), 3),
                        "summary": segment.get("summary", ""),
                        "text": segment.get("text", ""),
                    }
                )

            if warning:
                return warning

            manifest = {
                "schema_version": 1,
                "source_video": str(source_video),
                "segment_count": len(manifest_segments),
                "segments": manifest_segments,
            }
            archive.writestr(
                "manifest.json",
                json.dumps(manifest, ensure_ascii=False, indent=2),
            )
        temp_zip.replace(output_zip)
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)
        temp_zip.unlink(missing_ok=True)
    return None


def _normalize_zip_segments(segments: list[dict]) -> list[dict]:
    normalized: list[dict] = []
    for segment in segments:
        try:
            start = float(segment.get("start"))
            end = float(segment.get("end"))
        except (TypeError, ValueError):
            continue
        if end <= start:
            continue
        text = str(segment.get("text") or segment.get("transcript") or "").strip()
        summary = str(segment.get("summary") or text or segment.get("reason") or "segment").strip()
        normalized.append(
            {
                **segment,
                "start": round(start, 3),
                "end": round(end, 3),
                "text": text,
                "summary": summary,
            }
        )
    return normalized


def _segment_entry_name(index: int, segment: dict) -> str:
    start = _format_zip_time(float(segment["start"]))
    end = _format_zip_time(float(segment["end"]))
    label = _safe_segment_label(str(segment.get("summary") or segment.get("text") or "segment"))
    return f"{index:03d}_{start}_{end}_{label}.mp4"


def _format_zip_time(seconds: float) -> str:
    millis = int(round(max(0.0, seconds) * 1000))
    total_seconds, ms = divmod(millis, 1000)
    minutes, sec = divmod(total_seconds, 60)
    hours, minute = divmod(minutes, 60)
    return f"{hours:02d}-{minute:02d}-{sec:02d}.{ms:03d}"


def _safe_segment_label(label: str) -> str:
    safe = SEGMENT_FILENAME_RE.sub("", label)
    safe = re.sub(r"\s+", "_", safe).strip("._- ")
    return (safe or "segment")[:24]
