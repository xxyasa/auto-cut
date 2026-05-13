# Ubuntu Linux 部署指南

本文档针对 **Ubuntu Server**（20.04 / 22.04 / 24.04 LTS 均适用）的生产部署。
适合内网共享、公网对外、长期运行等场景。

> 如果是个人 Mac 试用，请参考 [deploy-mac.md](./deploy-mac.md)。

---

## 一、服务器选型建议

### 最低配置（够跑）

| 项 | 要求 |
|---|---|
| CPU | 4 核 x86_64 |
| 内存 | 8 GB |
| 磁盘 | 50 GB SSD |
| OS | Ubuntu 20.04+ |
| 网络 | 100 Mbps |

### 推荐配置（生产）

| 项 | 推荐 |
|---|---|
| CPU | 8 核 + 高主频（ASR 是 CPU 密集型） |
| 内存 | 16~32 GB |
| 磁盘 | 200 GB+ SSD（任务产物会增长） |
| GPU | 可选，NVIDIA T4 / A10 / 4090，10x 加速 ASR |
| 网络 | 千兆内网 + 公网 IP（如需对外） |

---

## 二、装 Docker Engine（一次性）

```bash
# 一键安装脚本（官方）
curl -fsSL https://get.docker.com | sudo sh

# 让普通用户能用 docker（避免每次 sudo）
sudo usermod -aG docker $USER
newgrp docker  # 立刻生效，或重新登录

# 验证
docker --version
docker compose version
```

> 不要装老版的 `docker.io` + `docker-compose`（python 版）。一键脚本装的是新的 Docker Engine + Compose v2 插件。

### 设置开机自启

```bash
sudo systemctl enable docker
```

---

## 三、拉代码 + 准备目录

```bash
# 任意位置，建议 /opt 或用户家目录
cd ~  # 或 sudo mkdir -p /opt && cd /opt

# 拉项目
git clone <your-repo-url> auto-cut
cd auto-cut

# 创建数据目录（不在 git 管理范围）
mkdir -p data/runs data/uploads models

# 准备环境变量
cp .env.example .env
nano .env
```

`.env` 至少填以下三项：

```bash
# 业务台前端鉴权 token，自行设置强随机字符串
AUTOCUT_API_TOKEN=$(openssl rand -hex 32)

# LLM 接口（用于评分和 remix 重排，可选）
AUTOCUT_LLM_API_URL=https://model-api.ecmax.cn/v1/chat/completions
AUTOCUT_LLM_MODEL=deepseek-v3.2
AUTOCUT_LLM_API_KEY=<填入你的 LLM key>
```

> 提示：`openssl rand -hex 32` 在 shell 里能直接生成强随机 token。
> 不要把 `.env` 提交到 git（`.gitignore` 已排除）。

---

## 四、下载模型（关键步骤）

模型文件**不在 git 仓库**，也**不在 Docker 镜像**里，需要单独下载到 `./models/`。

### 推荐方式：服务器直接从国内镜像下载

```bash
cd ~/auto-cut/models
mkdir -p faster-whisper-small
cd faster-whisper-small

MIRROR="https://hf-mirror.com"
REPO="Systran/faster-whisper-small"
for f in config.json tokenizer.json vocabulary.txt model.bin; do
  echo "Downloading $f..."
  curl -L -C - --retry 50 --retry-delay 5 \
       -o "$f" \
       "$MIRROR/$REPO/resolve/main/$f"
done

cd ../..

# 验证（应该看到 4 个文件，model.bin 约 461MB）
ls -lh models/faster-whisper-small/
```

服务器一般带宽 100Mbps+，整个 463MB 下载 **30~60 秒**。

### 备用方式：从开发机传到服务器

```bash
# 从 Windows
scp -r F:\workCode\auto-cut\models\faster-whisper-small \
    user@server-ip:/home/user/auto-cut/models/

# 从 Mac/Linux
rsync -avzP models/faster-whisper-small/ \
    user@server-ip:/home/user/auto-cut/models/faster-whisper-small/
```

### 想用更大模型？

```bash
# 改最后一个变量即可：tiny / base / small / medium / large-v3
for size in medium; do
  mkdir -p ~/auto-cut/models/faster-whisper-$size
  cd ~/auto-cut/models/faster-whisper-$size
  for f in config.json tokenizer.json vocabulary.txt model.bin; do
    curl -L -C - --retry 50 -o "$f" \
         "https://hf-mirror.com/Systran/faster-whisper-$size/resolve/main/$f"
  done
done
```

