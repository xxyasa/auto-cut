from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Protocol

from .lexicon import normalize_transcript_segments, terms_to_hotwords, terms_to_prompt
from .models import TranscriptSegment
from .srt import parse_srt

logger = logging.getLogger(__name__)


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

    @staticmethod
    def _resolve_local_model(model_size: str) -> str:
        """若 model_size 是简单名字（如 'small'）且项目本地存在
        models/faster-whisper-<name>/，则返回该本地绝对路径，避免走 HuggingFace 下载。
        若 model_size 已是路径或本地不存在，则原样返回。
        """
        # 已经是路径（包含分隔符或存在的目录），不动
        if any(sep in model_size for sep in ("/", "\\")):
            return model_size
        if Path(model_size).exists():
            return model_size

        # 项目根目录 = 本文件向上 3 层： src/autocut/asr.py -> repo root
        repo_root = Path(__file__).resolve().parents[2]
        candidate = repo_root / "models" / f"faster-whisper-{model_size}"
        if candidate.is_dir() and (candidate / "model.bin").exists():
            return str(candidate)
        return model_size

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

        resolved_model = self._resolve_local_model(self.model_size)
        model = WhisperModel(resolved_model, device=self.device, compute_type=self.compute_type)
        # VAD 档位 A（温和细化）：默认 min_silence=2000ms 太粗，直播口播
        # 整段都被合在一起；调成 700ms 让换气停顿能切开，单段不超过 15 秒。
        vad_parameters = {
            "min_silence_duration_ms": 700,
            "min_speech_duration_ms": 250,
            "max_speech_duration_s": 15,
            "speech_pad_ms": 300,
        }
        segments, _ = model.transcribe(
            str(audio_path),
            language=language,
            beam_size=self.beam_size,
            word_timestamps=True,
            vad_filter=True,
            vad_parameters=vad_parameters,
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


class GlmAsrEngine:
    """两阶段 ASR：faster-whisper 提供时间轴 + 分段，glm-asr 逐段校正文本。

    环境变量（与 LLM 共用同一组）：
      AUTOCUT_LLM_API_URL  - 例如 https://model-api.ecmax.cn/v1/audio/transcriptions
                             （若包含 /chat/completions 则自动替换为 /audio/transcriptions）
      AUTOCUT_LLM_API_KEY  - Bearer token
    """

    GLM_ASR_MODEL = "glm-asr"
    # glm-asr 单次转写最大音频时长（秒）。超过时拆成多段合并
    _MAX_CHUNK_SEC = 60.0

    def __init__(
        self,
        fw_model_size: str = "small",
        fw_device: str = "cpu",
        fw_compute_type: str = "int8",
        fw_beam_size: int = 5,
    ):
        self.fw_engine = FasterWhisperEngine(
            model_size=fw_model_size,
            device=fw_device,
            compute_type=fw_compute_type,
            beam_size=fw_beam_size,
        )

    def _api_url(self) -> str:
        url = os.environ.get("AUTOCUT_LLM_API_URL", "")
        # 兼容：若配置的是 chat/completions，自动替换为 audio/transcriptions
        if "/chat/completions" in url:
            url = url.replace("/chat/completions", "/audio/transcriptions")
        if not url:
            raise RuntimeError(
                "AUTOCUT_LLM_API_URL not set. "
                "Set it to the base URL of the glm-asr API."
            )
        return url

    def _api_key(self) -> str:
        return os.environ.get("AUTOCUT_LLM_API_KEY", "")

    def _transcribe_audio_bytes(self, audio_bytes: bytes, suffix: str = ".wav") -> str:
        """把一段音频字节发给 glm-asr，返回识别文本。"""
        import urllib.request

        url = self._api_url()
        api_key = self._api_key()

        # 构造 multipart/form-data
        boundary = "----AutoCutBoundary"
        body_parts: list[bytes] = []

        # model 字段
        body_parts.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"model\"\r\n\r\n{self.GLM_ASR_MODEL}\r\n".encode()
        )
        # file 字段
        body_parts.append(
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="file"; filename="audio{suffix}"\r\n'
                f"Content-Type: audio/wav\r\n\r\n"
            ).encode()
        )
        body_parts.append(audio_bytes)
        body_parts.append(f"\r\n--{boundary}--\r\n".encode())

        body = b"".join(body_parts)
        headers = {
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Authorization": f"Bearer {api_key}",
        }

        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                # OpenAI 格式: {"text": "..."}
                return str(data.get("text", "")).strip()
        except Exception as exc:
            logger.warning("glm-asr API call failed: %s", exc)
            return ""

    def _extract_audio_segment(
        self, audio_path: Path, start: float, end: float, tmp_dir: str
    ) -> Path:
        """用 ffmpeg 截取一段音频，返回临时文件路径。"""
        from . import media

        out_path = Path(tmp_dir) / f"seg_{start:.3f}_{end:.3f}.wav"
        duration = end - start
        ffmpeg = media.executable("ffmpeg")
        if not ffmpeg:
            raise RuntimeError("ffmpeg not found")
        cmd = [
            ffmpeg,
            "-y",
            "-ss", str(start),
            "-t", str(duration),
            "-i", str(audio_path),
            "-ar", "16000",
            "-ac", "1",
            "-f", "wav",
            str(out_path),
        ]
        import subprocess
        subprocess.run(cmd, check=True, capture_output=True)
        return out_path

    def transcribe(
        self,
        audio_path: Path,
        language: str = "zh",
        asr_terms: list[str] | None = None,
    ) -> list[TranscriptSegment]:
        # 第一步：faster-whisper 拿时间轴
        logger.info("glm-asr: running faster-whisper for timestamps...")
        segments = self.fw_engine.transcribe(audio_path, language=language, asr_terms=asr_terms)
        if not segments:
            return segments

        # 第二步：逐段截音频，发给 glm-asr 校正文本
        logger.info("glm-asr: correcting text for %d segments via API...", len(segments))
        with tempfile.TemporaryDirectory() as tmp_dir:
            for seg in segments:
                if seg.end - seg.start < 0.5:
                    continue  # 太短的段跳过校正
                try:
                    seg_path = self._extract_audio_segment(audio_path, seg.start, seg.end, tmp_dir)
                    corrected = self._transcribe_audio_bytes(seg_path.read_bytes(), suffix=".wav")
                    if corrected:
                        seg.text = corrected
                        seg.clean_text = None  # 让后续 clean_segments 重新处理
                except Exception as exc:
                    logger.warning(
                        "glm-asr: failed to correct segment [%.1f-%.1f]: %s",
                        seg.start, seg.end, exc,
                    )
                    # 校正失败则保留 faster-whisper 原文本，不中断

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
    if engine in {"glm-asr", "glm"}:
        return GlmAsrEngine(
            fw_model_size=model or "small",
            fw_device=device,
            fw_compute_type=compute_type,
            fw_beam_size=beam_size,
        )
    raise ValueError("No transcript supplied. Choose --asr faster-whisper/funasr/glm-asr or pass --transcript")
