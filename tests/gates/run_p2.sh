#!/usr/bin/env bash
# P2（最小可用 Agent）验证脚本
# 用法：把文件最底部想跑的那一行取消注释，然后执行：bash tests/gates/run_p2.sh
set -uo pipefail

CONDA_BASE="$(conda info --base 2>/dev/null || echo "$HOME/miniconda3")"
source "$CONDA_BASE/etc/profile.d/conda.sh"
conda activate zeke

cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit 1

# provider key 映射：本机若用 DS_API_KEY 存 DeepSeek 密钥，这里统一成适配器读取的名字
if [ -n "${DS_API_KEY:-}" ] && [ -z "${DEEPSEEK_API_KEY:-}" ]; then
    export DEEPSEEK_API_KEY="$DS_API_KEY"
fi

# 配置
PYTHON=python
CLI=zkagent
PROVIDER=fake
PROVIDER_REAL=deepseek
USER_ID=U1
USER_OTHER=U2
SESSION=p2check

prepare() {
    "$CLI" db reset >/dev/null || return 1
    "$CLI" db seed >/dev/null || return 1
}

gate() {
    prepare || return 1
    zone() {
        "$CLI" chat --provider "$PROVIDER" --task "查订单 A1001" --user "$USER_ID" --session "$SESSION-order"
    }
    zlog() {
        "$CLI" chat --provider "$PROVIDER" --task "查物流 A1001" --user "$USER_ID" --session "$SESSION-logi"
    }
    ztool() {
        "$CLI" tools list --tag order
    }
    zone || return 1
    zlog || return 1
    "$PYTHON" -m pytest -q tests/orchestrator tests/delivery/test_chat_e2e.py || return 1
    ztool || return 1
}

honest() {
    prepare || return 1
    "$CLI" chat --provider "$PROVIDER" --task "查订单 A1001" --user "$USER_ID" --session "$SESSION-ok" || return 1
    "$CLI" chat --provider "$PROVIDER" --task "查订单 A1001" --user "$USER_OTHER" --session "$SESSION-no" || return 1
}

real() {
    prepare || return 1
    "$CLI" chat --provider "$PROVIDER_REAL" --task "查一下订单 A1001 的物流" --user "$USER_ID" --session "$SESSION-real" || return 1
}

nokey() {
    prepare || return 1
    env -u DEEPSEEK_API_KEY "$CLI" chat --provider auto --task "查订单 A1001" --user "$USER_ID" --session "$SESSION-nokey" || return 1
}

context() {
    "$PYTHON" -m pytest -q tests/context/test_context_engine.py tests/delivery || return 1
}

units() {
    "$PYTHON" -m pytest -q tests/orchestrator tests/delivery/test_chat_e2e.py tests/context/test_context_engine.py tests/delivery || return 1
}

all() {
    "$PYTHON" -m pytest -q || return 1
}

full() {
    gate || return 1
    honest || return 1
    nokey || return 1
    all || return 1
    echo "P2 全部检查通过（未包含真实模型调用）"
}

# ============================================================
# 调用入口：只保留一行未注释，其余按需取消注释后执行
# ============================================================

gate              # P2 Gate 全流程：查订单 / 查物流 / 编排与端到端测试 / 按 tag 注入子集

# ---- 单点检查 ----
# honest          # 同一任务分别用本人与他人身份：回答必须随真实工具结果变化（失败不得说成功）
# nokey           # 无密钥时 auto 落到 Fake：验证「无密钥也能跑通」
# real            # 用真实 DeepSeek 跑一条物流查询（需要 DEEPSEEK_API_KEY，耗时较长）
# context         # 只跑上下文引擎与 CLI 渠道的测试
# units           # 只跑 P2 新增的编排 / 端到端 / 上下文 / 渠道测试
# all             # 只跑全量 pytest