| 模型 | 大小 | CPU 速度 | 精度 | 推荐场景 |
|---|---|---|---|---|
| `tiny` | 75MB | <1 分钟 | 差 | 仅测试 |
| `base` | 145MB | ~1 分钟 | 一般 | 演示 |
| **`small`**（默认） | **480MB** | 1.5~3 分钟 | 中文够用 | **生产起步** |
| `medium` | 1.5GB | 3~6 分钟 | 明显更好 | 生产推荐 |
| `large-v3` | 3GB | 8~15 分钟 | 最好 | 高质量需求 |

> 时间参考 8 核 x86 CPU 处理 10 分钟音频；GPU 大约 10x 更快。

---

## 五、启动

```bash
cd ~/auto-cut
docker compose up -d --build
```

**首次启动**会比较慢（3~5 分钟，下载基础镜像 + 装依赖）。

### 看日志

```bash
docker compose logs -f
# 看到 "Uvicorn running on http://0.0.0.0:8765" 就 OK
# Ctrl+C 退出日志（容器仍在后台运行）
```

### 验证服务起来了

```bash
curl http://127.0.0.1:8765/business.html | head -3
# 应该返回 HTML
```

---

## 六、对外暴露：内网 vs 公网

### 方案 A：内网访问（同 VPN / 局域网内）

`docker-compose.yml` 默认监听 `0.0.0.0:8765`，内网机器直接访问：

```
http://<服务器内网IP>:8765/business.html
```

防火墙放行：

```bash
sudo ufw allow from 192.168.0.0/16 to any port 8765
```

### 方案 B：公网访问（推荐：Nginx + HTTPS）

⚠️ **不要直接把 8765 端口暴露公网**。前面套 Nginx + Let's Encrypt 证书。

#### 1. 改 compose 让 8765 只监听本地

编辑 `docker-compose.yml`：

```yaml
ports:
  - "127.0.0.1:8765:8765"   # 只监听 127.0.0.1
```

```bash
docker compose up -d
```

#### 2. 装 Nginx + Certbot

```bash
sudo apt update
sudo apt install -y nginx certbot python3-certbot-nginx
```

#### 3. Nginx 配置

```bash
sudo nano /etc/nginx/sites-available/autocut
```

写入：

```nginx
server {
    listen 80;
    server_name autocut.yourdomain.com;

    # 上传大视频，关掉 body size 限制
    client_max_body_size 5G;

    # ASR / 视频导出是长任务，关掉 proxy timeout
    proxy_read_timeout 3600s;
    proxy_send_timeout 3600s;
    proxy_connect_timeout 60s;

    location / {
        proxy_pass http://127.0.0.1:8765;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        # WebSocket / SSE 支持（流式 LLM 用）
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_buffering off;
    }
}
```

启用配置：

```bash
sudo ln -s /etc/nginx/sites-available/autocut /etc/nginx/sites-enabled/
sudo nginx -t   # 测试配置语法
sudo systemctl reload nginx
```

#### 4. 配 HTTPS 证书（Let's Encrypt 免费）

```bash
sudo certbot --nginx -d autocut.yourdomain.com
# 按提示输入邮箱、同意 ToS、选自动 HTTPS 跳转
```

certbot 会自动改 nginx 配置加上 SSL 段，并配置自动续期。

#### 5. 防火墙

```bash
sudo ufw allow 80/tcp     # HTTP（certbot 验证用）
sudo ufw allow 443/tcp    # HTTPS
sudo ufw allow 22/tcp     # SSH
sudo ufw enable
sudo ufw status
```

访问：`https://autocut.yourdomain.com/business.html`

---

## 七、生产环境额外优化

### 优化 1：日志轮转（防磁盘撑爆）

编辑 `docker-compose.yml`，给 `autocut` 服务加：

```yaml
services:
  autocut:
    # ... 现有配置 ...
    logging:
      driver: json-file
      options:
        max-size: "50m"
        max-file: "5"
```

`docker compose up -d` 重启生效。

### 优化 2：定期清理旧任务产物

`data/runs/` 会随着任务增多而增长，每个任务大约 50~200MB。

```bash
crontab -e
# 加一行：每周日凌晨 3 点清理 30 天前的任务产物
0 3 * * 0 find /home/user/auto-cut/data/runs -maxdepth 1 -type d -name 'job_*' -mtime +30 -exec rm -rf {} +
```

