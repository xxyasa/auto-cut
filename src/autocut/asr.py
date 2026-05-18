from __future__ import annotations

import json
import logging
import os
import ssl
import subprocess
import tempfile
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator
from typing import Protocol
from urllib.parse import urlsplit
from urllib.parse import urlunsplit

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


class WhisperApiEngine:
    """OpenAI-compatible Whisper transcription API.

    Expected endpoint shape matches:
      POST /v1/audio/transcriptions
      model=whisper-large-v3
      response_format=verbose_json
      timestamp_granularities=["word","segment"]

    Environment variables:
      AUTOCUT_ASR_API_URL  - preferred transcription endpoint
      AUTOCUT_ASR_API_KEY  - preferred bearer token
      AUTOCUT_LLM_API_URL  - fallback, chat/completions is rewritten to audio/transcriptions
      AUTOCUT_LLM_API_KEY  - fallback bearer token
      AUTOCUT_ASR_HTTP_FALLBACK - set to 1/true/yes to try http:// after HTTPS TLS failures
    """

    def __init__(self, model: str = "whisper-large-v3", timeout: int = 300):
        self.model = model
        self.timeout = timeout
        self._last_transport = ""
        self._last_upload_path = ""
        self._last_curl_args: list[str] = []

    def _api_url(self) -> str:
        url = os.environ.get("AUTOCUT_ASR_API_URL", "").strip()
        if not url:
            url = os.environ.get("AUTOCUT_LLM_API_URL", "").strip()
        if "/chat/completions" in url:
            url = url.replace("/chat/completions", "/audio/transcriptions")
        if not url:
            raise RuntimeError(
                "AUTOCUT_ASR_API_URL not set. Set it to the Whisper transcription API URL."
            )
        return url

    def _api_urls(self) -> list[tuple[str, str]]:
        url = self._api_url()
        scheme = urlsplit(url).scheme.lower() or "api"
        urls = [(scheme, url)]
        if self._http_fallback_enabled() and url.lower().startswith("https://"):
            parts = urlsplit(url)
            urls.append(
                (
                    "http fallback",
                    urlunsplit(("http", parts.netloc, parts.path, parts.query, parts.fragment)),
                )
            )
        return urls

    def _api_key(self) -> str:
        return os.environ.get("AUTOCUT_ASR_API_KEY", "").strip() or os.environ.get(
            "AUTOCUT_LLM_API_KEY", ""
        ).strip()

    def transcribe(
        self,
        audio_path: Path,
        language: str = "zh",
        asr_terms: list[str] | None = None,
    ) -> list[TranscriptSegment]:
        fields = {
            "model": self.model,
            "response_format": "verbose_json",
            "timestamp_granularities": '["word","segment"]',
        }
        if self._send_language_enabled():
            fields["language"] = language
        prompt = terms_to_prompt(asr_terms or [])
        if prompt and self._send_prompt_enabled():
            fields["prompt"] = prompt

        with self._upload_audio_path(audio_path) as upload_path:
            self._last_upload_path = str(upload_path)
            data = self._post_file(upload_path, fields)
        segments = normalize_transcript_segments(self._segments_from_verbose_json(data))
        if self._should_retry_chunked(data, segments):
            try:
                chunk_data = self._post_file_in_chunks(upload_path, fields, data, segments)
                chunk_segments = normalize_transcript_segments(
                    self._segments_from_verbose_json(chunk_data)
                )
                if self._segments_duration(chunk_segments) > self._segments_duration(segments):
                    data = chunk_data
                    segments = chunk_segments
            except RuntimeError as exc:
                data["chunked_retry_failed"] = str(exc)
                logger.warning("ASR chunk retry failed, using initial response: %s", exc)
        self._write_debug_response(audio_path, data, segments)
        return segments

    def _write_debug_response(
        self,
        audio_path: Path,
        data: dict,
        segments: list[TranscriptSegment],
    ) -> None:
        metadata_dir = audio_path.parent.parent / "metadata"
        if audio_path.parent.name != "audio" or not metadata_dir.parent.exists():
            return
        try:
            metadata_dir.mkdir(parents=True, exist_ok=True)
            debug = {
                "model": self.model,
                "raw_keys": list(data.keys()),
                "raw_segments_count": len(data.get("segments") or []),
                "raw_words_count": len(data.get("words") or []),
                "parsed_segments_count": len(segments),
                "parsed_duration": round(sum(segment.duration for segment in segments), 3),
                "transport": self._last_transport,
                "upload_path": self._last_upload_path,
                "curl_args": self._redacted_curl_args(self._last_curl_args),
                "request_fields": {
                    "model": self.model,
                    "language_sent": self._send_language_enabled(),
                    "prompt_sent": self._send_prompt_enabled(),
                    "response_format": "verbose_json",
                    "timestamp_granularities": '["word","segment"]',
                },
                "response": data,
            }
            (metadata_dir / "asr_response.json").write_text(
                json.dumps(debug, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as exc:
            logger.warning("failed to write ASR debug response: %s", exc)

    def _should_retry_chunked(self, data: dict, segments: list[TranscriptSegment]) -> bool:
        if os.environ.get("AUTOCUT_ASR_CHUNK_RETRY", "1").strip().lower() in {"0", "false", "no"}:
            return False
        total_duration = self._response_duration(data)
        if total_duration < 60:
            return False
        parsed_duration = self._segments_duration(segments)
        expected_min_segments = max(6, int(total_duration // 20))
        return len(segments) < expected_min_segments or parsed_duration < total_duration * 0.35

    def _post_file_in_chunks(
        self,
        audio_path: Path,
        fields: dict[str, str],
        initial_data: dict,
        initial_segments: list[TranscriptSegment],
    ) -> dict:
        chunk_seconds = max(
            10.0,
            float(os.environ.get("AUTOCUT_ASR_CHUNK_SECONDS", "30")),
        )
        total_duration = self._response_duration(initial_data)
        chunks_dir = audio_path.parent / f"{audio_path.stem}_asr_chunks"
        chunks_dir.mkdir(parents=True, exist_ok=True)

        chunk_segments: list[TranscriptSegment] = []
        chunk_summaries: list[dict[str, object]] = []
        start = 0.0
        index = 1
        while start < total_duration - 0.01:
            duration = min(chunk_seconds, total_duration - start)
            chunk_path = chunks_dir / f"chunk_{index:03d}{audio_path.suffix}"
            try:
                self._extract_audio_chunk(audio_path, chunk_path, start, duration)
                data = self._post_file(chunk_path, fields)
                parsed = self._segments_from_verbose_json(data)
            except RuntimeError as exc:
                chunk_summaries.append(
                    {
                        "index": index,
                        "start": round(start, 3),
                        "duration": round(duration, 3),
                        "segments": 0,
                        "error": str(exc)[:500],
                    }
                )
                start += chunk_seconds
                index += 1
                continue
            for segment in parsed:
                segment.start = round(segment.start + start, 3)
                segment.end = round(segment.end + start, 3)
                for word in segment.words:
                    if "start" in word:
                        word["start"] = round(float(word["start"]) + start, 3)
                    if "end" in word:
                        word["end"] = round(float(word["end"]) + start, 3)
            chunk_segments.extend(parsed)
            chunk_summaries.append(
                {
                    "index": index,
                    "start": round(start, 3),
                    "duration": round(duration, 3),
                    "segments": len(parsed),
                    "transport": self._last_transport,
                }
            )
            start += chunk_seconds
            index += 1

        return {
            "duration": total_duration,
            "language": initial_data.get("language"),
            "text": "".join(segment.text for segment in chunk_segments),
            "segments": [self._segment_to_response_item(segment, idx) for idx, segment in enumerate(chunk_segments)],
            "words": [word for segment in chunk_segments for word in segment.words],
            "chunked_retry": True,
            "initial_segments_count": len(initial_segments),
            "initial_segments_duration": self._segments_duration(initial_segments),
            "chunks": chunk_summaries,
        }

    def _extract_audio_chunk(
        self,
        audio_path: Path,
        chunk_path: Path,
        start: float,
        duration: float,
    ) -> None:
        from . import media

        ffmpeg = media.executable("ffmpeg")
        if not ffmpeg:
            raise RuntimeError("ffmpeg not found for ASR chunk retry")
        cmd = [
            ffmpeg,
            "-y",
            "-ss",
            f"{start:.3f}",
            "-t",
            f"{duration:.3f}",
            "-i",
            str(audio_path),
            "-ar",
            "16000",
            "-ac",
            "1",
            str(chunk_path),
        ]
        completed = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if completed.returncode != 0:
            raise RuntimeError(
                f"failed to extract ASR chunk: exit {completed.returncode} {completed.stderr[-500:]}"
            )

    @staticmethod
    def _response_duration(data: dict) -> float:
        try:
            return float(data.get("duration") or 0.0)
        except (TypeError, ValueError):
            segments = data.get("segments") or []
            return max((float(item.get("end") or 0.0) for item in segments), default=0.0)

    @staticmethod
    def _segments_duration(segments: list[TranscriptSegment]) -> float:
        return round(sum(segment.duration for segment in segments), 3)

    @staticmethod
    def _segment_to_response_item(segment: TranscriptSegment, index: int) -> dict[str, object]:
        return {
            "id": index,
            "start": round(segment.start, 3),
            "end": round(segment.end, 3),
            "text": segment.text,
        }

    @contextmanager
    def _upload_audio_path(self, audio_path: Path) -> Iterator[Path]:
        upload_format = os.environ.get("AUTOCUT_ASR_UPLOAD_FORMAT", "wav").strip().lower()
        if upload_format in {"", "source", "original"}:
            yield audio_path
            return

        if upload_format not in {"mp3", "wav"}:
            raise RuntimeError(f"Unsupported AUTOCUT_ASR_UPLOAD_FORMAT: {upload_format}")
        if audio_path.suffix.lower() == f".{upload_format}":
            yield audio_path
            return

        from . import media

        ffmpeg = media.executable("ffmpeg")
        if not ffmpeg:
            logger.warning("ffmpeg not found, uploading original ASR audio: %s", audio_path)
            yield audio_path
            return

        converted = audio_path.parent / f"{audio_path.stem}_asr_upload.{upload_format}"
        if converted.exists() and converted.stat().st_mtime >= audio_path.stat().st_mtime:
            yield converted
            return

        cmd = [
            ffmpeg,
            "-y",
            "-i",
            str(audio_path),
            "-ar",
            "16000",
            "-ac",
            "1",
        ]
        if upload_format == "mp3":
            cmd.extend(["-codec:a", "libmp3lame", "-b:a", "64k"])
        cmd.append(str(converted))
        try:
            completed = subprocess.run(
                cmd,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except Exception as exc:
            logger.warning("failed to convert ASR upload audio, using original: %s", exc)
            yield audio_path
            return
        if completed.returncode != 0:
            logger.warning(
                "failed to convert ASR upload audio, using original: exit %s %s",
                completed.returncode,
                completed.stderr[-500:],
            )
            yield audio_path
            return
        yield converted

    def _post_file(self, audio_path: Path, fields: dict[str, str]) -> dict:
        all_errors: list[tuple[str, str]] = []
        for url_label, api_url in self._api_urls():
            try:
                return self._post_file_to_url(audio_path, fields, api_url, url_label)
            except RuntimeError as exc:
                message = str(exc)
                if not self._looks_like_tls_transport_error(message):
                    all_errors.append((url_label, message))
                    raise RuntimeError(self._format_transport_errors(all_errors)) from exc
                all_errors.append((url_label, message))
        if len(all_errors) == 1 and self._api_url().lower().startswith("https://"):
            all_errors.append(
                (
                    "hint",
                    "HTTPS TLS failed before HTTP. Fix the ASR gateway TLS, set "
                    "AUTOCUT_ASR_API_URL to http://... for trusted intranet use, or set "
            "AUTOCUT_ASR_HTTP_FALLBACK=1 in .env and restart the server.",
                )
            )
        raise RuntimeError(self._format_transport_errors(all_errors))

    def _post_file_to_url(
        self,
        audio_path: Path,
        fields: dict[str, str],
        api_url: str,
        url_label: str,
    ) -> dict:
        transport_errors: list[tuple[str, str]] = []
        httpx_available = True
        try:
            data = self._post_file_curl(audio_path, fields, api_url=api_url)
            self._last_transport = f"{url_label} curl"
            return data
        except RuntimeError as exc:
            message = str(exc)
            if not self._looks_like_tls_transport_error(message):
                raise RuntimeError(
                    self._format_transport_errors([(f"{url_label} curl", message)])
                ) from exc
            transport_errors.append(("curl", message))

        try:
            data = self._post_file_httpx(audio_path, fields, api_url=api_url)
            self._last_transport = f"{url_label} httpx"
            return data
        except ImportError as exc:
            httpx_available = False
            transport_errors.append(("httpx", f"unavailable: {exc}"))
        except RuntimeError as exc:
            message = str(exc)
            if not self._looks_like_ssl_eof(message) and not self._looks_like_tls_transport_error(
                message
            ):
                raise RuntimeError(
                    self._format_transport_errors([(f"{url_label} httpx", message)])
                ) from exc
            transport_errors.append(("httpx", message))

        if httpx_available and api_url.lower().startswith("https://"):
            try:
                data = self._post_file_httpx(audio_path, fields, api_url=api_url, tls12_max=True)
                self._last_transport = f"{url_label} httpx tls1.2"
                return data
            except RuntimeError as exc:
                message = str(exc)
                if not self._looks_like_tls_transport_error(message):
                    raise RuntimeError(
                        self._format_transport_errors([(f"{url_label} httpx tls1.2", message)])
                    ) from exc
                transport_errors.append(("httpx tls1.2", message))

        try:
            data = self._post_file_requests_with_retries(audio_path, fields, api_url=api_url)
            self._last_transport = f"{url_label} requests"
            return data
        except ImportError as exc:
            transport_errors.append(("requests", f"unavailable: {exc}"))
        except RuntimeError as exc:
            message = str(exc)
            if not self._looks_like_tls_transport_error(message):
                raise RuntimeError(
                    self._format_transport_errors([(f"{url_label} requests", message)])
                ) from exc
            transport_errors.append(("requests", message))

        try:
            data = self._post_file_urllib_with_tls_retries(audio_path, fields, api_url=api_url)
            self._last_transport = f"{url_label} urllib"
            return data
        except RuntimeError as exc:
            message = str(exc)
            if not self._looks_like_tls_transport_error(message):
                raise RuntimeError(
                    self._format_transport_errors([(f"{url_label} urllib", message)])
                ) from exc
            transport_errors.append(("urllib", message))

        raise RuntimeError(self._format_transport_errors(transport_errors))

    @staticmethod
    def _format_transport_errors(errors: list[tuple[str, str]]) -> str:
        details = " | ".join(
            f"{name}: {message[:240].replace(chr(10), ' ')}" for name, message in errors
        )
        return f"Whisper API transcription failed after transport retries: {details}"

    def _post_file_requests_with_retries(
        self,
        audio_path: Path,
        fields: dict[str, str],
        api_url: str,
    ) -> dict:
        verify_enabled = not self._ssl_verify_disabled()
        attempts = [verify_enabled]
        if verify_enabled and api_url.lower().startswith("https://"):
            attempts.append(False)
        errors: list[tuple[str, str]] = []
        for verify in attempts:
            try:
                return self._post_file_requests(audio_path, fields, api_url=api_url, verify=verify)
            except RuntimeError as exc:
                message = str(exc)
                errors.append((f"verify={verify}", message))
                if not self._looks_like_tls_transport_error(message):
                    raise
        raise RuntimeError(self._format_transport_errors(errors))

    def _post_file_requests(
        self,
        audio_path: Path,
        fields: dict[str, str],
        api_url: str,
        verify: bool = True,
    ) -> dict:
        import requests

        headers = {}
        api_key = self._api_key()
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        with audio_path.open("rb") as file_obj:
            files = {"file": (audio_path.name, file_obj, self._content_type(audio_path))}
            try:
                response = requests.post(
                    api_url,
                    headers=headers,
                    data=fields,
                    files=files,
                    timeout=self.timeout,
                    verify=verify,
                )
            except requests.RequestException as exc:
                raise RuntimeError(f"Whisper API transcription failed via requests: {exc}") from exc
        if response.status_code >= 400:
            raise RuntimeError(
                "Whisper API transcription failed via requests: "
                f"HTTP {response.status_code} {response.text[:500]}"
            )
        try:
            return response.json()
        except ValueError as exc:
            raise RuntimeError(
                "Whisper API transcription returned non-JSON via requests: "
                f"{response.text[:500]}"
            ) from exc

    def _post_file_httpx(
        self,
        audio_path: Path,
        fields: dict[str, str],
        api_url: str,
        tls12_max: bool = False,
    ) -> dict:
        import httpx

        headers = {}
        api_key = self._api_key()
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        with audio_path.open("rb") as file_obj:
            files = {"file": (audio_path.name, file_obj, self._content_type(audio_path))}
            try:
                with httpx.Client(
                    timeout=self.timeout,
                    verify=self._ssl_verify_config(tls12_max=tls12_max),
                    http2=False,
                ) as client:
                    response = client.post(
                        api_url,
                        headers=headers,
                        data=fields,
                        files=files,
                    )
            except httpx.HTTPError as exc:
                raise RuntimeError(f"Whisper API transcription failed: {exc}") from exc
        if response.status_code >= 400:
            raise RuntimeError(
                f"Whisper API transcription failed: HTTP {response.status_code} {response.text[:500]}"
            )
        return response.json()

    @staticmethod
    def _content_type(audio_path: Path) -> str:
        suffix = audio_path.suffix.lower()
        if suffix == ".mp3":
            return "audio/mpeg"
        if suffix == ".m4a":
            return "audio/mp4"
        if suffix == ".wav":
            return "audio/wav"
        return "application/octet-stream"

    @staticmethod
    def _ssl_verify_config(tls12_max: bool = False):
        if WhisperApiEngine._ssl_verify_disabled():
            return False

        return WhisperApiEngine._ssl_context(verify=True, tls12_max=tls12_max)

    @staticmethod
    def _ssl_context(verify: bool = True, tls12_max: bool = False) -> ssl.SSLContext:
        context = ssl.create_default_context() if verify else ssl._create_unverified_context()
        ignore_eof = getattr(ssl, "OP_IGNORE_UNEXPECTED_EOF", 0)
        if ignore_eof:
            context.options |= ignore_eof
        if tls12_max and hasattr(ssl, "TLSVersion"):
            context.maximum_version = ssl.TLSVersion.TLSv1_2
        return context

    def _post_file_urllib_with_tls_retries(
        self,
        audio_path: Path,
        fields: dict[str, str],
        api_url: str,
    ) -> dict:
        verify_enabled = not self._ssl_verify_disabled()
        attempts = [(verify_enabled, False)]
        if api_url.lower().startswith("https://"):
            attempts.append((verify_enabled, True))
        if verify_enabled and api_url.lower().startswith("https://"):
            attempts.append((False, True))
        last_error: RuntimeError | None = None
        for verify, tls12_max in attempts:
            try:
                return self._post_file_urllib(
                    audio_path,
                    fields,
                    api_url=api_url,
                    verify=verify,
                    tls12_max=tls12_max,
                )
            except RuntimeError as exc:
                last_error = exc
                if not self._looks_like_tls_transport_error(str(exc)):
                    raise
        raise last_error or RuntimeError("Whisper API transcription failed")

    def _post_file_urllib(
        self,
        audio_path: Path,
        fields: dict[str, str],
        api_url: str | None = None,
        verify: bool | None = None,
        tls12_max: bool = False,
    ) -> dict:
        api_url = api_url or self._api_url()
        if verify is None:
            verify = not self._ssl_verify_disabled()
        boundary = "----AutoCutBoundary"
        body_parts: list[bytes] = []
        for name, value in fields.items():
            body_parts.append(
                (
                    f"--{boundary}\r\n"
                    f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
                    f"{value}\r\n"
                ).encode("utf-8")
            )
        body_parts.append(
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="file"; filename="{audio_path.name}"\r\n'
                f"Content-Type: {self._content_type(audio_path)}\r\n\r\n"
            ).encode("utf-8")
        )
        body_parts.append(audio_path.read_bytes())
        body_parts.append(f"\r\n--{boundary}--\r\n".encode("utf-8"))

        headers = {"Content-Type": f"multipart/form-data; boundary={boundary}"}
        api_key = self._api_key()
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        req = urllib.request.Request(
            api_url,
            data=b"".join(body_parts),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(
                req,
                timeout=self.timeout,
                context=self._ssl_context(verify=verify, tls12_max=tls12_max),
            ) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            raise RuntimeError(f"Whisper API transcription failed: {exc}") from exc

    def _post_file_curl(
        self,
        audio_path: Path,
        fields: dict[str, str],
        api_url: str | None = None,
    ) -> dict:
        from . import media

        api_url = api_url or self._api_url()
        curl = media.executable("curl.exe") or media.executable("curl")
        if not curl:
            return self._post_file_urllib_with_tls_retries(audio_path, fields, api_url=api_url)

        base_args = [
            curl,
            "--location",
            "--request",
            "POST",
            api_url,
            "--silent",
            "--show-error",
            "--fail-with-body",
            "--http1.1",
        ]
        if api_url.lower().startswith("https://"):
            base_args.append("--ssl-no-revoke")
            attempts = [base_args]
            attempts.append([*base_args, "--tlsv1.2", "--tls-max", "1.2"])
            if self._ssl_verify_disabled():
                attempts = [[*args, "--insecure"] for args in attempts]
            else:
                attempts.append([*base_args, "--tlsv1.2", "--tls-max", "1.2", "--insecure"])
        else:
            attempts = [base_args]

        shared_args = []
        api_key = self._api_key()
        if api_key:
            shared_args.extend(["--header", f"Authorization: Bearer {api_key}"])
        for name, value in fields.items():
            shared_args.extend(["--form", f"{name}={self._curl_form_value(name, value)}"])
        shared_args.extend(["--form", f"file=@{audio_path}"])

        last_error = ""
        for args in attempts:
            full_args = [*args, *shared_args]
            self._last_curl_args = full_args
            completed = self._run_curl(full_args)
            if completed.returncode == 0:
                try:
                    return json.loads(completed.stdout)
                except json.JSONDecodeError as exc:
                    raise RuntimeError(
                        "Whisper API transcription returned non-JSON via curl: "
                        f"{completed.stdout[:500]}"
                    ) from exc
            detail = (completed.stderr or completed.stdout).strip()
            last_error = (
                f"Whisper API transcription failed via curl: exit {completed.returncode} "
                f"{detail[:500]}"
            )
            if not self._looks_like_tls_transport_error(last_error):
                break
        raise RuntimeError(last_error)

    def _run_curl(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                args,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout,
            )
        except Exception as exc:
            raise RuntimeError(f"Whisper API transcription failed via curl: {exc}") from exc

    @staticmethod
    def _curl_form_value(name: str, value: str) -> str:
        # Match the manually verified curl command shape for this gateway.
        if name in {"model", "response_format"} and not value.startswith('"'):
            return f'"{value}"'
        if name == "timestamp_granularities" and not value.startswith('"'):
            return '"' + value.replace('"', '\\"') + '"'
        return value

    @staticmethod
    def _redacted_curl_args(args: list[str]) -> list[str]:
        redacted: list[str] = []
        redact_next = False
        for value in args:
            if redact_next:
                redacted.append("Authorization: Bearer <redacted>")
                redact_next = False
                continue
            redacted.append(value)
            if value == "--header":
                redact_next = True
        return redacted

    @staticmethod
    def _looks_like_ssl_eof(message: str) -> bool:
        lowered = message.lower()
        return "ssl" in lowered and "eof" in lowered

    @staticmethod
    def _looks_like_tls_transport_error(message: str) -> bool:
        lowered = message.lower()
        return any(
            marker in lowered
            for marker in (
                "schannel",
                "handshake",
                "ssl/tls",
                "tls/ssl",
                "tls connection",
                "tls/ssl connection",
                "connection has been closed",
                "closed (eof)",
                "unexpected eof",
                "unexpected_eof",
                "eof occurred in violation of protocol",
            )
        )

    @staticmethod
    def _ssl_verify_disabled() -> bool:
        return os.environ.get("AUTOCUT_ASR_SSL_VERIFY", "").strip().lower() in {
            "0",
            "false",
            "no",
        }

    @staticmethod
    def _http_fallback_enabled() -> bool:
        return os.environ.get("AUTOCUT_ASR_HTTP_FALLBACK", "").strip().lower() in {
            "1",
            "true",
            "yes",
        }

    @staticmethod
    def _send_language_enabled() -> bool:
        return os.environ.get("AUTOCUT_ASR_SEND_LANGUAGE", "").strip().lower() in {
            "1",
            "true",
            "yes",
        }

    @staticmethod
    def _send_prompt_enabled() -> bool:
        return os.environ.get("AUTOCUT_ASR_SEND_PROMPT", "").strip().lower() in {
            "1",
            "true",
            "yes",
        }

    @staticmethod
    def _segments_from_verbose_json(data: dict) -> list[TranscriptSegment]:
        raw_segments = data.get("segments") or []
        raw_words = data.get("words") or []
        result: list[TranscriptSegment] = []

        if raw_segments:
            for item in raw_segments:
                start = float(item.get("start", 0.0))
                end = float(item.get("end", start))
                words = [
                    {
                        "start": float(word.get("start", 0.0)),
                        "end": float(word.get("end", 0.0)),
                        "word": str(word.get("word", "")),
                    }
                    for word in raw_words
                    if start <= float(word.get("start", 0.0)) < end
                ]
                result.append(
                    TranscriptSegment(
                        start=start,
                        end=end,
                        text=str(item.get("text", "")).strip(),
                        words=words,
                    )
                )
            return [segment for segment in result if segment.text]

        if raw_words:
            text = "".join(str(word.get("word", "")) for word in raw_words).strip()
            return [
                TranscriptSegment(
                    start=float(raw_words[0].get("start", 0.0)),
                    end=float(raw_words[-1].get("end", 0.0)),
                    text=text,
                    words=[
                        {
                            "start": float(word.get("start", 0.0)),
                            "end": float(word.get("end", 0.0)),
                            "word": str(word.get("word", "")),
                        }
                        for word in raw_words
                    ],
                )
            ]

        text = str(data.get("text", "")).strip()
        return [TranscriptSegment(start=0.0, end=0.0, text=text)] if text else []


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
    if engine in {"whisper-api", "remote-whisper", "faster-whisper-api"}:
        api_model = "whisper-large-v3" if model in {None, "", "small"} else model
        return WhisperApiEngine(model=api_model)
    if engine in {"glm-asr", "glm"}:
        return GlmAsrEngine(
            fw_model_size=model or "small",
            fw_device=device,
            fw_compute_type=compute_type,
            fw_beam_size=beam_size,
        )
    raise ValueError(
        "No transcript supplied. Choose --asr faster-whisper/funasr/glm-asr/whisper-api "
        "or pass --transcript"
    )
