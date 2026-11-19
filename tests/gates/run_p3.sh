#!/usr/bin/env bash
# P3（RAG：知识可追溯）验证脚本
# 用法：把文件最底部想跑的那一行取消注释，然后执行：bash tests/gates/run_p3.sh
set -uo pipefail

CONDA_BASE="$(conda info --base 2>/dev/null || echo "$HOME/miniconda3")"
source "$CONDA_BASE/etc/profile.d/conda.sh"
conda activate zeke

cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit 1

PYTHON=python
CLI=zkagent
INDUSTRY=homeapp
# 索引目录可覆盖：CI 里建议指向临时目录，避免污染工作区
export ZKAGENT_INDEX_DIR="${ZKAGENT_INDEX_DIR:-data/indexes}"

gate() {
    # 注意：--industry 是**全局参数**，必须写在子命令之前
    echo "[1/4] 构建 / 增量索引（真实本地嵌入模型）"
    "$CLI" --industry "$INDUSTRY" rag index || return 1

    echo "[2/4] 二次 --dry-run 必须为 0/0/0（增量幂等）"
    "$CLI" --industry "$INDUSTRY" rag index --dry-run || return 1

    echo "[3/4] 强制全量重建"
    "$CLI" --industry "$INDUSTRY" rag index --rebuild || return 1

    echo "[4/5] RAG 测试（禁网 / 增量 / 隔离 / 可见性）"
    "$PYTHON" -m pytest -q tests/rag || return 1

    echo "[5/5] 真实模型下的「无依据不编造」（相关性闸门生效）"
    nobasis || return 1
}

nobasis() {
    # 需要真实嵌入模型：hash 兜底模式的相似度不是相关性信号，闸门默认关闭
    local out
    out="$("$CLI" chat --provider fake --task "查商品 红楼梦" --user U1 --session p3-nobasis 2>/dev/null)" || return 1
    grep -q "未检索到依据" <<<"$out" || {
        echo "失败：知识库无依据时未明确说明（输出如下）"
        echo "$out" | head -5
        return 1
    }
    echo "OK：无依据时明确说明"
}

hash() {
    # 离线快速档：不加载本地模型，走确定性 hash 兜底（指标口径会降级，报告里会标注）
    "$CLI" --industry "$INDUSTRY" rag index --hash-embedder || return 1
}

offline() {
    "$PYTHON" -m pytest -q tests/rag/test_offline.py || return 1
}

incremental() {
    "$PYTHON" -m pytest -q tests/rag/test_incremental.py || return 1
}

isolation() {
    "$PYTHON" -m pytest -q tests/rag/test_index_isolation.py || return 1
}

units() {
    "$PYTHON" -m pytest -q tests/rag || return 1
}

guard() {
    # 框架边界：llama_index 只能出现在 core/rag/llamaindex_engine.py
    "$PYTHON" -m pytest -q tests/contract/test_framework_boundary.py || return 1
}

all() {
    "$PYTHON" -m pytest -q || return 1
}

full() {
    gate || return 1
    hash || return 1
    guard || return 1
    all || return 1
    echo "P3 全部检查通过"
}

# ============================================================
# 调用入口：只保留一行未注释，其余按需取消注释后执行
# ============================================================

gate    # P3 Gate：建索引 → dry-run 幂等 → 重建 → RAG 测试

# ---- 单点检查 ----
# hash          # 用确定性 hash 兜底嵌入建索引（离线 / 快速）
# offline       # 只跑禁网与降级测试
# incremental   # 只跑增量与重建判定测试
# isolation     # 只跑行业隔离、引用与可见性过滤测试
# units         # 只跑 tests/rag 全部
# guard         # 只跑框架边界守卫
# all           # 全量 pytest
# full          # Gate + hash + guard + 全量
