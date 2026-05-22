from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from .models import MediaInfo


class MediaToolError(RuntimeError):
    pass


DEFAULT_EXPORT_AUDIO_GAIN_DB = 20.0
ENV_EXPORT_AUDIO_GAIN_DB = "AUTOCUT_EXPORT_AUDIO_GAIN_DB"


def has_binary(name: str) -> bool:
    return executable(name) is not None


def executable(name: str) -> str | None:
    found = shutil.which(name)
    if found:
        return found
    if name == "ffmpeg":
        try:
            import imageio_ffmpeg
        except ImportError:
            return None
        return imageio_ffmpeg.get_ffmpeg_exe()
    return None


def run_command(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, capture_output=True, text=True, check=False)


def export_audio_filter_args() -> list[str]:
    raw = os.environ.get(ENV_EXPORT_AUDIO_GAIN_DB, str(DEFAULT_EXPORT_AUDIO_GAIN_DB)).strip()
    try:
        gain_db = float(raw)
    except ValueError:
        gain_db = DEFAULT_EXPORT_AUDIO_GAIN_DB
    if gain_db == 0:
        return []
    return ["-filter:a", f"volume={gain_db:g}dB"]


def probe_video(video_path: Path) -> MediaInfo:
    ffprobe = executable("ffprobe")
    if not ffprobe:
        return probe_video_with_ffmpeg(video_path)

    result = run_command(
        [
            ffprobe,
            "-v",
            "error",
            "-show_streams",
            "-show_format",
            "-of",
            "json",
            str(video_path),
        ]
    )
    if result.returncode != 0:
        raise MediaToolError(result.stderr.strip() or "ffprobe failed")

    data = json.loads(result.stdout)
    info = MediaInfo(path=video_path)
    if "format" in data and data["format"].get("duration"):
        info.duration = float(data["format"]["duration"])

    for stream in data.get("streams", []):
        if stream.get("codec_type") == "video":
            info.video_streams += 1
            info.width = stream.get("width")
            info.height = stream.get("height")
            rate = stream.get("avg_frame_rate") or stream.get("r_frame_rate")
            if rate and rate != "0/0":
                numerator, denominator = rate.split("/")
                info.fps = float(numerator) / float(denominator)
        elif stream.get("codec_type") == "audio":
            info.audio_streams += 1
    return info


def probe_video_with_ffmpeg(video_path: Path) -> MediaInfo:
    ffmpeg = executable("ffmpeg")
    info = MediaInfo(path=video_path)
    if not ffmpeg:
        return info

    result = run_command([ffmpeg, "-i", str(video_path)])
    output = "\n".join([result.stdout or "", result.stderr or ""])

    duration_match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", output)
    if duration_match:
        hours, minutes, seconds = duration_match.groups()
        info.duration = int(hours) * 3600 + int(minutes) * 60 + float(seconds)

    video_match = re.search(r"Video:.*?(\d{2,5})x(\d{2,5})", output)
    if video_match:
        info.video_streams = 1
        info.width = int(video_match.group(1))
        info.height = int(video_match.group(2))

    if "Audio:" in output:
        info.audio_streams = 1
    return info


def extract_audio(video_path: Path, audio_path: Path) -> str | None:
    ffmpeg = executable("ffmpeg")
    if not ffmpeg:
        return "ffmpeg not found; skip audio extraction"
    audio_path.parent.mkdir(parents=True, exist_ok=True)
    result = run_command(
        [
            ffmpeg,
            "-y",
            "-i",
            str(video_path),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            str(audio_path),
        ]
    )
    if result.returncode != 0:
        return result.stderr.strip() or "ffmpeg audio extraction failed"
    return None


def export_clip(video_path: Path, output_path: Path, start: float, end: float) -> str | None:
    ffmpeg = executable("ffmpeg")
    if not ffmpeg:
        return "ffmpeg not found; skip clip export"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    duration = max(0.0, end - start)
    result = run_command(
        [
            ffmpeg,
            "-y",
            "-ss",
            f"{start:.3f}",
            "-i",
            str(video_path),
            "-t",
            f"{duration:.3f}",
            "-map",
            "0:v:0?",
            "-map",
            "0:a:0?",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "20",
            *export_audio_filter_args(),
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-movflags",
            "+faststart",
            str(output_path),
        ]
    )
    if result.returncode != 0:
        fallback = run_command(
            [
                ffmpeg,
                "-y",
                "-ss",
                f"{start:.3f}",
                "-i",
                str(video_path),
                "-t",
                f"{duration:.3f}",
                "-c",
                "copy",
                str(output_path),
            ]
        )
        if fallback.returncode != 0:
            return result.stderr.strip() or fallback.stderr.strip() or "ffmpeg clip export failed"
    return None


def export_clip_segments(
    video_path: Path,
    output_path: Path,
    ranges: list[dict[str, float]],
) -> str | None:
    ffmpeg = executable("ffmpeg")
    if not ffmpeg:
        return "ffmpeg not found; skip compact clip export"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if not ranges:
        return "no ranges for compact clip export"

    temp_dir = tempfile.mkdtemp(prefix=f"{output_path.stem}_parts_", dir=str(output_path.parent))
    try:
        temp_root = Path(temp_dir)
        segment_paths: list[Path] = []
        for index, item in enumerate(ranges):
            start = float(item["start"])
            end = float(item["end"])
            if end <= start:
                continue
            segment_path = temp_root / f"part_{index:03d}.mp4"
            warning = export_clip(video_path, segment_path, start, end)
            if warning:
                return warning
            segment_paths.append(segment_path)

        if not segment_paths:
            return "no valid ranges for compact clip export"

        concat_path = temp_root / "concat.txt"
        concat_path.write_text(
            "\n".join(f"file '{_concat_path(path)}'" for path in segment_paths),
            encoding="utf-8",
        )
        result = run_command(
            [
                ffmpeg,
                "-y",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(concat_path),
                "-c",
                "copy",
                "-movflags",
                "+faststart",
                str(output_path),
            ]
        )
        if result.returncode != 0:
            return result.stderr.strip() or "ffmpeg compact concat failed"
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)
    return None


def _concat_path(path: Path) -> str:
    return str(path.resolve()).replace("\\", "/").replace("'", "'\\''")


def extract_cover(video_path: Path, output_path: Path, timestamp: float) -> str | None:
    ffmpeg = executable("ffmpeg")
    if not ffmpeg:
        return "ffmpeg not found; skip cover export"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    result = run_command(
        [
            ffmpeg,
            "-y",
            "-ss",
            f"{timestamp:.3f}",
            "-i",
            str(video_path),
            "-frames:v",
            "1",
            str(output_path),
        ]
    )
    if result.returncode != 0:
        return result.stderr.strip() or "ffmpeg cover export failed"
    return None
