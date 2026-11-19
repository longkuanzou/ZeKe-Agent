#!/usr/bin/env bash
# P6.5（加固）验证脚本
# 用法：把文件最底部想跑的那一行取消注释，然后执行：bash tests/gates/run_p6_5.sh
set -uo pipefail

CONDA_BASE="$(conda info --base 2>/dev/null || echo "$HOME/miniconda3")"
source "$CONDA_BASE/etc/profile.d/conda.sh"
conda activate zeke

cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit 1

PYTHON=python
CLI=zkagent
INDUSTRY=appliance

gate() {
    echo "[1/6] 安全：降级闸门 / 审批人权限 / 重启恢复"
    security || return 1

    echo "[2/6] 并发：索引锁 + 后台构建状态"
    concurrency || return 1

    echo "[3/6] 检索：用例规模 / 包级阈值 / 四配置指标"
    retrieval || return 1

    echo "[4/6] 数据：时间宏 / db check / 参数扫描 / 启动检查"
    data || return 1

    echo "[5/6] 评估隔离：临时库不污染演示数据"
    isolation || return 1

    echo "[6/6] 边界守卫"
    guard || return 1

    echo "P6.5 Gate 通过"
}

security() {
    "$PYTHON" -m pytest -q \
        tests/rag/test_gate_fallback.py \
        tests/policy/test_approver_permissions.py \
        tests/orchestrator/test_restart_resume.py \
        tests/policy/test_permissions.py || return 1
}

concurrency() {
    "$PYTHON" -m pytest -q tests/rag/test_index_lock.py || return 1
    "$CLI" --industry "$INDUSTRY" rag info >/dev/null || return 1
    echo "OK：索引锁单测通过，rag info 可读"
}

retrieval() {
    "$PYTHON" -m pytest -q tests/rag/test_calibration.py || return 1
    local out
    out="$("$CLI" --industry "$INDUSTRY" rag eval-retrieval --k 3 2>/dev/null)" || return 1
    printf '%s' "$out" | "$PYTHON" -c '
import json
import sys

data = json.load(sys.stdin)
runs = {item["mode"]: item for item in data.get("runs", [])}
required = {"sparse", "dense", "hybrid", "hybrid+rerank"}
assert required <= set(runs), f"缺少配置：{required - set(runs)}"
assert all(item["cases"] >= 40 for item in runs.values()), "用例数不足 40"
print("  cases=%s modes=%s" % (runs["hybrid"]["cases"], ",".join(sorted(runs))))
' || return 1
}

data() {
    "$PYTHON" -m pytest -q tests/industry/test_seed_time_macros.py tests/rag/test_knowledge_param_scan.py \
        tests/industry/test_startup_check.py || return 1
    "$CLI" --industry "$INDUSTRY" db check >/dev/null || return 1
    "$PYTHON" -m scripts.check_knowledge_params --pack "$INDUSTRY" >/dev/null || return 1
    echo "OK：时间宏 / db check / 参数扫描 / 启动检查"
}

isolation() {
    "$PYTHON" -m pytest -q tests/evals/test_isolation.py || return 1
}

guard() {
    "$PYTHON" -m pytest -q tests/contract/test_framework_boundary.py || return 1
}

all() {
    "$PYTHON" -m pytest -q || return 1
}

full() {
    gate || return 1
    all || return 1
    echo "P6.5 全部检查通过"
}

background() {
    local out
    out="$("$CLI" --industry "$INDUSTRY" rag index --background --rebuild 2>/dev/null)" || return 1
    printf '%s' "$out" | grep -q '"background": true' || return 1
    local i status
    for i in $(seq 1 90); do
        status="$("$CLI" --industry "$INDUSTRY" rag status 2>/dev/null)" || return 1
        if printf '%s' "$status" | grep -q '"status": "succeeded"'; then
            echo "OK：后台构建在 ${i}s 内完成"
            return 0
        fi
        if printf '%s' "$status" | grep -q '"status": "failed"'; then
            echo "失败：后台构建失败"
            printf '%s\n' "$status"
            return 1
        fi
        sleep 1
    done
    echo "失败：后台构建 90s 未完成"
    return 1
}

deepseek() {
    "$CLI" --industry appliance eval run --provider deepseek --k 3 \
        --report out/report-deepseek-appliance.md || return 1
    "$CLI" --industry homeapp eval run --provider deepseek --k 3 \
        --report out/report-deepseek-homeapp.md || return 1
}

# ============================================================
# 调用入口：只保留一行未注释，其余按需取消注释后执行
# ============================================================

gate    # P6.5 Gate：安全 → 并发 → 检索 → 数据 → 隔离 → 边界

# ---- 单点检查 ----
# background  # 实际跑一次后台构建并轮询 rag status
# deepseek    # 两包真实模型评估（需要 DEEPSEEK_API_KEY，耗时较长）
# all         # 只跑全量 pytest
# full        # Gate + 全量
