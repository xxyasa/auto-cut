#!/usr/bin/env bash
# 一键部署 auto-cut 到 Linux 服务器
# 在服务器上执行: bash deploy.sh
set -e

REPO="https://github.com/xxyasa/auto-cut.git"
DEPLOY_DIR="/usr/local/auto-cut"
PORT=8765

echo "=========================================="
echo " Auto Cut 部署脚本"
echo "=========================================="

# 安装 Docker（如果没有）
if ! command -v docker &>/dev/null; then
  echo "[1/5] 安装 Docker..."
  curl -fsSL https://get.docker.com | sh
  systemctl start docker
  systemctl enable docker
  echo "Docker 安装完成"
else
  echo "[1/5] Docker 已安装: $(docker --version)"
fi

# 克隆或更新代码
if [ -d "$DEPLOY_DIR/.git" ]; then
  echo "[2/5] 更新代码..."
  git -C "$DEPLOY_DIR" pull origin main
else
  echo "[2/5] 克隆代码..."
  git clone "$REPO" "$DEPLOY_DIR"
fi

cd "$DEPLOY_DIR"

# 创建数据目录
echo "[3/5] 创建数据目录..."
mkdir -p data/runs data/uploads models

# 初始化 .env（如果不存在）
if [ ! -f .env ]; then
  cp .env.example .env
  # 生成随机 token
  TOKEN="autocut-$(head -c 8 /dev/urandom | xxd -p)"
  sed -i "s/changeme-please-set-a-strong-token/$TOKEN/" .env
  echo ""
  echo "  ⚠️  已自动生成 API Token: $TOKEN"
  echo "  ⚠️  如需配置 LLM，编辑 $DEPLOY_DIR/.env"
  echo ""
fi

# 构建并启动
echo "[4/5] 构建并启动服务（首次约 3-5 分钟）..."
docker compose up -d --build

# 验证
echo "[5/5] 验证服务..."
sleep 5
if docker compose ps | grep -q "Up"; then
  TOKEN=$(grep AUTOCUT_API_TOKEN .env | cut -d= -f2)
  echo ""
  echo "=========================================="
  echo " 部署成功！"
  echo "=========================================="
  echo " 访问地址: http://$(hostname -I | awk '{print $1}'):${PORT}/business.html"
  echo " API Token: $TOKEN"
  echo " 日志: docker compose logs -f"
  echo "=========================================="
else
  echo "⚠️  容器可能未正常启动，查看日志："
  docker compose logs --tail=30
fi
