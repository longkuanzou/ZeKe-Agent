#!/usr/bin/env bash
# P7（交付 + 通用性验证）验证脚本
# 用法：把文件最底部想跑的那一行取消注释，然后执行：bash tests/gates/run_p7.sh
set -uo pipefail

CONDA_BASE="$(conda info --base 2>/dev/null || echo "$HOME/miniconda3")"
source "$CONDA_BASE/etc/profile.d/conda.sh"
conda activate zeke

cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit 1

PYTHON=python
CLI=zkagent

gate() {
    echo "[1/5] 3c 行业包：加载 / 查单 / 全包链路规约"
    pack || return 1

    echo "[2/5] 双后端契约：mock 与 http 同一套断言"
    backends || return 1

    echo "[3/5] API：health / chat / HITL / 回调 501"
    api || return 1

    echo "[4/5] 后端切换：同一 Core 驱动 mock 与 http"
    switch || return 1

    echo "[5/5] 框架边界守卫"
    guard || return 1

    echo "P7 代码 Gate 通过"
}

pack() {
    "$PYTHON" -m pytest -q tests/industry/test_3c_pack.py tests/rag/test_journey_taxonomy.py || return 1
}

backends() {
    "$PYTHON" -m pytest -q tests/contract tests/backends/test_http_backend.py || return 1
}

api() {
    "$PYTHON" -m pytest -q tests/delivery || return 1
}

switch() {
    "$PYTHON" -m pytest -q tests/backends/test_backend_switch.py || return 1
}

guard() {
    "$PYTHON" -m pytest -q tests/contract/test_framework_boundary.py || return 1
}

all() {
    "$PYTHON" -m pytest -q || return 1
}

docker() {
    if ! docker info >/dev/null 2>&1; then
        echo "失败：Docker daemon 未运行；请先启动 daemon 或设置可用的 DOCKER_HOST"
        return 2
    fi
    docker compose up -d --build || return 1
    local i
    for i in $(seq 1 60); do
        if "$PYTHON" -c '
import urllib.request
try:
    urllib.request.urlopen("http://127.0.0.1:8000/health", timeout=2)
except Exception:
    raise SystemExit(1)
' >/dev/null 2>&1; then
            break
        fi
        sleep 2
    done
    "$PYTHON" -m scripts.smoke_p7_api --base-url http://127.0.0.1:8000 || return 1
}

core-diff() {
    echo "=== zkagent/core diff（P7 期望为空） ==="
    local diff
    diff="$(git diff --stat -- zkagent/core)"
    printf '%s\n' "$diff"
    if [ -n "$diff" ]; then
        echo "失败：P7 修改了 Core"
        return 1
    fi
    if git status --short -- zkagent/core | grep -q '^??'; then
        echo "失败：P7 新增了未跟踪的 Core 文件"
        git status --short -- zkagent/core
        return 1
    fi
    echo "OK：P7 未改 Core；新增能力都在 backends / channels / apps / config"
}

quickstart() {
    "$CLI" --industry 3c db reset >/dev/null || return 1
    "$CLI" --industry 3c db seed >/dev/null || return 1
    "$CLI" --industry 3c chat --provider fake --task "查订单 B2001" --user U9 \
        | grep -q "B2001" || return 1
    echo "OK：3c 快速开始可跑通"
}

full() {
    gate || return 1
    all || return 1
    docker || return 1
    core-diff || return 1
    echo "P7 全部检查通过"
}

# ============================================================
# 调用入口：只保留一行未注释，其余按需取消注释后执行
# ============================================================

gate    # P7 代码 Gate：3c → 双后端 → API → 切换 → 边界

# ---- 单点检查 ----
# docker      # 构建并启动 compose，跑 health/chat/HITL 冒烟
# core-diff   # 打印 Core 基线并检查 P7 未新增 Core 文件
# quickstart  # 只跑 3c 的本地快速开始
# all         # 只跑全量 pytest
# full        # 代码 Gate + 全量 + Docker + Core 对照
