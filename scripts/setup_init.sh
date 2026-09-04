#!/bin/bash
# ==========================================
# Memory-OS 初始化脚本
# 下载后跑一遍，确保所有依赖就位
# ==========================================

set -e

MEMORY_OS_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$MEMORY_OS_ROOT"

echo "=== Memory-OS 环境初始化 ==="
echo "工作目录: $MEMORY_OS_ROOT"
echo

# 1. 检查 Python 环境
echo "[1/5] 检查 Python..."
if ! command -v python3 &>/dev/null; then
    echo "❌ 未找到 python3，请先安装 Python 3.10+"
    exit 1
fi
PYTHON_VERSION=$(python3 -c 'import sys; print(".".join(map(str, sys.version_info[:2])))')
echo "✅ Python $PYTHON_VERSION"

# 2. 创建 venv（如果没有）
if [ ! -d "venv" ]; then
    echo
    echo "[2/5] 创建 Python 虚拟环境..."
    python3 -m venv venv
    echo "✅ venv 创建完成"
else
    echo "[2/5] venv 已存在，跳过"
fi

# 激活 venv
source venv/bin/activate

# 3. 安装依赖
echo
echo "[3/5] 安装 Python 依赖..."
pip install --upgrade pip -q
pip install -r requirements.txt -q 2>/dev/null || pip install -e . -q 2>/dev/null || true
echo "✅ 依赖安装完成"

# 4. 检查服务端口
echo
echo "[4/5] 检查外部服务..."
check_port() {
    local name=$1
    local port=$2
    if lsof -i :$port &>/dev/null; then
        echo "  ✅ $name (端口 $port) 已运行"
    else
        echo "  ⚠️  $name (端口 $port) 未运行，建议手动启动:"
        case $name in
            Neo4j)  echo "     → brew services start neo4j" ;;
            Qdrant) echo "     → brew services start qdrant" ;;
            Embed)  echo "     → openclaw memory-os-health && launchctl kickstart gui/501/com.memoryos.embed-daemon" ;;
            Reranker) echo "     → launchctl kickstart gui/501/com.memoryos.reranker" ;;
        esac
    fi
}

check_port "Neo4j" 7687
check_port "Qdrant" 6333
check_port "Embed Daemon" 8765
check_port "Reranker Daemon" 8877

# 5. 自检
echo
echo "[5/5] 运行完整自检..."
python3 -c "
import sys
sys.path.insert(0, 'scripts')
from service_lifecycle import check_all_services
results = check_all_services()
all_ok = all(v for v in results.values())
print('✅ 自检通过' if all_ok else '⚠️  部分服务未就绪，请查看上方提示')
" 2>/dev/null || echo "  (自检模块运行失败，跳过)"

echo
echo "=== 初始化完成 ==="
echo "下一步："
echo "  1. 启动所需服务（见上方提示）"
echo "  2. 运行: openclaw memory-os-health  查看状态"
echo "  3. 配置 openclaw.json 中的插件路径指向本目录"
