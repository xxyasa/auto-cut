from __future__ import annotations

import json
from pathlib import Path
from typing import Protocol

from .lexicon import normalize_transcript_segments, terms_to_hotwords, terms_to_prompt
from .models import TranscriptSegment
from .srt import parse_srt


class ASREngine(Protocol):
    def transcribe(
        self,
        audio_path: Path,
        language: str = "zh",
        asr_terms: list[str] | None = None,
    ) -> list[TranscriptSegment]:
        ...


def load_transcript_file(path: Path) -> list[TranscriptSegment]:
    suffix = path.suffix.lower()
    if suffix == ".srt":
        return parse_srt(path)
    if suffix != ".json":
        raise ValueError(f"Unsupported transcript format: {path.suffix}")

    data = json.loads(path.read_text(encoding="utf-8"))
    raw_segments = data.get("segments", data) if isinstance(data, dict) else data
    if not isinstance(raw_segments, list):
        raise ValueError("Transcript JSON must be a list or contain a 'segments' list")

    segments: list[TranscriptSegment] = []
    for item in raw_segments:
        segments.append(
            TranscriptSegment(
                start=float(item["start"]),
                end=float(item["end"]),
                text=str(item["text"]),
                speaker=item.get("speaker"),
                words=item.get("words") or [],
            )
        )
    return segments


class TranscriptFileEngine:
    def __init__(self, transcript_path: Path):
        self.transcript_path = transcript_path

    def transcribe(
        self,
        audio_path: Path,
        language: str = "zh",
        asr_terms: list[str] | None = None,
    ) -> list[TranscriptSegment]:
        return load_transcript_file(self.transcript_path)


class FasterWhisperEngine:
    def __init__(
        self,
        model_size: str = "small",
        device: str = "cpu",
        compute_type: str = "int8",
        beam_size: int = 5,
    ):
        self.model_size = model_size
        self.device = device
        self.compute_type = compute_type
        self.beam_size = beam_size

    def transcribe(
        self,
        audio_path: Path,
        language: str = "zh",
        asr_terms: list[str] | None = None,
    ) -> list[TranscriptSegment]:
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise RuntimeError("Install faster-whisper or use --transcript first") from exc

        model = WhisperModel(self.model_size, device=self.device, compute_type=self.compute_type)
        segments, _ = model.transcribe(
            str(audio_path),
            language=language,
            beam_size=self.beam_size,
            word_timestamps=True,
            vad_filter=True,
            initial_prompt=terms_to_prompt(asr_terms or []) or None,
            hotwords=terms_to_hotwords(asr_terms or []) or None,
        )
        result: list[TranscriptSegment] = []
        for segment in segments:
            words = []
            for word in segment.words or []:
                words.append({"start": word.start, "end": word.end, "word": word.word})
            result.append(
                TranscriptSegment(
                    start=float(segment.start),
                    end=float(segment.end),
                    text=segment.text.strip(),
                    words=words,
                )
            )
        return normalize_transcript_segments(result)


class FunASREngine:
    def __init__(self, model: str = "paraformer-zh", vad_model: str = "fsmn-vad", punc_model: str = "ct-punc"):
        self.model = model
        self.vad_model = vad_model
        self.punc_model = punc_model

    def transcribe(
        self,
        audio_path: Path,
        language: str = "zh",
        asr_terms: list[str] | None = None,
    ) -> list[TranscriptSegment]:
        try:
            from funasr import AutoModel
        except ImportError as exc:
            raise RuntimeError("Install funasr or use --transcript first") from exc

        model = AutoModel(model=self.model, vad_model=self.vad_model, punc_model=self.punc_model)
        response = model.generate(input=str(audio_path), batch_size_s=300)
        if not response:
            return []

        first = response[0]
        sentence_info = first.get("sentence_info") or []
        if sentence_info:
            segments: list[TranscriptSegment] = []
            for item in sentence_info:
                segments.append(
                    TranscriptSegment(
                        start=float(item.get("start", 0)) / 1000,
                        end=float(item.get("end", 0)) / 1000,
                        text=str(item.get("text", "")).strip(),
                    )
                )
            return normalize_transcript_segments([segment for segment in segments if segment.text])

        text = str(first.get("text", "")).strip()
        segments = [TranscriptSegment(start=0.0, end=0.0, text=text)] if text else []
        return normalize_transcript_segments(segments)


def create_asr_engine(
    engine: str,
    transcript_path: Path | None = None,
    model: str | None = None,
    device: str = "cpu",
    compute_type: str = "int8",
    beam_size: int = 5,
) -> ASREngine:
    if transcript_path:
        return TranscriptFileEngine(transcript_path)
    if engine == "faster-whisper":
        return FasterWhisperEngine(
            model_size=model or "small",
            device=device,
            compute_type=compute_type,
            beam_size=beam_size,
        )
    if engine in {"funasr", "fun-asr"}:
        return FunASREngine(model=model or "paraformer-zh")
    raise ValueError("No transcript supplied. Choose --asr faster-whisper/funasr or pass --transcript")
