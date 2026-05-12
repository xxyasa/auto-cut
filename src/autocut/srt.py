from __future__ import annotations

import re
from pathlib import Path

from .models import TranscriptSegment


TIMESTAMP_RE = re.compile(
    r"(?P<start>\d{2}:\d{2}:\d{2}[,.]\d{3})\s*-->\s*"
    r"(?P<end>\d{2}:\d{2}:\d{2}[,.]\d{3})"
)


def seconds_to_srt_time(seconds: float) -> str:
    seconds = max(0.0, seconds)
    millis = int(round(seconds * 1000))
    hours, rest = divmod(millis, 3_600_000)
    minutes, rest = divmod(rest, 60_000)
    secs, ms = divmod(rest, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{ms:03d}"


def srt_time_to_seconds(value: str) -> float:
    value = value.replace(",", ".")
    hours, minutes, rest = value.split(":")
    seconds, millis = rest.split(".")
    return (
        int(hours) * 3600
        + int(minutes) * 60
        + int(seconds)
        + int(millis.ljust(3, "0")[:3]) / 1000
    )


def write_srt(segments: list[TranscriptSegment], path: Path, offset: float = 0.0) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    for index, segment in enumerate(segments, 1):
        start = seconds_to_srt_time(segment.start - offset)
        end = seconds_to_srt_time(segment.end - offset)
        text = segment.clean_text or segment.text
        lines.extend([str(index), f"{start} --> {end}", text.strip(), ""])
    path.write_text("\n".join(lines), encoding="utf-8")


def parse_srt(path: Path) -> list[TranscriptSegment]:
    content = path.read_text(encoding="utf-8-sig")
    blocks = re.split(r"\n\s*\n", content.strip())
    segments: list[TranscriptSegment] = []
    for block in blocks:
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        if not lines:
            continue
        time_line_index = 0
        if len(lines) > 1 and lines[0].isdigit():
            time_line_index = 1
        match = TIMESTAMP_RE.search(lines[time_line_index])
        if not match:
            continue
        text = " ".join(lines[time_line_index + 1 :]).strip()
        segments.append(
            TranscriptSegment(
                start=srt_time_to_seconds(match.group("start")),
                end=srt_time_to_seconds(match.group("end")),
                text=text,
            )
        )
    return segments

