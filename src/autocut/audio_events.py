from __future__ import annotations

import math
import wave
from array import array
from pathlib import Path
from typing import Any

from .models import CandidateClip, TranscriptSegment


def detect_cough_like_events(
    audio_path: Path,
    candidate: CandidateClip,
    transcript_segments: list[TranscriptSegment],
    *,
    max_events: int = 5,
) -> list[dict[str, Any]]:
    """Detect short noisy bursts that ASR usually misses.

    This is intentionally conservative: events overlapping ASR word ranges are
    ignored so normal speech is not removed by the compact export.
    """

    if not audio_path.exists():
        return []
    try:
        sample_rate, samples = _read_wav_mono(audio_path)
    except (wave.Error, OSError, ValueError):
        return []
    if sample_rate <= 0 or not samples:
        return []

    start_index = max(0, int(candidate.start_time * sample_rate))
    end_index = min(len(samples), int(candidate.end_time * sample_rate))
    if end_index - start_index < int(0.3 * sample_rate):
        return []

    frame_size = max(1, int(0.025 * sample_rate))
    hop_size = max(1, int(0.010 * sample_rate))
    protected_ranges = _protected_speech_ranges(candidate, transcript_segments)
    frames = _analyze_frames(samples, sample_rate, start_index, end_index, frame_size, hop_size)
    if len(frames) < 5:
        return []

    rms_values = sorted(frame["rms"] for frame in frames)
    median_rms = _percentile(rms_values, 0.50)
    p90_rms = _percentile(rms_values, 0.90)
    threshold = max(0.025, median_rms * 4.0, p90_rms * 1.15)

    active_frames = [
        frame
        for frame in frames
        if frame["rms"] >= threshold
        and frame["diff_ratio"] >= 0.16
        and frame["zcr"] >= 0.035
        and not _overlaps_ranges(frame["start"], frame["end"], protected_ranges, min_overlap_ratio=0.15)
    ]
    events = _merge_active_frames(active_frames, max_gap=0.08)
    filtered: list[dict[str, Any]] = []
    for start, end, peak in events:
        duration = end - start
        if duration < 0.10 or duration > 0.95:
            continue
        if _overlaps_ranges(start, end, protected_ranges, min_overlap_ratio=0.10):
            continue
        filtered.append(
            {
                "text": "疑似咳嗽/突发噪声",
                "start": round(start, 3),
                "end": round(end, 3),
                "kind": "audio_cough_like",
                "confidence": round(min(0.95, peak / max(threshold, 1e-6) / 4), 3),
            }
        )
    return filtered[:max_events]


def _read_wav_mono(audio_path: Path) -> tuple[int, list[float]]:
    with wave.open(str(audio_path), "rb") as handle:
        channels = handle.getnchannels()
        sample_width = handle.getsampwidth()
        sample_rate = handle.getframerate()
        raw = handle.readframes(handle.getnframes())

    if sample_width != 2:
        raise ValueError("only 16-bit PCM wav is supported")
    values = array("h")
    values.frombytes(raw)
    if channels > 1:
        mono: list[float] = []
        for index in range(0, len(values), channels):
            mono.append(sum(values[index : index + channels]) / channels / 32768.0)
        return sample_rate, mono
    return sample_rate, [value / 32768.0 for value in values]


def _protected_speech_ranges(
    candidate: CandidateClip,
    transcript_segments: list[TranscriptSegment],
) -> list[tuple[float, float]]:
    ranges: list[tuple[float, float]] = []
    for segment in transcript_segments:
        if segment.end <= candidate.start_time or segment.start >= candidate.end_time:
            continue
        if segment.words:
            for word in segment.words:
                try:
                    start = float(word.get("start"))
                    end = float(word.get("end"))
                except (TypeError, ValueError):
                    continue
                if end <= start:
                    continue
                ranges.append((max(candidate.start_time, start - 0.06), min(candidate.end_time, end + 0.06)))
        else:
            ranges.append((max(candidate.start_time, segment.start), min(candidate.end_time, segment.end)))
    return _merge_ranges(ranges, max_gap=0.08)


def _analyze_frames(
    samples: list[float],
    sample_rate: int,
    start_index: int,
    end_index: int,
    frame_size: int,
    hop_size: int,
) -> list[dict[str, float]]:
    frames: list[dict[str, float]] = []
    for index in range(start_index, max(start_index, end_index - frame_size), hop_size):
        chunk = samples[index : index + frame_size]
        if not chunk:
            continue
        rms = math.sqrt(sum(sample * sample for sample in chunk) / len(chunk))
        sign_changes = sum(
            1 for left, right in zip(chunk, chunk[1:]) if (left >= 0 and right < 0) or (left < 0 and right >= 0)
        )
        zcr = sign_changes / max(1, len(chunk) - 1)
        diff = sum(abs(right - left) for left, right in zip(chunk, chunk[1:])) / max(1, len(chunk) - 1)
        frames.append(
            {
                "start": index / sample_rate,
                "end": min(end_index, index + frame_size) / sample_rate,
                "rms": rms,
                "zcr": zcr,
                "diff_ratio": diff / max(rms, 1e-6),
            }
        )
    return frames


def _merge_active_frames(frames: list[dict[str, float]], max_gap: float) -> list[tuple[float, float, float]]:
    if not frames:
        return []
    merged: list[tuple[float, float, float]] = []
    start = frames[0]["start"]
    end = frames[0]["end"]
    peak = frames[0]["rms"]
    for frame in frames[1:]:
        if frame["start"] - end <= max_gap:
            end = max(end, frame["end"])
            peak = max(peak, frame["rms"])
        else:
            merged.append((start, end, peak))
            start = frame["start"]
            end = frame["end"]
            peak = frame["rms"]
    merged.append((start, end, peak))
    return merged


def _overlaps_ranges(
    start: float,
    end: float,
    ranges: list[tuple[float, float]],
    *,
    min_overlap_ratio: float,
) -> bool:
    duration = max(1e-6, end - start)
    overlap = 0.0
    for range_start, range_end in ranges:
        overlap += max(0.0, min(end, range_end) - max(start, range_start))
    return overlap / duration >= min_overlap_ratio


def _merge_ranges(ranges: list[tuple[float, float]], max_gap: float) -> list[tuple[float, float]]:
    normalized = sorted((start, end) for start, end in ranges if end > start)
    if not normalized:
        return []
    merged = [normalized[0]]
    for start, end in normalized[1:]:
        last_start, last_end = merged[-1]
        if start - last_end <= max_gap:
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))
    return merged


def _percentile(sorted_values: list[float], percent: float) -> float:
    if not sorted_values:
        return 0.0
    index = min(len(sorted_values) - 1, max(0, int(round((len(sorted_values) - 1) * percent))))
    return sorted_values[index]
