# Mac 部署指南（Apple Silicon）

本文档针对 **Apple Silicon Mac**（M1/M2/M3/M4 系列，含 MacBook Air / Pro / Mac mini / Mac Studio）。
Intel Mac 也能用，但需要把 `platform` 改为 `linux/amd64`。

---

## 一、环境前提

### 必备

- **Docker Desktop for Mac**（Apple Silicon 版）
  下载地址：<https://docs.docker.com/desktop/install/mac-install/>
- 宿主机磁盘空间 ≥ 15GB（镜像 + 模型 + 任务产物）
- 内存 ≥ 8GB（推荐 16GB 起，M4 MBA 起步即 16GB）

### Docker Desktop 配置（首次必须调）

打开 Docker Desktop → 设置：

| 配置项 | 建议值 |
|---|---|
| Resources → Memory | **8 GB**（16GB Mac）/ 10 GB（24GB+ Mac） |
| Resources → CPUs | 留默认（一般是 CPU 核数 / 2）|
| General → Use Virtualization framework | ✅ 勾选 |
| General → Use Rosetta for x86_64/amd64 emulation | ✅ 勾选（保险）|

> ⚠️ 默认 Docker Desktop 只分 2GB 给虚拟机，跑 ASR 会 OOM。**这一步必须做**。

---

## 二、文件准备

从源机器（Windows / Linux）迁移到 Mac，需要以下文件：

```
auto-cut/
├── Dockerfile
├── docker-compose.yml
├── pyproject.toml
├── .dockerignore
├── .env.example
├── README.md
├── docs/
├── src/                  ← 项目代码（必须）
└── models/               ← 模型文件（必须，约 4GB）
    └── faster-whisper-small/
        ├── model.bin
        ├── config.json
        ├── tokenizer.json
        └── vocabulary.txt
```

### 不需要传的内容（避免传一堆没用的）

- `.venv/`
- `__pycache__/`、`*.pyc`
- `data/runs/`、`data/uploads/`（旧任务产物，Mac 上重新生成即可）
- `*.log`、`tmp_*`
- `.dev-token`、`.env`（敏感信息，Mac 上重新创建）

### 推荐迁移方式

| 内容 | 方式 |
|---|---|
| 代码（src + 配置文件） | Git push/pull 或 zip |
| `models/` 目录（4GB） | AirDrop / rsync / scp / 移动硬盘 |

```bash
# 示例：从 Windows/Linux scp 到 Mac
scp -r models/ user@mac-ip:/Users/user/auto-cut/
```

---

## 三、启动步骤

### Step 1：克隆/解压项目

```bash
cd ~/  # 或任意位置
# 方式 A：git
git clone <your-repo-url> auto-cut
cd auto-cut

# 方式 B：解压 zip
unzip auto-cut.zip
cd auto-cut
```

### Step 2：放好模型文件

```bash
mkdir -p models
# 把 faster-whisper-small/ 整个目录放进 models/
ls models/
# 应该看到：faster-whisper-small/
```

### Step 3：创建 `.env`

```bash
cp .env.example .env
```

编辑 `.env`，**至少填**：

```bash
# API 鉴权 Token（业务前端会用这个 token，自行起一个）
AUTOCUT_API_TOKEN=mysecrettoken123

# LLM 接口（用于评分和 remix 重排）
AUTOCUT_LLM_API_URL=https://model-api.ecmax.cn/v1/chat/completions
AUTOCUT_LLM_MODEL=deepseek-v3.2
AUTOCUT_LLM_API_KEY=<填入你的 LLM key>
```

如果不需要 LLM 评分/remix，后三项可以留空。

### Step 4：创建数据目录

```bash
mkdir -p data/runs data/uploads
```

### Step 5：构建并启动

```bash
docker compose up -d --build
```

**首次启动**：
- M4 MBA 上 build 大约需要 **3~5 分钟**（pip install 依赖）
- 启动后看日志确认：

```bash
docker compose logs -f
```

看到 `Uvicorn running on http://0.0.0.0:8765` 就成功了。Ctrl+C 退出日志（容器仍在后台运行）。

### Step 6：访问业务台

```bash
open http://127.0.0.1:8765/business.html
```

浏览器会自动打开。

---

## 四、首次任务流程（验证环境可用）

1. **登录**：前端会要求填 token，输入 `.env` 里设置的 `AUTOCUT_API_TOKEN`
2. **填业务参数**：产品名、卖点、品牌词
3. **上传视频**或填 OSS 链接
4. **ASR 模型路径**：填 `/models/faster-whisper-small`（**容器内路径**，对应宿主机 `./models/faster-whisper-small`）
5. **提交任务** → 看日志：

