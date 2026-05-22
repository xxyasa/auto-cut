# Auto Cut

直播素材智能切片 MVP。

目标是把完整直播录屏加工成可人工复核的视频轨道：一个视频进去，一条可导出的成片出来。播放器和轨道都基于完整原视频；水词、咳嗽、低价值、不完整对话、异常场景和长停顿默认置灰禁用，业务可以重新启用或禁用任意轨道块。第一阶段定位为“AI 初剪轨道 + 人工复核”，不是全自动成片。

## 功能

- 媒体标准化接口：基于 FFmpeg/FFprobe。
- ASR 抽象：支持 transcript 文件、faster-whisper、FunASR/Fun-ASR 扩展。
- 水词和无效话术清洗。
- 语义分段和完整视频轨道生成。
- 多维评分：内容完整度、卖点匹配、语言质量、媒体质量、转化力、多样性。
- 导出：转写 JSON、SRT、轨道剪辑决策，以及按启用轨道块拼接出来的成片 MP4。
- 轨道审核台：按完整原视频展示视频轨，灰色块默认跳过，用户可重新启用/禁用，并查看每段梗概和字幕。
- 千川重排：把字幕句子池交给 LLM 生成 `ordered_ids`，再按原句时间戳非顺序拼接成 25 秒左右视频。
- Skills 文档：可作为 Agent 编排规范。

## 快速开始

```powershell
cd F:\workCode\auto-cut
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .
```

如果暂时没有 ASR 环境，可以先用转写 JSON 跑通流程：

```powershell
autocut run .\examples\sample.mp4 `
  --product "示例产品" `
  --selling-point "舒适" `
  --selling-point "适合日常" `
  --transcript .\tests\fixtures\transcript.json `
  --output .\data\runs\sample
```

输出目录会包含：

```text
metadata/
subtitles/
clips/
frames/
```

没有安装系统 FFmpeg 时，项目会优先使用 `imageio-ffmpeg` 内置的 FFmpeg。若两者都不可用，系统仍会输出轨道元数据和 SRT，并给出媒体导出警告。

## 可选依赖

```powershell
pip install -e ".[api]"
pip install -e ".[asr]"
pip install -e ".[funasr]"
pip install imageio-ffmpeg
```

## 国内镜像下载模型

推荐使用 `hf-mirror.com` 下载 faster-whisper 模型，并放在项目本地 `models/` 目录：

```powershell
.\scripts\download_faster_whisper.ps1 -Model small
```

下载完成后用本地模型路径运行，避免每次走 Hugging Face 缓存：

```powershell
autocut run .\your-live.mp4 `
  --product "产品名" `
  --selling-point "卖点" `
  --brand-term "Pinkypinky" `
  --brand-term "哈利波特" `
  --asr faster-whisper `
  --asr-model ".\models\faster-whisper-small" `
  --asr-device cpu `
  --asr-compute-type int8 `
  --output .\data\runs\demo
```

项目内置了一份基础品牌/IP/材质词表：[brand_terms.txt](src/autocut/resources/brand_terms.txt)。运行时会把内置词表、产品名、卖点和 `--brand-term` 一起传给 ASR 的 `initial_prompt/hotwords`，并在转写后做轻量统一化。

ASR 引擎建议先做样本评测：

- 中文直播优先评测 FunASR/Fun-ASR。
- 要词级时间戳和说话人分离时评测 faster-whisper + WhisperX。
- 当前 Python 3.13 环境下，FunASR 可能会被 `editdistance` 的本地编译依赖卡住；建议优先使用 `faster-whisper` 跑通自动转写。

## 测试

```powershell
python -m unittest discover
```

## 启动业务审核台

```powershell
cd F:\workCode\auto-cut
.\scripts\start_review_server.ps1
```

打开：

```text
http://127.0.0.1:8765
```

审核台可以查看处理批次、完整视频轨、每个轨道块的梗概和字幕。点击轨道块或右侧轨道卡片可以细调启用状态；“启用当前段/禁用当前段”会写入 `metadata/timeline_overrides.json`，播放启用片段时会自动跳过灰色块。启用片段为淡绿色，禁用片段为红色；点击“导出视频”会按当前启用状态生成一条成片。

## 千川脚本重排

模型接口通过环境变量配置，不要把密钥写进代码：

```powershell
$env:AUTOCUT_LLM_API_URL="https://model-api.ecmax.cn/v1/chat/completions"
$env:AUTOCUT_LLM_MODEL="deepseek-v3.2"
$env:AUTOCUT_LLM_API_KEY="<your-api-key>"
```

生成字幕池、prompt 和本地兜底重排视频：

```powershell
autocut remix .\data\runs\demo --target-duration 25
```

调用模型生成顺序并导出：

```powershell
autocut remix .\data\runs\demo --target-duration 25 --llm
```

如果需要流式调用模型并聚合结果：