### 优化 3：监控容器状态

```bash
# 实时资源占用
docker stats autocut

# 健康状态（如果 Dockerfile 加了 healthcheck）
docker inspect --format='{{.State.Health.Status}}' autocut
```

### 优化 4：备份数据

```bash
# 重要：data/runs/、data/uploads/、.env、models/
# 模型可以重新下载，data/ 是任务产物不能丢
tar -czf autocut-backup-$(date +%F).tar.gz data/ .env
```

---

## 八、可选：NVIDIA GPU 加速

如果服务器有 NVIDIA GPU（T4 / A10 / 4090 等），ASR 能从 CPU 的 3 分钟降到 **20 秒**。

### 1. 装 NVIDIA Container Toolkit

```bash
# Ubuntu 22.04 示例
curl -s -L https://nvidia.github.io/libnvidia-container/gpgkey | \
    sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list | \
    sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' | \
    sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list
sudo apt update
sudo apt install -y nvidia-container-toolkit
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker
```

### 2. 验证 GPU 可见

```bash
docker run --rm --gpus all nvidia/cuda:12.4.1-base-ubuntu22.04 nvidia-smi
# 应该看到 GPU 信息
```

### 3. 改 `docker-compose.yml`

把 `deploy.resources.limits.memory` 那段替换成：

```yaml
deploy:
  resources:
    reservations:
      devices:
        - driver: nvidia
          count: 1
          capabilities: [gpu]
```

### 4. 改 Dockerfile 基础镜像

把首行改成（**注意：此改动较大，建议第一阶段先不做**）：

```dockerfile
FROM nvidia/cuda:12.4.1-cudnn-runtime-ubuntu22.04

RUN apt-get update && apt-get install -y --no-install-recommends \
        python3.11 python3-pip ffmpeg curl \
        && rm -rf /var/lib/apt/lists/* \
        && ln -sf /usr/bin/python3.11 /usr/bin/python
# 后续与原 Dockerfile 一样
```

### 5. 创建任务时使用 GPU

ASR 参数：
- `device=cuda`
- `compute_type=float16`（GPU 上不要用 int8）

---

## 九、常见问题

### 1. `docker compose up` 报 permission denied

**原因**：用户没加入 docker 组。

```bash
sudo usermod -aG docker $USER
newgrp docker
# 或重新登录
```

### 2. 端口 8765 已被占用

```bash
sudo lsof -i :8765
# 或
sudo ss -tlnp | grep 8765
```

改 `docker-compose.yml` 把映射端口换掉：

```yaml
ports:
  - "127.0.0.1:8866:8765"
```

### 3. 模型路径填错

任务创建时 ASR 模型路径**必须填 `/models/faster-whisper-small`**（容器内路径），不是宿主机路径。

### 4. 任务卡在 queued 不动

```bash
docker compose logs --tail=200
# 看 worker 是否报错
```

### 5. 磁盘满了

```bash
# 看哪儿大
du -sh data/runs/* | sort -rh | head -20

# 清理旧任务
rm -rf data/runs/job_<old_id>

# 清理 Docker 自身
docker system prune -af --volumes
```

### 6. 容器内时区不对（日志时间错位）

`docker-compose.yml` 加：

```yaml
environment:
  TZ: Asia/Shanghai
```

---

## 十、日常维护命令速查

```bash
# 状态
docker compose ps
docker compose logs -f
docker stats autocut

# 重启服务（代码改动后必须）
docker compose restart

# 完全停止
docker compose down

# 拉新代码 + 重新部署
git pull
docker compose up -d --build

# 查看磁盘占用
du -sh data/runs/
df -h

# 备份
tar -czf autocut-$(date +%F).tar.gz data/ .env

# 进容器排查
docker compose exec autocut bash
```

---

## 十一、安全 Checklist

- [ ] `.env` 里 `AUTOCUT_API_TOKEN` 用强随机字符串，不要用 `changeme`
- [ ] `.env` 不要提交到 git（`.gitignore` 已排除）
- [ ] 公网部署务必走 Nginx + HTTPS
- [ ] 8765 端口不要直接对公网开放
- [ ] SSH 用 key 登录，禁用密码登录
- [ ] 定期 `apt update && apt upgrade -y` 打补丁
- [ ] LLM API key 定期轮换
- [ ] 数据目录权限：`chmod 750 data/`