```bash
docker compose logs -f
# 应该依次看到：
#   job enqueued
#   worker picked up job
#   asr running...
#   pipeline 完成
```

6. **看产物**：

```bash
ls data/runs/
# job_xxx/
#   metadata/      <- transcript.json / candidates.json / request.json
#   subtitles/     <- *.srt
#   clips/         <- *.mp4 候选片段
#   clips_compact/ <- 紧凑版片段
```

---

## 五、常见问题

### 1. 镜像架构错误，ASR 巨慢

**症状**：单条 5 分钟音频 ASR 跑了 20+ 分钟。

**原因**：build 出来的镜像是 x86_64，在 M 系列上跑 Rosetta 仿真。

**确认**：
```bash
docker inspect autocut:latest | grep Architecture
# 正确应是 "arm64"
```

**解决**：
```bash
docker compose down
docker rmi autocut:latest
# 在 docker-compose.yml 里取消注释这行：
#   platform: linux/arm64
docker compose up -d --build
```

### 2. 容器频繁 OOM 被 kill

**症状**：日志里看到 `Killed` 或容器突然停止。

**解决**：
- Docker Desktop → Resources → Memory 调到 10GB+
- 或在 `docker-compose.yml` 把 `deploy.resources.limits.memory` 加大（默认 6g）

### 3. 任务卡在 `queued` 不动

**症状**：任务创建后没有 `started` 日志。

**排查**：
```bash
docker compose ps
# 看容器是否在 Up 状态
docker compose logs --tail=200
# 看 worker 是否报错
```

常见原因：模型路径错了（`/models/faster-whisper-small` 不存在），或 LLM key 错。

### 4. 端口 8765 被占

**修改 `docker-compose.yml`**：

```yaml
ports:
  - "8866:8765"   # 把宿主机 8765 改成 8866
```

然后 `docker compose up -d` 重启，访问 `http://127.0.0.1:8866/business.html`。

### 5. M4 MBA 长时间跑会降频

**症状**：连续跑 3+ 个任务后，单任务耗时从 5 分钟变成 8 分钟。

**原因**：MBA 无风扇，CPU 持续高负载会触发 thermal throttling。

**缓解**：
- 任务间隔 5 分钟让 CPU 冷却
- 或换 MacBook Pro / Mac mini（有风扇）
- 长期生产环境建议用 Linux 服务器，不要用 MBA

---

## 六、日常维护命令

```bash
# 查看状态
docker compose ps

# 看实时日志
docker compose logs -f

# 重启服务（代码改动后必须重启）
docker compose restart

# 完全停止
docker compose down

# 代码 / 依赖改动后重新 build
docker compose up -d --build

# 清理旧任务产物（释放磁盘）
rm -rf data/runs/job_xxx
```

---

## 七、性能参考（M4 MacBook Air 16GB）

| 任务规模 | 预期耗时 |
|---|---|
| 10 分钟直播音频，ASR (small) | 1.5~3 分钟 |
| 完整 pipeline（ASR + 清洗 + 分段 + 评分 + 导出） | 5~10 分钟 |
| LLM remix（调远端 deepseek） | 30 秒~2 分钟 |

> 实际时间取决于音频复杂度和 LLM API 响应速度。

---

## 八、安全提醒

- ⚠️ 不要把 `.env` 提交到 git（`.dockerignore` 已排除 `.env*`，但 `.gitignore` 也要检查）
- ⚠️ `AUTOCUT_API_TOKEN` 用强随机字符串，不要用 `changeme`
- ⚠️ 若要对公网暴露，建议在前面加 Nginx + HTTPS + 限流
- ⚠️ Mac 默认不对外暴露端口（只听 127.0.0.1）；如果要让局域网其他设备访问，把 `docker-compose.yml` 的 `ports` 改成 `"0.0.0.0:8765:8765"`，并确保 Mac 防火墙允许

---

## 九、跨平台共存

如果你的 Windows 主机和 Mac 都想跑这套：

- **代码**：共用一个 git 仓库，pull 即可
- **模型**：分别存（4GB，不要 git 管理）
- **数据**：分别存（任务产物机器特定，互不污染）
- **环境变量**：各自 `.env`，token / key 可以一致

`docker-compose.yml` 里如果开启了 `platform: linux/arm64`，Windows 上 build 会失败——这时**注释掉那一行**即可，让 Docker 自动判断架构。
