#!/usr/bin/env bash
# P4（安全与 HITL）验证脚本
# 用法：把文件最底部想跑的那一行取消注释，然后执行：bash tests/gates/run_p4.sh
set -uo pipefail

CONDA_BASE="$(conda info --base 2>/dev/null || echo "$HOME/miniconda3")"
source "$CONDA_BASE/etc/profile.d/conda.sh"
conda activate zeke

cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit 1

PYTHON=python
CLI=zkagent
INDUSTRY=homeapp
# 约定：homeapp 是参考实现；appliance 只作「不回归」的兼容基线。

gate() {
    # 注意：--industry 是**全局参数**，必须写在子命令之前
    echo "[1/5] 策略层单测（规则 / 事实 / 审批 / 编排 / 行业包契约）"
    policy || return 1

    echo "[2/5] 全量回归（appliance 既有断言必须零改动）"
    all || return 1

    echo "[3/5] 框架边界守卫（策略层不得 import 框架）"
    guard || return 1

    echo "[4/5] homeapp 索引必须仍是 0/0/0（P4 不改知识）"
    index || return 1

    echo "[5/5] CLI 装配冒烟（approvals list）"
    cli-smoke || return 1
}

policy() {
    "$PYTHON" -m pytest -q tests/policy || return 1
}

approvals() {
    "$PYTHON" -m pytest -q tests/policy/test_approvals.py || return 1
}

identity() {
    # 覆盖「未核验拒绝 / 核验后放行 / 核验前置条件」三类断言
    "$PYTHON" -m pytest -q tests/policy -k "verified or identity" || return 1
}

scope() {
    "$PYTHON" -m pytest -q tests/policy -k "scope or fridge" || return 1
}

guard() {
    # 框架边界：策略层只能依赖协议，不得 import llama_index / langgraph
    "$PYTHON" -m pytest -q tests/contract/test_framework_boundary.py || return 1
}

index() {
    # 只读：知识与索引应当一致，added / modified / removed 必须全为 0
    local out
    out="$("$CLI" --industry "$INDUSTRY" rag index --dry-run 2>/dev/null)" || return 1
    printf '%s' "$out" | "$PYTHON" -c '
import json
import sys

data = json.load(sys.stdin)
counts = {key: len(data.get(key) or []) for key in ("added", "modified", "removed")}
print("  dry-run:", counts, "unchanged:", len(data.get("unchanged") or []))
raise SystemExit(0 if not any(counts.values()) else 1)
' || return 1
}

cli-smoke() {
    # 装配冒烟：审批库、规则、行业包任一处配错，这里就会失败
    local out
    out="$("$CLI" --industry "$INDUSTRY" approvals list 2>/dev/null)" || return 1
    grep -q '"ok": true' <<<"$out" || {
        echo "失败：approvals list 未返回 ok（输出如下）"
        echo "$out" | head -5
        return 1
    }
    echo "OK：approvals CLI 可用"
}

acceptance() {
    # 需要 DEEPSEEK_API_KEY：7 场景真实调用模型，耗时数分钟
    "$PYTHON" -m scripts.run_p4_acceptance --industry "$INDUSTRY" --provider deepseek || return 1
}

all() {
    "$PYTHON" -m pytest -q || return 1
}

full() {
    gate || return 1
    guard || return 1
    all || return 1
    echo "P4 全部检查通过（未包含真实模型验收）"
}

# ============================================================
# 调用入口：只保留一行未注释，其余按需取消注释后执行
# ============================================================

gate    # P4 Gate：策略单测 → 全量回归 → 边界守卫 → 索引 0/0/0 → CLI 冒烟

# ---- 单点检查 ----
# policy      # 只跑策略层单测（规则 / 事实 / 审批 / 编排 / 行业包契约）
# approvals   # 只跑审批状态机、参数指纹与一次性消费
# identity    # 只跑身份门禁（未核验拒绝 / 核验后放行）
# scope       # 只跑未收录品类拦截与安全红线
# guard       # 只跑框架边界守卫
# index       # 只检查 homeapp 索引仍为 0/0/0
# cli-smoke   # 只检查 approvals CLI 能正常装配
# acceptance  # 真实模型 7 场景验收（需要 DEEPSEEK_API_KEY，耗时较长）
# all         # 只跑全量 pytest
# full        # Gate + 边界 + 全量（不含真实模型）
