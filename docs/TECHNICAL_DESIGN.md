# 直播素材智能切片技术方案

## 技术路线

MVP 采用模块化流水线：

```text
视频导入 -> FFmpeg/FFprobe -> ASR -> 文本清洗 -> 语义分段 -> 候选评分 -> 人审 -> 导出
```

## 开源组件

- FFmpeg：转码、抽音频、裁剪、封面。
- FunASR/Fun-ASR：中文直播 ASR 候选。
- faster-whisper：高性能 Whisper 推理、词级时间戳、VAD filter。
- WhisperX：词级对齐和说话人分离，放在 P1。
- PySceneDetect：镜头检测和关键帧，放在 P0/P1。
- OpenTimelineIO：剪辑时间线交换，放在 P1。
- OpenMontage/VideoCaptioner：参考 Skills、字幕和 Agent 编排，不直接嵌入闭源代码。

## Skills

每个阶段都拆成可被 Agent 调用的 Skill：

- `live-ingest`
- `asr-transcribe`
- `filler-clean`
- `semantic-segment`
- `clip-ranker`
- `review-export`
- `qa-check`

## 部署

MVP 使用单机 CLI + 可选 FastAPI。生产化后拆成 API 服务、Worker、对象存储、PostgreSQL 和 GPU Worker。

