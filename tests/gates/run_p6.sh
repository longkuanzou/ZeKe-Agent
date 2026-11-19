#!/usr/bin/env bash
# P6（评估 + 可观测）验证脚本
# 用法：把文件最底部想跑的那一行取消注释，然后执行：bash tests/gates/run_p6.sh
set -uo pipefail

CONDA_BASE="$(conda info --base 2>/dev/null || echo "$HOME/miniconda3")"
source "$CONDA_BASE/etc/profile.d/conda.sh"
conda activate zeke

cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit 1

PYTHON=python
CLI=zkagent
INDUSTRY=appliance

gate() {
    echo "[1/6] 评估单元测试（加载 / 引用 / 断言 / 报告）"
    evals || return 1

    echo "[2/6] 准备数据与索引（appliance：确定性基线）"
    prepare || return 1

    echo "[3/6] 一键评估（Fake，20 条五类用例）"
    report || return 1

    echo "[4/6] 关闭 LangSmith 仍可出报告"
    notrace || return 1

    echo "[5/6] 压缩前后对照"
    compression || return 1

    echo "[6/6] Trace 定位（用上一步报告的 trace_id）"
    traces || return 1
}

evals() {
    "$PYTHON" -m pytest -q tests/evals || return 1
}

prepare() {
    "$CLI" --industry "$INDUSTRY" db reset >/dev/null || return 1
    "$CLI" --industry "$INDUSTRY" db seed >/dev/null || return 1
    "$CLI" --industry "$INDUSTRY" rag index >/dev/null || return 1
}

report() {
    local out
    out="$("$CLI" --industry "$INDUSTRY" eval run --report out/report.md 2>/dev/null)" || return 1
    printf '%s' "$out" | "$PYTHON" -c '
import json
import sys

data = json.load(sys.stdin)
print(
    "  cases=%s passed=%s pass@1=%s pass^k=%s"
    % (data["cases"], data["passed"], data["pass_at_1"], data["pass_at_k"])
)
raise SystemExit(0 if data["cases"] >= 20 and data["passed"] == data["cases"] else 1)
' || return 1
}

notrace() {
    local out
    out="$(env -u LANGSMITH_API_KEY "$CLI" --industry "$INDUSTRY" eval run --report out/report_notrace.md 2>/dev/null)" || return 1
    grep -q '"ok": true' <<<"$out" || {
        echo "失败：关闭 LangSmith 后评估未通过"
        echo "$out" | head -5
        return 1
    }
    echo "OK：无 LangSmith 也能出报告"
}

compression() {
    local out
    out="$("$CLI" --industry "$INDUSTRY" eval context-compression --turns 25 --report out/compression.md 2>/dev/null)" || return 1
    printf '%s' "$out" | "$PYTHON" -c '
import json
import sys

data = json.load(sys.stdin)
print(
    "  chars %s → %s（保留 %.1f%%），retention=%s"
    % (
        data["chars_before"],
        data["chars_after"],
        data["ratio"] * 100,
        data["retention_passed"],
    )
)
raise SystemExit(0 if data["retention_passed"] and data["chars_after"] < data["chars_before"] else 1)
' || return 1
}

traces() {
    local trace_id
    trace_id="$("$PYTHON" -c '
import json
print(next(item["trace_id"] for item in json.load(open("out/report.json"))["cases"] if item["trace_id"]))
')" || return 1
    "$CLI" --industry "$INDUSTRY" traces show "$trace_id" >/dev/null || return 1
    echo "OK：trace $trace_id 可定位"
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
    echo "P6 全部检查通过"
}

# ============================================================
# 调用入口：只保留一行未注释，其余按需取消注释后执行
# ============================================================

gate    # P6 Gate：评估单测 → 数据/索引 → 出报告 → 无 LangSmith → 压缩对照 → trace

# ---- 单点检查 ----
# evals        # 只跑评估单元测试
# prepare      # 只重建 appliance 数据与索引
# report       # 只跑 20 条用例并出报告
# notrace      # 只验证关闭 LangSmith 仍可跑
# compression  # 只跑压缩前后对照
# traces       # 只验证 trace 可定位
# guard        # 只跑框架边界守卫
# all          # 只跑全量 pytest
# full         # Gate + 边界 + 全量
