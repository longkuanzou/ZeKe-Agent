"""框架边界守卫：静态扫描强制分层不被破坏（DESIGN §3.3 / §6.4）。

六条规则：

1. ``langgraph`` / ``langchain`` 只能在 ``langgraph_adapter.py`` 内被 import；
2. ``llama_index`` 只能在 ``llamaindex_engine.py`` 内被 import；
3. ``core/types.py`` 与 ``core/interfaces.py`` 的**类型注解**中不得出现框架类型；
4. ``core/`` 不得直接 import 两个接缝实现；
5. 行业词汇与金额阈值只能出现在行业包数据中（``zkagent/``、``apps/`` 内禁止硬编码）；
6. 源码 / 测试 / 文档中不得出现形如 ``sk-`` 的长密钥串。

实现方式为 **AST 静态解析 + 正则兜底**，不依赖运行期 import；每条规则都有对应的
「有牙齿」（has-teeth）用例：构造一个违规文件后，扫描器必须报出违规。
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

#: 参与扫描的顶层目录。
SCAN_ROOT_DIRS: tuple[str, ...] = ("zkagent", "apps", "tests")

#: 允许 import 编排 / 模型框架的唯一文件（相对仓库根）。
LANGGRAPH_ADAPTER = "zkagent/core/orchestrator/langgraph_adapter.py"
#: 允许 import 知识框架的唯一文件（相对仓库根）。
LLAMAINDEX_ENGINE = "zkagent/core/rag/llamaindex_engine.py"

#: 框架模块前缀（``langchain_core`` 等子包同样命中）。
LANGGRAPH_FAMILY = ("langgraph", "langchain")
LLAMAINDEX_FAMILY = ("llama_index",)

#: 禁止出现在自研契约注解中的框架类型名。
FORBIDDEN_CONTRACT_TYPES: frozenset[str] = frozenset(
    {
        "BaseMessage",
        "Document",
        "Node",
        "StructuredTool",
        "CompiledGraph",
        "ChatModel",
        "Runnable",
    }
)

#: 契约文件（注解必须保持纯净）。
CONTRACT_FILES: tuple[str, ...] = (
    "zkagent/core/types.py",
    "zkagent/core/interfaces.py",
)

#: 接缝实现模块名（禁止被 core 其他模块直接 import）。
SEAM_MODULES: frozenset[str] = frozenset({"langgraph_adapter", "llamaindex_engine"})

#: 行业包数据目录（允许出现行业词汇的唯一 ``zkagent/`` 子目录）。
INDUSTRY_PACK_DIR = "zkagent/industry_packs"

#: 硬编码行业词汇黑名单（行业包数据与 tests/ 用例数据之外一律禁止）。
FORBIDDEN_INDUSTRY_TERMS: tuple[str, ...] = (
    "空调",
    "洗衣机",
    "冰箱",
    "扫地机器人",
    "无理由",
)

#: 硬编码金额阈值模式（如「1000 元」「6999 块」；不匹配标识符内部的数字）。
FORBIDDEN_AMOUNT_PATTERN = re.compile(r"(?<![\w.])\d[\d,]*(?:\.\d+)?\s*(?:元|块钱|块)")

#: 形如 sk- 的长密钥串。
SECRET_PATTERN = re.compile(r"sk-[A-Za-z0-9_\-]{16,}")


@dataclass(frozen=True)
class Violation:
    """一条违规记录。"""

    rule: str
    path: Path
    detail: str

    def __str__(self) -> str:  # pragma: no cover - 仅用于断言失败信息
        return f"[{self.rule}] {self.path}: {self.detail}"


# --------------------------------------------------------------------------- 工具函数


def iter_python_files(roots: Iterable[Path]) -> Iterator[Path]:
    """递归收集 Python 文件（跳过缓存与虚拟环境目录）。"""
    skip = {"__pycache__", ".venv", "venv", ".git", "build", "dist", ".eggs"}
    for root in roots:
        if not root.exists():
            continue
        for path in sorted(root.rglob("*.py")):
            if any(part in skip for part in path.parts):
                continue
            yield path


def repo_python_files() -> list[Path]:
    """收集仓库中参与扫描的全部 Python 文件。"""
    return list(iter_python_files(REPO_ROOT / name for name in SCAN_ROOT_DIRS))


def relative(path: Path, base: Path = REPO_ROOT) -> str:
    """返回相对仓库根的 POSIX 路径，便于规则比对。

    若文件不在仓库内（例如「有牙齿」用例写在临时目录），则退化为：从路径中
    第一个出现的扫描根目录（``zkagent`` / ``apps`` / ``tests``）开始截取——
    这样临时源码树也能得到与真实仓库一致的逻辑路径。
    """
    resolved = path.resolve()
    try:
        return resolved.relative_to(base.resolve()).as_posix()
    except ValueError:
        parts = resolved.parts
        for index, part in enumerate(parts):
            if part in SCAN_ROOT_DIRS:
                return "/".join(parts[index:])
        return resolved.as_posix()


def parse_module(path: Path) -> ast.Module:
    """解析 Python 文件；语法错误直接抛出，避免「静默跳过」造成假绿。"""
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def imported_modules(tree: ast.Module) -> set[str]:
    """提取模块中所有 import 的完整模块名。"""
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            modules.add(node.module)
    return modules


def _startswith_any(name: str, prefixes: Iterable[str]) -> bool:
    """模块名是否属于给定框架家族（覆盖 ``langchain_core`` 这类下划线子包）。"""
    return any(
        name == prefix or name.startswith(prefix + ".") or name.startswith(prefix + "_")
        for prefix in prefixes
    )


def is_framework_importer_allowed(
    path: Path, *, family: tuple[str, ...], allowed_file: str
) -> bool:
    """判断某个文件是否有权 import 指定框架家族。

    允许两种情形：
    1. 它就是唯一的接缝实现文件；
    2. 它是**专门测试该接缝**的测试文件（``tests/**/test_*<seam>*_*.py``），
       P2/P3 需要通过真框架验证适配器行为。
    """
    rel = relative(path)
    if rel == allowed_file:
        return True
    if rel.startswith("tests/"):
        stem = path.stem
        return any(key in stem for key in ("langgraph_adapter", "llamaindex_engine"))
    return False


# --------------------------------------------------------------------------- 规则 1 / 2


def scan_framework_imports(
    files: Iterable[Path],
    *,
    family: tuple[str, ...],
    allowed_file: str,
    rule: str,
) -> list[Violation]:
    """扫描指定框架家族的越界 import。"""
    violations: list[Violation] = []
    for path in files:
        try:
            tree = parse_module(path)
        except SyntaxError as exc:  # pragma: no cover - 兜底
            violations.append(Violation(rule, path, f"语法错误：{exc}"))
            continue
        if is_framework_importer_allowed(path, family=family, allowed_file=allowed_file):
            continue
        for module in sorted(imported_modules(tree)):
            if _startswith_any(module, family):
                violations.append(
                    Violation(rule, path, f"禁止在 {allowed_file} 之外 import {module}")
                )
    return violations


def scan_langgraph_imports(files: Iterable[Path]) -> list[Violation]:
    """规则 1：``langgraph`` / ``langchain`` 只能在适配器内 import。"""
    return scan_framework_imports(
        files,
        family=LANGGRAPH_FAMILY,
        allowed_file=LANGGRAPH_ADAPTER,
        rule="only-adapter-imports-langgraph",
    )


def scan_llama_index_imports(files: Iterable[Path]) -> list[Violation]:
    """规则 2：``llama_index`` 只能在检索实现内 import。"""
    return scan_framework_imports(
        files,
        family=LLAMAINDEX_FAMILY,
        allowed_file=LLAMAINDEX_ENGINE,
        rule="only-engine-imports-llama-index",
    )


# --------------------------------------------------------------------------- 规则 3


def _annotation_names(node: ast.AST) -> set[str]:
    """收集一段注解表达式里出现的所有标识符（含字符串注解）。"""
    names: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Name):
            names.add(child.id)
        elif isinstance(child, ast.Attribute):
            names.add(child.attr)
        elif isinstance(child, ast.Constant) and isinstance(child.value, str):
            # PEP 563 字符串注解：按标识符切分
            names.update(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", child.value))
    return names


def scan_contract_annotations(files: Iterable[Path]) -> list[Violation]:
    """规则 3：契约文件的类型注解不得出现框架类型。

    只检查**注解**（含返回值与 ``ClassVar``），不检查 docstring / 注释——
    契约文档里本来就会说明「禁止出现哪些框架类型」。
    """
    rule = "contracts-have-no-framework-types"
    violations: list[Violation] = []
    for path in files:
        tree = parse_module(path)
        for node in ast.walk(tree):
            annotation: ast.AST | None = None
            if isinstance(node, ast.AnnAssign):
                annotation = node.annotation
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                annotation = node.returns
            elif isinstance(node, ast.arg):
                annotation = node.annotation
            if annotation is None:
                continue
            bad = _annotation_names(annotation) & FORBIDDEN_CONTRACT_TYPES
            if bad:
                violations.append(
                    Violation(rule, path, f"注解中出现框架类型：{', '.join(sorted(bad))}")
                )
    return violations


def scan_chat_model_handle_purity(path: Path) -> list[Violation]:
    """规则 3 的加强项：``ChatModelHandle`` 必须是纯数据（不得定义任何方法）。"""
    rule = "chat-model-handle-has-no-methods"
    violations: list[Violation] = []
    tree = parse_module(path)
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "ChatModelHandle":
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    violations.append(
                        Violation(rule, path, f"ChatModelHandle 不应定义方法：{item.name}")
                    )
    return violations


# --------------------------------------------------------------------------- 规则 4


def scan_seam_coupling(files: Iterable[Path]) -> list[Violation]:
    """规则 4：``core/`` 不得直接 import 两个接缝实现模块。"""
    rule = "core-does-not-import-seam-modules"
    violations: list[Violation] = []
    for path in files:
        rel = relative(path)
        if not rel.startswith("zkagent/core/"):
            continue
        if rel in {LANGGRAPH_ADAPTER, LLAMAINDEX_ENGINE}:
            continue
        tree = parse_module(path)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                leaf = node.module.split(".")[-1]
                if leaf in SEAM_MODULES:
                    violations.append(
                        Violation(rule, path, f"core 内禁止直接 import 接缝实现：{node.module}")
                    )
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split(".")[-1] in SEAM_MODULES:
                        violations.append(
                            Violation(rule, path, f"core 内禁止直接 import 接缝实现：{alias.name}")
                        )
    return violations


# --------------------------------------------------------------------------- 规则 5


def scan_industry_terms(files: Iterable[Path]) -> list[Violation]:
    """规则 5：行业词汇与金额阈值不得硬编码在代码中。

    白名单：``zkagent/industry_packs/``（行业包数据）与 ``tests/``（用例数据）。
    """
    rule = "no-hardcoded-industry-terms"
    violations: list[Violation] = []
    for path in files:
        rel = relative(path)
        if rel.startswith(INDUSTRY_PACK_DIR) or rel.startswith("tests/"):
            continue
        text = path.read_text(encoding="utf-8")
        for term in FORBIDDEN_INDUSTRY_TERMS:
            if term in text:
                violations.append(Violation(rule, path, f"硬编码行业词汇：{term}"))
        for match in FORBIDDEN_AMOUNT_PATTERN.finditer(text):
            violations.append(Violation(rule, path, f"硬编码金额阈值：{match.group(0)!r}"))
    return violations


# --------------------------------------------------------------------------- 规则 6


def scan_secret_literals(files: Iterable[Path]) -> list[Violation]:
    """规则 6：源码 / 测试 / 文档中不得出现形如 ``sk-`` 的长密钥串。"""
    rule = "no-secret-literals"
    violations: list[Violation] = []
    for path in files:
        text = path.read_text(encoding="utf-8")
        for match in SECRET_PATTERN.finditer(text):
            violations.append(
                Violation(rule, path, f"疑似密钥字面量：{match.group(0)[:8]}…（已截断）")
            )
    return violations


def doc_files() -> list[Path]:
    """文档文件（``.md``），用于密钥扫描。"""
    return sorted((REPO_ROOT / "docs").rglob("*.md"))


# --------------------------------------------------------------------------- 规则校验


def test_only_adapter_imports_langgraph() -> None:
    """除适配器（及其专项测试）外，全仓库不得 import langgraph / langchain。"""
    assert scan_langgraph_imports(repo_python_files()) == []


def test_only_engine_imports_llama_index() -> None:
    """除检索实现（及其专项测试）外，全仓库不得 import llama_index。"""
    assert scan_llama_index_imports(repo_python_files()) == []


def test_contracts_have_no_framework_types() -> None:
    """契约文件的注解必须纯净。"""
    contract_paths = [REPO_ROOT / name for name in CONTRACT_FILES]
    assert scan_contract_annotations(contract_paths) == []
    assert scan_chat_model_handle_purity(REPO_ROOT / "zkagent/core/types.py") == []


def test_core_does_not_import_seam_modules() -> None:
    """core 内不得直接依赖接缝实现模块。"""
    assert scan_seam_coupling(repo_python_files()) == []


def test_no_hardcoded_industry_terms() -> None:
    """代码中不得硬编码行业词汇与金额阈值（行业包与测试用例除外）。"""
    assert scan_industry_terms(repo_python_files()) == []


def test_no_secret_literals() -> None:
    """源码、测试与文档中都不得出现密钥字面量。"""
    assert scan_secret_literals([*repo_python_files(), *doc_files()]) == []


# --------------------------------------------------------------------------- 有牙齿


@pytest.mark.parametrize(
    ("rule_name", "relative_path", "content", "scanner"),
    [
        (
            "langgraph 越界 import",
            "zkagent/core/evil.py",
            "import langgraph\n",
            lambda p: scan_langgraph_imports([p]),
        ),
        (
            "langchain 子包越界 import",
            "apps/evil.py",
            "from langchain_core.messages import BaseMessage\n",
            lambda p: scan_langgraph_imports([p]),
        ),
        (
            "llama_index 越界 import",
            "zkagent/backends/evil.py",
            "from llama_index.core import VectorStoreIndex\n",
            lambda p: scan_llama_index_imports([p]),
        ),
        (
            "契约注解混入框架类型",
            "zkagent/core/types.py",
            "from typing import Any\n\n\ndef f(x: Any) -> Any: ...\n"
            "\n\nclass A:\n    field: BaseMessage\n",
            lambda p: scan_contract_annotations([p]),
        ),
        (
            "ChatModelHandle 定义了方法",
            "zkagent/core/types.py",
            "class ChatModelHandle:\n    def invoke(self):\n        return 1\n",
            lambda p: scan_chat_model_handle_purity(p),
        ),
        (
            "core 直接 import 接缝",
            "zkagent/core/bad.py",
            "from zkagent.core.orchestrator.langgraph_adapter import build_graph\n",
            lambda p: scan_seam_coupling([p]),
        ),
        (
            "硬编码行业词汇",
            "zkagent/core/bad.py",
            "PRODUCT = '空调'\n",
            lambda p: scan_industry_terms([p]),
        ),
        (
            "硬编码金额阈值",
            "zkagent/core/bad.py",
            "THRESHOLD = '1000 元以内可自动退款'\n",
            lambda p: scan_industry_terms([p]),
        ),
        (
            "密钥字面量",
            "zkagent/core/bad.py",
            # 运行时拼接，避免本测试文件自身被规则 6 判定为含密钥字面量
            'API_KEY = "' + "sk-" + "a" * 24 + '"\n',
            lambda p: scan_secret_literals([p]),
        ),
    ],
)
def test_scanners_have_teeth(
    fake_tree,
    rule_name: str,
    relative_path: str,
    content: str,
    scanner,
) -> None:
    """每条规则都必须能因构造的违规样例而失败（否则守卫形同虚设）。"""
    path = fake_tree(relative_path, content)
    violations = scanner(path)
    assert violations, f"{rule_name}：扫描器未报出违规，说明该规则没有牙齿"


def test_seam_file_may_import_frameworks(repo_root: Path) -> None:
    """正向对照：接缝文件存在、确实 import 了框架，且**只有它**被允许。"""
    adapter = repo_root / LANGGRAPH_ADAPTER
    assert adapter.is_file(), f"接缝文件应当存在（P2 起）：{LANGGRAPH_ADAPTER}"

    imported = imported_modules(parse_module(adapter))
    assert any(
        _startswith_any(module, LANGGRAPH_FAMILY) for module in imported
    ), "接缝文件应当真的 import 了框架，否则白名单毫无意义"

    assert scan_langgraph_imports([adapter]) == []
    assert is_framework_importer_allowed(
        adapter, family=LANGGRAPH_FAMILY, allowed_file=LANGGRAPH_ADAPTER
    )


def test_conftest_and_plain_tests_do_not_import_frameworks() -> None:
    """除接缝专项测试外，其余测试文件不得 import 框架。"""
    files = repo_python_files()
    offenders = [
        relative(path)
        for path in files
        if relative(path).startswith("tests/")
        and not is_framework_importer_allowed(
            path, family=LANGGRAPH_FAMILY, allowed_file=LANGGRAPH_ADAPTER
        )
        and any(
            _startswith_any(module, LANGGRAPH_FAMILY)
            for module in imported_modules(parse_module(path))
        )
    ]
    assert offenders == [], f"以下测试文件越界 import 了编排框架：{offenders}"


def test_scanners_are_quiet_on_clean_code(fake_tree) -> None:
    """对照实验：同一批扫描器面对干净代码必须零违规（避免「一律报错」的假守卫）。"""
    path = fake_tree(
        "zkagent/core/clean.py",
        "import json\n\n\ndef f(x: int) -> str:\n    return json.dumps(x)\n",
    )
    assert scan_langgraph_imports([path]) == []
    assert scan_llama_index_imports([path]) == []
    assert scan_contract_annotations([path]) == []
    assert scan_seam_coupling([path]) == []
    assert scan_industry_terms([path]) == []
    assert scan_secret_literals([path]) == []
