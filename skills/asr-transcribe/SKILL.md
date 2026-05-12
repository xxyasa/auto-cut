# asr-transcribe

## 目标

把直播音频转成带时间戳的转写文本。

## 推荐引擎

- 中文直播优先评测 FunASR/Fun-ASR。
- 需要词级时间戳时评测 faster-whisper。
- 需要说话人分离时评测 WhisperX 或 FunASR diarization 方案。

## 输出

- transcript JSON。
- SRT 字幕。
- ASR 引擎、模型、耗时和警告。

