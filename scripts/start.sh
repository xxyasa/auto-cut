#!/usr/bin/env bash
# 启动 auto-cut 业务自助成片服务（Linux/macOS 直接运行，不依赖 Docker）
# 用法：
#   chmod +x scripts/start.sh
#   ./scripts/start.sh
#   ./scripts/start.sh --port 8770 --host 0.0.0.0

set -euo pipefail

BIND_HOST="${BIND_HOST:-127.0.0.1}"
PORT="${PORT:-8765}"
RELOAD=""

# 解析参数
while [[ $# -gt 0 ]]; do
  case "$1" in
    --host) BIND_HOST="$2"; shift 2 ;;
    --port) PORT="$2"; shift 2 ;;
    --reload) RELOAD="--reload"; shift ;;
    *) echo "Unknown arg: $1"; exit 1 ;;
  esac
done

# 切到项目根目录
cd "$(dirname "$0")/.."

# 加载 .env
if [[ -f .env ]]; then
  set -o allexport
  source .env
  set +o allexport
fi

# Token 管理
if [[ -z "${AUTOCUT_API_TOKEN:-}" ]]; then
  if [[ -f .dev-token ]]; then
    AUTOCUT_API_TOKEN=$(cat .dev-token)
  else
    AUTOCUT_API_TOKEN="devtoken-$(head -c 6 /dev/urandom | xxd -p)"
    echo "$AUTOCUT_API_TOKEN" > .dev-token
  fi
fi
export AUTOCUT_API_TOKEN

export PYTHONUNBUFFERED=1
export AUTOCUT_RUNS_DIR="${AUTOCUT_RUNS_DIR:-$(pwd)/data/runs}"

mkdir -p data/runs data/uploads

echo ""
echo "================================================"
echo " Auto Cut Business Server"
echo "================================================"
echo " URL  : http://${BIND_HOST}:${PORT}/business.html"
echo " API  : http://${BIND_HOST}:${PORT}/api/business/"
echo " Token: ${AUTOCUT_API_TOKEN}"
echo " Runs : ${AUTOCUT_RUNS_DIR}"
echo "================================================"
echo ""

# 优先用 venv，否则用系统 python
PYTHON="python3"
if [[ -f .venv/bin/python ]]; then
  PYTHON=".venv/bin/python"
fi

exec "$PYTHON" -m uvicorn autocut.api:app \
  --host "$BIND_HOST" \
  --port "$PORT" \
  --workers 1 \
  $RELOAD
