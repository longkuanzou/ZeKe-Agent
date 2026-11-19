#!/usr/bin/env bash
# P0（架构设计）验证脚本
# 用法：把文件最底部想跑的那一行取消注释，然后执行：bash tests/gates/run_p0.sh
set -uo pipefail

CONDA_BASE="$(conda info --base 2>/dev/null || echo "$HOME/miniconda3")"
source "$CONDA_BASE/etc/profile.d/conda.sh"
conda activate zeke

cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit 1

DESIGN=docs/DESIGN.md
ADR=docs/decisions/001-框架分工.md
MAX_ADR_LINES=20

docs() {
    local missing=0
    for f in "$DESIGN" "$ADR"; do
        if [ -f "$f" ]; then
            echo "OK   存在：$f"
        else
            echo "FAIL 缺失：$f"
            missing=1
        fi
    done
    return $missing
}

adr() {
    local lines
    lines=$(wc -l < "$ADR")
    echo "ADR 行数：$lines（上限 $MAX_ADR_LINES）"
    if [ "$lines" -le "$MAX_ADR_LINES" ]; then
        echo "OK   行数约束满足"
        return 0
    fi
    echo "FAIL ADR 超长，请精简"
    return 1
}

sections() {
    local required=(
        "## 2. 分层架构"
        "## 5. \`BusinessBackend\` 与 \`ChannelAdapter\` 的边界"
        "## 6. 三框架分工与两处接缝"
        "## 7. 核心接口与数据模型草案"
        "## 9. P1–P7 验收标准"
        "## 11. 主要风险与应对"
        "## 12. 论证：替换两个接缝文件后"
    )
    local failed=0
    for s in "${required[@]}"; do
        if grep -qF "$s" "$DESIGN"; then
            echo "OK   章节存在：$s"
        else
            echo "FAIL 缺少章节：$s"
            failed=1
        fi
    done
    return $failed
}

judge() {
    local hits=0
    for kw in "无需改动一行" "L1 必达" "替换承诺分档" "HITL 审批设计" "Recall@k"; do
        if grep -qF "$kw" "$DESIGN"; then
            echo "OK   判据/关键设计已写明：$kw"
            hits=$((hits + 1))
        else
            echo "FAIL 未找到：$kw"
        fi
    done
    [ "$hits" -eq 5 ]
}

gates() {
    local failed=0
    for phase in P1 P2 P3 P4 P5 P6 P7; do
        local block gate_count stretch_count
        block=$(awk -v pat="### $phase " '
            index($0, pat) == 1 { inside = 1 }
            inside && /^### / && index($0, pat) != 1 { exit }
            inside { print }
        ' "$DESIGN")
        if [ -z "$block" ]; then
            echo "FAIL 缺少阶段验收小节：$phase"
            failed=1
            continue
        fi
        gate_count=$(printf '%s\n' "$block" | grep -c '\*\*Gate')
        stretch_count=$(printf '%s\n' "$block" | grep -c '\*\*Stretch')
        echo "OK   $phase 验收小节：Gate=$gate_count Stretch=$stretch_count"
        if [ "$gate_count" -lt 1 ]; then
            echo "FAIL $phase 缺少 Gate 段落"
            failed=1
        fi
    done
    return $failed
}

all() {
    docs && adr && sections && judge && gates
}

# ============================================================
# 调用入口：只保留一行未注释，其余按需取消注释后执行
# ============================================================

all                 # 全量：文档存在性 + ADR 行数 + 必需章节 + 解耦判据 + 各阶段验收小节

# ---- 单点检查 ----
# docs              # 只检查两份设计文档是否存在
# adr               # 只检查 ADR 是否仍在 20 行以内
# sections          # 只检查 DESIGN 的必需章节是否齐全
# judge             # 只检查「零改动判据 / 承诺分档 / HITL / 检索指标」等关键设计是否写明
# gates             # 只检查 P1–P7 是否都写了验收小节
