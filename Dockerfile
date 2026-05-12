# syntax=docker/dockerfile:1
FROM python:3.11-slim

# 系统依赖：FFmpeg + 编译工具
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        curl \
        && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 先只复制依赖描述文件，利用 Docker 层缓存
COPY pyproject.toml ./
COPY src/ ./src/

# 安装项目依赖（business extras 包含 fastapi/uvicorn/httpx/minio）
RUN pip install --no-cache-dir -e ".[business,asr]"

# 静态文件（html/js/css）由 package-data 已随 src/ 一起进来
# 数据目录运行时通过 volume 挂载
RUN mkdir -p /data/runs /data/uploads /models

# 非 root 用户运行
RUN groupadd -r autocut && useradd -r -g autocut autocut \
    && chown -R autocut:autocut /app /data /models
USER autocut

EXPOSE 8765

# 启动：环境变量由 docker-compose / -e 注入
CMD ["python", "-m", "uvicorn", "autocut.api:app", \
     "--host", "0.0.0.0", \
     "--port", "8765", \
     "--workers", "1"]
