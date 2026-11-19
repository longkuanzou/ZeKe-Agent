#!/usr/bin/env bash
# P1（骨架 + 模拟后端 + 工具层）验证脚本
# 用法：把文件最底部想跑的那一行取消注释，然后执行：bash tests/gates/run_p1.sh
set -uo pipefail

CONDA_BASE="$(conda info --base 2>/dev/null || echo "$HOME/miniconda3")"
source "$CONDA_BASE/etc/profile.d/conda.sh"
conda activate zeke

cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit 1

# 配置
PYTHON=python
CLI=zkagent
DB_TABLE=orders

deps() {
    "$PYTHON" -c "import fastapi, pydantic, pytest, yaml; print('deps ok')" || return 1
    "$PYTHON" -c "import zkagent; print('zkagent', zkagent.__version__)" || return 1
}

db() {
    "$CLI" db reset || return 1
    "$CLI" db seed || return 1
    "$CLI" db inspect --table "$DB_TABLE" --limit 1 || return 1
}

tools() {
    "$CLI" tools list --tag order || return 1
}

boundary() {
    "$PYTHON" -m pytest -q tests/contract/test_framework_boundary.py || return 1
}

contract() {
    "$PYTHON" -m pytest -q tests/contract || return 1
}

units() {
    "$PYTHON" -m pytest -q tests/tools || return 1
}

all() {
    "$PYTHON" -m pytest -q || return 1
}

gate() {
    deps || return 1
    db || return 1
    boundary || return 1
    tools || return 1
    units || return 1
    all || return 1
    echo "P1 Gate 全部通过"
}

# ============================================================
# 调用入口：只保留一行未注释，其余按需取消注释后执行
# ============================================================

gate              # P1 Gate 全流程：依赖自检 → 建库灌数据 → 边界守卫 → 工具列表 → 工具单测 → 全量测试

# ---- 单点检查 ----
# deps            # 只做依赖与包导入自检
# db              # 只重建业务库、灌种子数据并查看 orders 表首行
# tools           # 只按 tag 过滤查看已注册工具（验证按需注入）
# boundary        # 只跑框架边界守卫（6 条规则 + 「有牙齿」用例）
# contract        # 只跑后端契约测试（工具清单 / schema / 幂等键要求）
# units           # 只跑工具层单测（正常 / 异常 / 幂等 三类）
# all             # 只跑全量 pytest