```powershell
autocut remix .\data\runs\demo --target-duration 25 --llm --llm-stream
```

API 也支持同样链路：

```http
POST /api/runs/{run_id}/remix/export
{
  "use_llm": true,
  "stream": false,
  "target_duration": 25
}
```

## Docker 部署（推荐生产环境）

### 前置条件

- Docker 20.10+
- docker compose v2（`docker compose` 命令）

### 快速启动

```bash
# 1. 克隆项目
git clone <repo-url>
cd auto-cut

# 2. 创建环境变量文件（不要提交到 git）
cp .env.example .env
# 编辑 .env，至少设置：
#   AUTOCUT_API_TOKEN=<你的 token>
#   AUTOCUT_LLM_API_KEY=<你的 LLM key>（可选）

# 3. 创建本地数据目录
mkdir -p data/runs data/uploads models

# 4. 构建并启动
docker compose up -d --build

# 访问：http://your-server-ip:8765/business.html
```

### 环境变量说明

| 变量 | 必填 | 说明 |
|------|------|------|
| `AUTOCUT_API_TOKEN` | ✅ | 前端鉴权 token，自行设置 |
| `AUTOCUT_LLM_API_URL` | 否 | LLM 接口地址 |
| `AUTOCUT_LLM_MODEL` | 否 | 模型名，如 `deepseek-v3.2` |
| `AUTOCUT_LLM_API_KEY` | 否 | LLM API Key |
| `AUTOCUT_LLM_HTTP_FALLBACK` | 否 | HTTPS 握手失败后尝试同路径 HTTP，可信内网才开启 |
| `AUTOCUT_ASR_API_URL` | 否 | 远端 Whisper/OpenAI 兼容转写接口 |
| `AUTOCUT_ASR_API_KEY` | 否 | 远端 ASR API Key；不填时回退用 `AUTOCUT_LLM_API_KEY` |
| `AUTOCUT_ASR_UPLOAD_FORMAT` | 否 | 远端 ASR 上传格式，默认 `wav`；可设 `mp3` / `source` |
| `AUTOCUT_EXPORT_AUDIO_GAIN_DB` | 否 | 导出 MP4 音量增益，默认 `20` dB；设 `0` 可关闭 |

### 本地模型挂载

把模型文件放在宿主机 `./models/` 目录下，容器会自动以只读方式挂载到 `/models`。

```bash
# 例：把 faster-whisper-small 放进去
ls models/
# faster-whisper-small/
```

在任务创建时 ASR 模型路径填 `/models/faster-whisper-small` 即可。

### 日志与重启

```bash
# 查看日志
docker compose logs -f

# 重启服务
docker compose restart

# 停止
docker compose down
```

### Linux 裸机启动（不用 Docker）

```bash
chmod +x scripts/start.sh
./scripts/start.sh                        # 默认 127.0.0.1:8765
./scripts/start.sh --host 0.0.0.0 --port 8770   # 对外暴露
```

`stream=true` 时后端会按流式读取模型响应，但最终仍聚合出 `ordered_ids` 后再导出视频。模型只允许返回字幕 ID 顺序，不能改写每句字幕文本。

## 远端 Whisper ASR

如果有 OpenAI 兼容的 `/v1/audio/transcriptions` 服务，可以用远端 `whisper-large-v3`
直接返回 segment/word 时间戳，替代本地 `models/faster-whisper-small`：

```powershell
$env:AUTOCUT_ASR_API_URL="https://model-api.ecmax.cn/v1/audio/transcriptions"
$env:AUTOCUT_ASR_API_KEY="<your-api-key>"

autocut run .\your-live.mp4 `
  --product "产品名" `
  --selling-point "卖点" `
  --asr whisper-api `
  --asr-model whisper-large-v3 `
  --output .\data\runs\demo-large
```

业务页面的“高级 ASR 设置”里也可以选择 `whisper-large-v3 API`。该模式会请求
`response_format=verbose_json` 和 `timestamp_granularities=["word","segment"]`。

如果日志显示所有客户端都在 HTTPS 握手阶段失败，例如 `UNEXPECTED_EOF_WHILE_READING`
或 `TLS/SSL connection has been closed (EOF)`，通常是远端 ASR 网关的 HTTPS 配置问题。
优先修复网关证书/TLS；若确认该接口只在可信内网提供 HTTP，可将
`AUTOCUT_ASR_API_URL` 改为 `http://.../v1/audio/transcriptions`，或显式设置
`AUTOCUT_ASR_HTTP_FALLBACK=1` 让客户端在 HTTPS 失败后尝试同路径 HTTP。

LLM remix 使用 `AUTOCUT_LLM_API_URL`。如果出现同类 TLS EOF，优先修复 LLM 网关
HTTPS；可信内网场景可改为 `http://.../v1/chat/completions`，或设置
`AUTOCUT_LLM_HTTP_FALLBACK=1`。
