"""L15：homeapp 知识缺失时启动即失败，并给出重建命令。"""

from __future__ import annotations

from types import SimpleNamespace
from pathlib import Path

import pytest

from zkagent.config import IndustryPackError, require_pack_knowledge


def test_homeapp_missing_knowledge_fails_with_rebuild_command(tmp_path: Path) -> None:
    """验证：homeapp 缺知识目录时启动即失败，并在错误信息里给出重建命令（L15）。"""
    pack = SimpleNamespace(id="homeapp", knowledge_dir="knowledge/")

    with pytest.raises(IndustryPackError) as excinfo:
        require_pack_knowledge(pack, tmp_path)

    message = str(excinfo.value)
    assert "homeapp 知识目录缺失" in message
    assert "apps.build_kb_pack --kb appliance-kb --pack homeapp" in message


def test_other_pack_does_not_require_generated_knowledge(tmp_path: Path) -> None:
    """验证：手写包（appliance）不要求生成知识，只解析路径、不报错。"""
    pack = SimpleNamespace(id="appliance", knowledge_dir="knowledge/")

    path = require_pack_knowledge(pack, tmp_path)

    assert path == tmp_path / "knowledge"


def test_existing_homeapp_knowledge_passes(tmp_path: Path) -> None:
    """验证：目录存在时正常放行——拦的是「缺失」，不是「homeapp」这个 id 本身。"""
    (tmp_path / "knowledge").mkdir()
    pack = SimpleNamespace(id="homeapp", knowledge_dir="knowledge/")

    assert require_pack_knowledge(pack, tmp_path) == tmp_path / "knowledge"
