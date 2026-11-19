#!/usr/bin/env bash
# P5（上下文工程 + 工具注入 + 编排韧性）验证脚本
# 用法：把文件最底部想跑的那一行取消注释，然后执行：bash tests/gates/run_p5.sh
set -uo pipefail

CONDA_BASE="$(conda info --base 2>/dev/null || echo "$HOME/miniconda3")"
source "$CONDA_BASE/etc/profile.d/conda.sh"
conda activate zeke

cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit 1

PYTHON=python
CLI=zkagent
INDUSTRY=homeapp

gate() {
    # 注意：--industry 是**全局参数**，必须写在子命令之前
    echo "[1/5] 上下文压缩与配对保护（两种策略 + 故障降级）"
    context || return 1

    echo "[2/5] 工具层与后端契约（含 3 个新工具）"
    tools || return 1

    echo "[3/5] 注入上限必须生效（全集 20 → 注入 10）"
    preview || return 1

    echo "[4/5] 全量回归（P1–P4 既有断言零改动）"
    all || return 1

    echo "[5/5] 框架边界守卫（压缩不得引入框架依赖）"
    guard || return 1
}

context() {
    "$PYTHON" -m pytest -q tests/context || return 1
}

tools() {
    "$PYTHON" -m pytest -q tests/tools tests/contract || return 1
}

preview() {
    local out
    out="$("$CLI" --industry "$INDUSTRY" tools list --inject-preview --max 10 2>/dev/null)" || return 1
    printf '%s' "$out" | "$PYTHON" -c '
import json
import sys

data = json.load(sys.stdin)
preview = data["inject_preview"]
print(
    "  enabled=%s injected=%s ratio=%s missing_core=%s"
    % (
        preview["enabled"],
        preview["injected"],
        preview["ratio"],
        preview["missing_core"],
    )
)
ok = (
    preview["enabled"] >= 20
    and preview["injected"] == 10
    and preview["injected"] < preview["enabled"]
    and not preview["missing_core"]
)
raise SystemExit(0 if ok else 1)
' || return 1
}

guard() {
    "$PYTHON" -m pytest -q tests/contract/test_framework_boundary.py || return 1
}

all() {
    "$PYTHON" -m pytest -q || return 1
}

full() {
    gate || return 1
    guard || return 1
    all || return 1
    echo "P5 全部检查通过"
}

# ============================================================
# 调用入口：只保留一行未注释，其余按需取消注释后执行
# ============================================================

gate    # P5 Gate：压缩 → 工具契约 → 注入上限 → 全量回归 → 边界守卫

# ---- 单点检查 ----
# context     # 只跑上下文压缩与故障降级
# tools       # 只跑工具层与后端契约
# preview     # 只检查注入上限与核心工具是否齐全
# guard       # 只跑框架边界守卫
# all         # 只跑全量 pytest
# full        # Gate + 边界 + 全量
