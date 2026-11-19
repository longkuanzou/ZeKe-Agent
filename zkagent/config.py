"""组合根：读取环境变量、解析路径、定位行业包。

本模块是**唯一允许**选择具体实现（后端、检索器、编排器、渠道）的地方；
Core 业务模块只依赖 ``zkagent.core.interfaces`` 中的抽象。

配置来源优先级：显式参数 > 环境变量 > ``.env`` 文件 > 默认值。
密钥只从环境变量读取，**不得**写入代码、日志或版本库。
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Annotated, Any

import yaml
from pydantic import Field, ValidationError, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

import zkagent
from zkagent.backends.http import BackendFactsProvider, HttpBackend
from zkagent.backends.mock.approvals import SqliteApprovalStore
from zkagent.backends.mock.backend import MockBackend
from zkagent.backends.mock.db import Database, SqliteIdempotencyStore
from zkagent.backends.mock.facts import MockFactsProvider
from zkagent.channels.cli import CliChannel
from zkagent.core.context.engine import ContextEngine
from zkagent.core.interfaces import AgentRunner, BusinessBackend, ChannelAdapter, Retriever
from zkagent.core.policy.facts import FactsProvider
from zkagent.core.rag.knowledge_tool import KnowledgeSearchTool
from zkagent.core.tools.composite import CompositeBackend
from zkagent.core.llm import (
    FakeProvider,
    ProviderConfigError,
    RegistryProvider,
    auto_select_provider,
    provider_catalog,
)
from zkagent.core.rag.embedder import (
    DEFAULT_EMBED_MODEL_ID,
    LOCAL_MODE,
    EmbedderBundle,
    build_embedder,
)
from zkagent.core.rag.errors import RagError
from zkagent.core.rag.manifest import scan_documents
from zkagent.core.rag.metadata import JOURNEY_LABELS, load_knowledge_meta
from zkagent.core.policy import RulePolicyEngine, load_rule_set
from zkagent.core.rag.metrics import evaluate_set, load_eval_set
from zkagent.core.rag.rerank import DEFAULT_RERANKER_MODEL_ID, CrossEncoderReranker
from zkagent.core.rag.llamaindex_engine import (
    DEFAULT_CHUNK_OVERLAP,
    DEFAULT_CHUNK_SIZE,
    LlamaIndexRetriever,
)
from zkagent.core.observability.tracer import Tracer
from zkagent.core.orchestrator.langgraph_adapter import build_runner, chat_once, open_checkpointer
from zkagent.core.tools.executor import ToolExecutor
from zkagent.core.tools.registry import (
    ToolRegistry,
    apply_industry_overrides,
    missing_enabled_tools,
)
from zkagent.core.types import (
    ChatModelHandle,
    IndustryPackConfig,
    IndustryToolsConfig,
    PolicyRule,
    PolicyRulesDocument,
    ToolContext,
    ToolSpec,
)

__all__ = [
    "Settings",
    "AppPaths",
    "IndustryPackError",
    "load_settings",
    "resolve_paths",
    "default_industry_packs_dir",
    "load_industry_pack",
    "load_tools_config",
    "load_policy_rules",
    "pack_file",
    "settings_key_env",
    "provider_catalog_with_keys",
]


class IndustryPackError(ValueError):
    """行业包缺失或格式非法——**必须报错**，不得静默降级为默认值。"""


def default_industry_packs_dir() -> Path:
    """返回随包分发的行业包根目录（``zkagent/industry_packs``）。"""
    return Path(zkagent.__file__).resolve().parent / "industry_packs"


class Settings(BaseSettings):
    """运行期配置。字段名与 DESIGN §7.5 的行业包约定保持一致。"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        # 允许用字段名（而非仅别名）构造，便于测试注入
        populate_by_name=True,
    )

    #: 默认行业包 id（``industry_packs/<id>/``）
    industry_id: str = Field(default="appliance", validation_alias="ZKAGENT_INDUSTRY")
    #: 业务后端实现：``mock``（本地 SQLite）或 ``http``（legacy 业务服务）
    backend: str = Field(default="mock", validation_alias="ZKAGENT_BACKEND")
    #: HTTP 后端地址（仅在 backend=http 时使用）
    http_backend_url: str = Field(
        default="http://localhost:8100", validation_alias="ZKAGENT_HTTP_BACKEND_URL"
    )
    #: 项目根目录（默认取本文件的上两级目录，避免依赖当前工作目录）
    project_root: Path = Field(
        default_factory=lambda: Path(__file__).resolve().parents[1],
        validation_alias="ZKAGENT_PROJECT_ROOT",
    )
    #: 业务库路径（相对路径按 ``project_root`` 解析）
    db_path: Path = Field(default=Path("data/business.db"), validation_alias="ZKAGENT_DB_PATH")
    #: 编排检查点库路径（P2 起使用；**不用** MemorySaver，保证进程重启可续跑）
    checkpoints_db_path: Path = Field(
        default=Path("data/checkpoints.db"), validation_alias="ZKAGENT_CHECKPOINTS_DB"
    )
    #: trace 落盘目录（相对路径按 ``project_root`` 解析）
    trace_dir: Path = Field(default=Path("data/traces"), validation_alias="ZKAGENT_TRACE_DIR")
    #: 索引根目录：每个行业包一个子目录（``data/indexes/<industry_id>/``）
    index_root: Path = Field(default=Path("data/indexes"), validation_alias="ZKAGENT_INDEX_DIR")
    #: 嵌入实现：``auto`` 优先本地模型、失败降级 hash；``local`` 必须用本地模型；``hash`` 只用兜底
    embedder_mode: str = Field(default="auto", validation_alias="ZKAGENT_EMBEDDER")
    #: 本地嵌入模型 id（P3 起使用；默认中文小模型，CPU 可跑）
    embed_model_id: str = Field(
        default=DEFAULT_EMBED_MODEL_ID, validation_alias="ZKAGENT_EMBED_MODEL_ID"
    )
    #: 分块参数（写入 manifest，改了就要求全量重建）
    rag_chunk_size: int = Field(
        default=DEFAULT_CHUNK_SIZE, validation_alias="ZKAGENT_RAG_CHUNK_SIZE"
    )
    rag_chunk_overlap: int = Field(
        default=DEFAULT_CHUNK_OVERLAP, validation_alias="ZKAGENT_RAG_CHUNK_OVERLAP"
    )
    #: 检索返回条数
    rag_top_k: int = Field(default=5, validation_alias="ZKAGENT_RAG_TOP_K")
    #: 相关性闸门（稠密相似度下限）：留空表示按嵌入模式自动标定
    #: （真实模型 0.45；hash 兜底 0 = 关闭，因为 hash 相似度不是相关性信号）
    rag_min_similarity: float | None = Field(
        default=None, validation_alias="ZKAGENT_RAG_MIN_SIMILARITY"
    )
    #: 是否启用 cross-encoder 精排（默认关：模型约 1.1GB 且每个候选都要跑一次前向）
    rag_rerank: bool = Field(default=False, validation_alias="ZKAGENT_RAG_RERANK")
    #: 精排模型 id
    rag_reranker_model_id: str = Field(
        default=DEFAULT_RERANKER_MODEL_ID, validation_alias="ZKAGENT_RAG_RERANKER_MODEL_ID"
    )
    #: 精排最多重排多少条候选（上限越小越快；L8）
    rag_rerank_max_candidates: int = Field(
        default=20, validation_alias="ZKAGENT_RAG_RERANK_MAX_CANDIDATES"
    )
    #: 模型供应商：``auto`` 表示按「哪个密钥就位」自动选择（顺序见 llm.AUTO_PRIORITY）
    provider: str = Field(default="auto", validation_alias="ZKAGENT_PROVIDER")
    #: 模型 id；留空表示使用所选供应商的默认模型（未定义默认值的供应商必须显式指定）
    model_id: str | None = Field(default=None, validation_alias="ZKAGENT_MODEL_ID")
    #: 模型端点覆盖：自建网关 / 本地 vLLM / LM Studio / 任意 OpenAI 兼容端点
    base_url: str | None = Field(default=None, validation_alias="ZKAGENT_BASE_URL")
    #: 编排最大步数
    max_steps: int = Field(default=8, validation_alias="ZKAGENT_MAX_STEPS")
    #: 调用方权限（开发期发放路径）：`kb:internal,refund:approve`。
    #: **不是生产鉴权**：没有登录体系，只用于让受限文档与审批权限可被端到端验证。
    permissions: Annotated[list[str], NoDecode] = Field(
        default_factory=list, validation_alias="ZKAGENT_PERMISSIONS"
    )
    #: 审批人权限映射：`alice=refund:approve|kb:internal,bob=...`
    hitl_approver_permissions: str = Field(
        default="", validation_alias="HITL_APPROVER_PERMISSIONS"
    )
    #: 是否启用 LLM 摘要（仅非 fake provider；失败自动回退确定性摘要）
    context_llm_summary: bool = Field(
        default=False, validation_alias="ZKAGENT_CONTEXT_LLM_SUMMARY"
    )

    @field_validator("backend")
    @classmethod
    def _validate_backend(cls, value: str) -> str:
        """校验并归一化业务后端类型。

        :param value: 配置值（``mock`` 或 ``http``，允许大小写与空白）。
        :returns: 小写、去空白的后端名。
        :raises ValueError: 取值不在允许集合内。
        """
        normalized = str(value or "").strip().lower()
        if normalized not in {"mock", "http"}:
            raise ValueError("backend 只能是 mock 或 http")
        return normalized

    @field_validator("permissions", mode="before")
    @classmethod
    def _split_permissions(cls, value: Any) -> Any:
        """环境变量是字符串：按逗号/分号/竖线拆分，避免只支持 JSON 数组。"""
        if isinstance(value, str):
            return [item.strip() for item in re.split(r"[,;|]", value) if item.strip()]
        return value

    #: 各供应商密钥（P2 起使用）。字段名对应环境变量名，声明齐全是为了让 ``.env``
    #: 对所有供应商都生效；**未配置密钥的供应商只是不可选，不影响无密钥跑通**。
    deepseek_api_key: str | None = Field(default=None, validation_alias="DEEPSEEK_API_KEY")
    openai_api_key: str | None = Field(default=None, validation_alias="OPENAI_API_KEY")
    dashscope_api_key: str | None = Field(default=None, validation_alias="DASHSCOPE_API_KEY")
    zhipu_api_key: str | None = Field(default=None, validation_alias="ZHIPUAI_API_KEY")
    anthropic_api_key: str | None = Field(default=None, validation_alias="ANTHROPIC_API_KEY")
    #: HITL 审批人配置，形如 ``alice:<token>,bob:<token>``（P4 起使用）
    hitl_approvers: str | None = Field(default=None, validation_alias="HITL_APPROVERS")


class AppPaths:
    """解析后的绝对路径集合（只读视图，便于传递与测试注入）。"""

    def __init__(
        self,
        project_root: Path,
        db_path: Path,
        trace_dir: Path,
        industry_pack_dir: Path,
        checkpoints_db_path: Path | None = None,
        index_root: Path | None = None,
    ) -> None:
        self.project_root = project_root
        self.db_path = db_path
        self.trace_dir = trace_dir
        self.industry_pack_dir = industry_pack_dir
        self.checkpoints_db_path = checkpoints_db_path or (db_path.parent / "checkpoints.db")
        self.index_root = index_root or (db_path.parent / "indexes")

    def index_dir_for(self, industry_id: str) -> Path:
        """某行业包的索引目录（**每行业一套，互不共享**）。"""
        return self.index_root / industry_id

    def __repr__(self) -> str:  # pragma: no cover - 仅用于调试输出
        return (
            f"AppPaths(project_root={self.project_root!r}, db_path={self.db_path!r}, "
            f"checkpoints_db_path={self.checkpoints_db_path!r}, trace_dir={self.trace_dir!r}, "
            f"index_root={self.index_root!r}, industry_pack_dir={self.industry_pack_dir!r})"
        )


def _resolve(base: Path, path: Path) -> Path:
    """相对路径按 ``base`` 解析为绝对路径；绝对路径原样返回。"""
    return path if path.is_absolute() else (base / path).resolve()


def load_settings(**overrides: object) -> Settings:
    """加载配置；``overrides`` 用于测试注入（优先级最高）。"""
    return Settings(**overrides)  # type: ignore[arg-type]


def settings_key_env(settings: Settings) -> dict[str, str]:
    """收集配置对象中声明为 ``*_API_KEY`` 的字段，返回 ``环境变量名 -> 密钥值``。

    存在的理由：``pydantic-settings`` 会把 ``.env`` 读进配置对象，但**不会**写进
    ``os.environ``；而接缝层（唯一读取密钥的地方）是按环境变量名取值的。
    因此这里只做「名字到值」的搬运，用途仅限两处：

    1. 判断「哪个供应商的密钥已就位」（``auto`` 选择）；
    2. 装配时把 ``.env`` 里的值回填进 ``os.environ`` 供接缝读取（``setdefault``）。

    任何情况下都**不得**写入句柄、日志或 trace。
    """
    found: dict[str, str] = {}
    for field_name, field in type(settings).model_fields.items():
        alias = field.validation_alias
        if isinstance(alias, str) and alias.endswith("_API_KEY"):
            value = getattr(settings, field_name, None)
            if value:
                found[alias] = str(value)
    return found


def provider_catalog_with_keys(settings: Settings | None = None) -> list[dict[str, Any]]:
    """登记表 + 「本机密钥是否就位」的视图（**不含密钥值**），供 CLI 展示与排障。"""
    settings = settings or load_settings()
    return provider_catalog({**os.environ, **settings_key_env(settings)})


def build_embedder_bundle(settings: Settings, *, force_hash: bool = False) -> EmbedderBundle:
    """按配置构造嵌入实现（模块级函数，供惰性工厂在容器之后使用）。

    - ``auto``（默认）：优先本地模型，失败**显式降级**为确定性 hash 并说明原因；
    - ``local``：必须用本地模型，失败直接抛出——**不静默降级**，否则指标口径会被悄悄改变；
    - ``hash``：只用兜底实现（离线测试与快速 CI）。
    """
    mode = (settings.embedder_mode or "auto").strip().lower()

    if force_hash or mode == "hash":
        return build_embedder(prefer_local=False)

    if mode == "local":
        from zkagent.core.rag.embedder import SentenceTransformerEmbedder

        return EmbedderBundle(
            embedder=SentenceTransformerEmbedder(settings.embed_model_id), mode=LOCAL_MODE
        )

    return build_embedder(settings.embed_model_id)


#: 默认行业包：沿用历史路径，避免已有的 ``data/business.db`` 突然「消失」。
DEFAULT_INDUSTRY_ID = "appliance"
DEFAULT_DB_PATH = Path("data/business.db")
DEFAULT_CHECKPOINTS_DB_PATH = Path("data/checkpoints.db")


def _runtime_db_paths(settings: Settings, project_root: Path) -> tuple[Path, Path]:
    """按行业包隔离业务库与检查点库。

    规则（显式配置优先）：

    1. ``db_path`` 不等于默认值 → 完全按配置走，不做任何推导；
    2. 默认包 ``appliance`` → 沿用 ``data/business.db`` / ``data/checkpoints.db``（不迁移既有数据）；
    3. 其他包 → ``data/business-<id>.db`` / ``data/checkpoints-<id>.db``。

    **为什么必须隔离**：共库时 ``list_orders`` 会把另一个包的种子订单一起列出来
    （``tests/tools/test_mock_backend.py`` 断言 U1 恰好 5 单）；检查点库共用还会让
    同 ``thread_id`` 的会话跨包串味（接着另一个行业包的对话继续聊）。
    """
    if settings.db_path != DEFAULT_DB_PATH:
        return (
            _resolve(project_root, settings.db_path),
            _resolve(project_root, settings.checkpoints_db_path),
        )
    if settings.industry_id == DEFAULT_INDUSTRY_ID:
        return (
            _resolve(project_root, DEFAULT_DB_PATH),
            _resolve(project_root, DEFAULT_CHECKPOINTS_DB_PATH),
        )
    return (
        _resolve(project_root, DEFAULT_DB_PATH.with_name(f"business-{settings.industry_id}.db")),
        _resolve(
            project_root,
            DEFAULT_CHECKPOINTS_DB_PATH.with_name(f"checkpoints-{settings.industry_id}.db"),
        ),
    )


def resolve_paths(settings: Settings, *, create: bool = True) -> AppPaths:
    """把配置解析为绝对路径，并按需创建运行期目录。

    :param create: 为 ``True`` 时创建 ``data/`` 与 trace 目录。
    """
    project_root = settings.project_root.resolve()
    db_path, checkpoints_db_path = _runtime_db_paths(settings, project_root)
    paths = AppPaths(
        project_root=project_root,
        db_path=db_path,
        trace_dir=_resolve(project_root, settings.trace_dir),
        industry_pack_dir=default_industry_packs_dir() / settings.industry_id,
        checkpoints_db_path=checkpoints_db_path,
        index_root=_resolve(project_root, settings.index_root),
    )
    if create:
        paths.db_path.parent.mkdir(parents=True, exist_ok=True)
        paths.trace_dir.mkdir(parents=True, exist_ok=True)
    return paths


# --------------------------------------------------------------------- 行业包加载


def _read_yaml(path: Path) -> dict[str, Any]:
    """读取 YAML 文件并确保顶层是映射；文件缺失或格式错误一律抛 :class:`IndustryPackError`。"""
    if not path.exists():
        raise IndustryPackError(f"行业包文件不存在：{path}")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise IndustryPackError(f"行业包 YAML 解析失败：{path}：{exc}") from exc
    if not isinstance(data, dict):
        raise IndustryPackError(f"行业包 YAML 顶层必须是映射：{path}")
    return data


def load_industry_pack(pack_dir: Path | str) -> IndustryPackConfig:
    """加载 ``pack.yaml`` 并做 schema 校验。"""
    pack_dir = Path(pack_dir)
    try:
        return IndustryPackConfig.model_validate(_read_yaml(pack_dir / "pack.yaml"))
    except ValidationError as exc:
        raise IndustryPackError(f"pack.yaml 校验失败：{exc}") from exc


def pack_file(pack_dir: Path | str, relative: str) -> Path:
    """把行业包内相对路径解析为绝对路径。"""
    return Path(pack_dir) / relative


def require_pack_knowledge(pack: IndustryPackConfig, pack_dir: Path | str) -> Path:
    """校验行业包知识目录；缺失时给出可执行的重建命令（L15）。

    ``homeapp`` 的知识由 ``appliance-kb`` 打包生成，不入版本库。直接 clone 仓库
    的新环境没有这份目录，必须在启动时就明确失败，而不是等检索时抛「索引为空」。
    """
    path = pack_file(pack_dir, pack.knowledge_dir)
    if pack.id == "homeapp" and not path.is_dir():
        raise IndustryPackError(
            "homeapp 知识目录缺失："
            f"{path}\n"
            "请先重建（需要 appliance-kb）："
            "python -m apps.build_kb_pack --kb appliance-kb --pack homeapp"
        )
    return path


def load_tools_config(config: IndustryPackConfig, pack_dir: Path | str) -> IndustryToolsConfig:
    """加载 ``tools.yaml``（工具启用清单、tag 补充、元数据覆盖与注入策略）。"""
    try:
        return IndustryToolsConfig.model_validate(
            _read_yaml(pack_file(pack_dir, config.tools_file))
        )
    except ValidationError as exc:
        raise IndustryPackError(f"tools.yaml 校验失败：{exc}") from exc


def load_policy_rules(config: IndustryPackConfig, pack_dir: Path | str) -> list[PolicyRule]:
    """加载 ``policies/rules.yaml``，并按 ``priority`` 升序返回（数值小者优先）。

    本阶段只做加载与校验；**判定逻辑**由 ``PolicyEngine`` 在 P4 实现。
    """
    try:
        document = PolicyRulesDocument.model_validate(
            _read_yaml(pack_file(pack_dir, config.policies_file))
        )
    except ValidationError as exc:
        raise IndustryPackError(f"rules.yaml 校验失败：{exc}") from exc
    return sorted(document.rules, key=lambda rule: rule.priority)


def load_system_prompt(config: IndustryPackConfig, pack_dir: Path | str) -> str:
    """读取行业包的系统提示（P2 起被上下文引擎消费）。"""
    path = pack_file(pack_dir, config.prompts.system)
    if not path.exists():
        raise IndustryPackError(f"行业包缺少系统提示文件：{path}")
    return path.read_text(encoding="utf-8")


def load_fewshots(config: IndustryPackConfig, pack_dir: Path | str) -> list[dict[str, Any]]:
    """读取少样本示例；未声明或文件缺失时返回空列表（不是错误）。"""
    if not config.prompts.fewshots:
        return []
    path = pack_file(pack_dir, config.prompts.fewshots)
    if not path.exists():
        return []
    examples = _read_yaml(path).get("examples") or []
    if not isinstance(examples, list):
        raise IndustryPackError(f"fewshots.yaml 的 examples 必须是列表：{path}")
    return [dict(item) for item in examples if isinstance(item, dict)]


def load_demo_scenarios(config: IndustryPackConfig, pack_dir: Path | str) -> list[dict[str, Any]]:
    """读取「演示脚本」（仅服务于无密钥离线验收）；未声明或缺失时返回空列表。"""
    if not config.demo_scenarios:
        return []
    path = pack_file(pack_dir, config.demo_scenarios)
    if not path.exists():
        return []
    scenarios = _read_yaml(path).get("scenarios") or []
    if not isinstance(scenarios, list):
        raise IndustryPackError(f"demo_scenarios.yaml 的 scenarios 必须是列表：{path}")
    return [dict(item) for item in scenarios if isinstance(item, dict)]


def find_demo_script(scenarios: list[dict[str, Any]], task: str) -> list[dict[str, Any]] | None:
    """按演示任务文本查找确定性脚本；找不到返回 ``None``（由调用方显式报错）。"""
    normalized = task.strip()
    for scenario in scenarios:
        if str(scenario.get("task", "")).strip() == normalized:
            script = scenario.get("script") or []
            if not isinstance(script, list) or not script:
                raise IndustryPackError(f"演示场景 {scenario.get('id')!r} 的 script 为空或格式非法")
            return [dict(step) for step in script]
    return None


def demo_task_list(scenarios: list[dict[str, Any]]) -> list[str]:
    """列出可用的演示任务文本（用于错误提示，避免用户猜）。"""
    return [str(scenario.get("task", "")) for scenario in scenarios if scenario.get("task")]


# --------------------------------------------------------------------- 组合根装配


class Container:
    """组合根装配产物：后端 / 工具 / 上下文 / 渠道 + 编排器工厂。

    「换实现只改一行」就发生在本类：替换后端、检索器或编排器时只需要改这里的装配，
    Core 业务模块不受影响。
    """

    def __init__(
        self,
        *,
        settings: Settings,
        paths: AppPaths,
        pack: IndustryPackConfig,
        tools_config: IndustryToolsConfig,
        specs: list[ToolSpec],
        registry: ToolRegistry,
        backend: BusinessBackend,
        executor: ToolExecutor,
        tool_backend: BusinessBackend | None = None,
        tracer: Tracer,
        context_engine: ContextEngine,
        channel: ChannelAdapter,
        scenarios: list[dict[str, Any]],
        tools_config_error: list[str],
        policy_engine: RulePolicyEngine,
        facts_provider: FactsProvider,
        approval_store: SqliteApprovalStore,
        business_database: Database | None = None,
    ) -> None:
        self.settings = settings
        self.paths = paths
        self.pack = pack
        self.tools_config = tools_config
        self.specs = specs
        self.registry = registry
        self.backend = backend
        #: 仅 MockBackend 暴露本地业务库；HTTP 模式为 None（评估隔离等能力需显式判断）。
        self.business_database = business_database
        #: 执行器实际使用的后端：业务后端 + RAG 工具（``knowledge_search``）
        self.tool_backend: BusinessBackend = tool_backend or backend
        self.executor = executor
        self.tracer = tracer
        self.context_engine = context_engine
        self.channel = channel
        self.scenarios = scenarios
        self.missing_enabled_tools = tools_config_error
        #: 规则引擎：判定只由代码得出，模型无权越过
        self.policy_engine = policy_engine
        self.facts_provider = facts_provider
        #: 审批仓储：CLI 与编排图共用同一份，避免「图里挂起、CLI 查不到」
        self.approval_store = approval_store

    # ------------------------------------------------------------------ 装配

    @classmethod
    def build(
        cls, settings: Settings | None = None, *, channel: ChannelAdapter | None = None
    ) -> "Container":
        """按配置装配全链路（后端 → 工具 → 执行器 → 上下文 → 渠道）。"""
        settings = settings or load_settings()
        # .env 中配置的密钥回填进进程环境（setdefault，不覆盖已有值），供接缝层按名读取
        for env_name, value in settings_key_env(settings).items():
            os.environ.setdefault(env_name, value)
        paths = resolve_paths(settings)
        pack = load_industry_pack(paths.industry_pack_dir)
        tools_config = load_tools_config(pack, paths.industry_pack_dir)

        business_database: Database | None
        if settings.backend == "http":
            backend: BusinessBackend = HttpBackend(settings.http_backend_url)
            business_database = None
        else:
            mock_backend = MockBackend(Database(paths.db_path))
            backend = mock_backend
            business_database = mock_backend.database

        # ---- 知识侧工具：惰性构建，只有真正检索时才加载嵌入模型 / 读索引 ----
        # （否则 `zkagent tools list`、`zkagent db seed` 这类命令都要白等几秒模型加载）
        knowledge_dir = require_pack_knowledge(pack, paths.industry_pack_dir)
        index_dir = paths.index_dir_for(pack.id)

        def _rag_retriever() -> Retriever:
            """惰性构建检索器（第一次真正检索时才加载嵌入模型与索引）。

            这样 ``tools list`` / ``db seed`` 这类命令不必白等几秒模型加载。

            :returns: 绑定当前行业包知识目录与索引目录的 :class:`Retriever`。
            """
            bundle = build_embedder_bundle(settings)
            return LlamaIndexRetriever(
                industry_id=pack.id,
                knowledge_dir=knowledge_dir,
                index_dir=index_dir,
                embedder=bundle.embedder,
                embedding_mode=bundle.mode,
                chunk_size=settings.rag_chunk_size,
                chunk_overlap=settings.rag_chunk_overlap,
                top_k=settings.rag_top_k,
                dense_min_similarity=settings.rag_min_similarity,
            )

        tool_backend = CompositeBackend(
            backend, [KnowledgeSearchTool(_rag_retriever, default_top_k=settings.rag_top_k)]
        )

        declared = tool_backend.list_tools()
        missing = missing_enabled_tools(declared, tools_config)
        if missing:
            # 配置声明启用但没有任何来源提供：显式失败，不得静默忽略
            raise IndustryPackError(f"行业包声明启用的工具未提供：{missing}")

        specs = apply_industry_overrides(declared, tools_config)
        registry = ToolRegistry()
        registry.register_many(specs)

        # ---- 规则引擎：只加载数据，不把行业阈值写进代码 ----
        # known_tools 用**已启用**清单交叉校验：规则引用未启用的工具等于永远不生效。
        rule_set = load_rule_set(
            pack_file(paths.industry_pack_dir, pack.policies_file),
            known_tools=set(registry.names()),
        )
        policy_engine = RulePolicyEngine(rule_set)
        approval_database = business_database or Database(paths.db_path)
        if settings.backend == "http":
            facts_provider: FactsProvider = BackendFactsProvider(backend)
        else:
            facts_provider = MockFactsProvider(approval_database)
        approval_store = SqliteApprovalStore(approval_database)

        tracer = Tracer(paths.trace_dir)
        executor = ToolExecutor(
            tool_backend,
            SqliteIdempotencyStore(Database(paths.db_path)),
            tracer,
        )
        context_engine = ContextEngine(
            system_prompt=load_system_prompt(pack, paths.industry_pack_dir),
            fewshots=load_fewshots(pack, paths.industry_pack_dir),
        )
        container = cls(
            settings=settings,
            paths=paths,
            pack=pack,
            tools_config=tools_config,
            specs=specs,
            registry=registry,
            backend=backend,
            tool_backend=tool_backend,
            executor=executor,
            tracer=tracer,
            context_engine=context_engine,
            channel=channel or CliChannel(session_id="cli-session", user_id=None),
            scenarios=load_demo_scenarios(pack, paths.industry_pack_dir),
            tools_config_error=missing,
            policy_engine=policy_engine,
            facts_provider=facts_provider,
            approval_store=approval_store,
            business_database=business_database,
        )
        container._install_summarizer()
        return container

    def _install_summarizer(self) -> None:
        """按 ``ZKAGENT_CONTEXT_LLM_SUMMARY`` 注入 LLM 摘要；fake / 无密钥时跳过。"""
        if not self.settings.context_llm_summary:
            return
        try:
            handle = self.resolve_model_handle(provider=self.settings.provider)
        except (ProviderConfigError, IndustryPackError):
            return
        if handle.provider_id == "fake":
            return

        def _summarize(text: str) -> str:
            """用真实模型生成历史摘要（可选增强，失败时上层回退确定性摘要）。

            :param text: 待压缩的历史片段。
            :returns: 单行摘要文本；模型返回空时返回空串，由调用方判定回退。
            """
            result = chat_once(
                handle,
                [
                    {"role": "system", "content": "把客服对话摘要压缩成一行，保留订单号与关键结论。"},
                    {"role": "user", "content": text},
                ],
            )
            return str(result.get("text") or "").strip()

        self.context_engine._summarizer = _summarize  # noqa: SLF001 - 组合根负责装配

    # ------------------------------------------------------------------ 会话级编排器

    def make_runner(
        self,
        *,
        user_id: str | None = None,
        session_id: str = "cli-session",
        provider: str | None = None,
        task: str | None = None,
        model: str | None = None,
        base_url: str | None = None,
        tool_tags: list[str] | None = None,
        max_steps: int | None = None,
        trace_id: str | None = None,
        script: list[dict[str, Any]] | None = None,
        permissions: list[str] | None = None,
        intent: str | None = None,
        verified: bool = False,
    ) -> tuple[AgentRunner, ToolContext]:
        """按会话构建编排器与执行上下文。

        注意：``AgentRunner`` 与 ``ToolContext`` 是**会话级**的（工具回调内已绑定身份），
        多会话并发需要各自构建；按会话缓存属 P7 的优化项。
        """
        handle = self.resolve_model_handle(
            provider=provider, task=task, model=model, base_url=base_url, script=script
        )
        steps = max_steps or self.settings.max_steps
        ctx = ToolContext(
            trace_id=trace_id or self.tracer.new_trace_id(),
            session_id=session_id,
            thread_id=None,
            user_id=user_id,
            actor="user",
            permissions=list(permissions if permissions is not None else self.settings.permissions),
            idempotency_key=None,
            industry_id=self.pack.id,
            channel_id=self.channel.channel_id,
            # 登录态已核验：由入口声明，不再要求模型每轮问尾号（详见 LangGraphRunner.run）
            verified=verified,
        )
        selected = self.registry.select(
            tags=tool_tags,
            intent=intent,
            max_tools=self.tools_config.injection.max_tools,
            always_include=self.tools_config.injection.always_include,
        )
        runner = build_runner(
            handle=handle,
            specs=selected,
            executor=self.executor,
            tool_context=ctx,
            context_engine=self.context_engine,
            checkpointer=open_checkpointer(self.paths.checkpoints_db_path),
            max_steps=steps,
            policy_engine=self.policy_engine,
            facts_provider=self.facts_provider,
            approval_store=self.approval_store,
            approval_ttl_hours=self.policy_engine.settings.approval_ttl_hours,
            tracer=self.tracer,
        )
        return runner, ctx

    def resolve_model_handle(
        self,
        *,
        provider: str | None = None,
        task: str | None = None,
        model: str | None = None,
        base_url: str | None = None,
        script: list[dict[str, Any]] | None = None,
    ) -> ChatModelHandle:
        """把 ``--provider``（或配置项 ``provider``）解析为模型句柄。

        - ``auto``：按 :data:`zkagent.core.llm.AUTO_PRIORITY` 挑选第一个「密钥就位」的
          供应商；全都缺失 → 落到 **Fake**（显式且可追溯，不伪装成真实模型）；
        - ``fake``：必须能命中行业包演示脚本，否则**显式报错**（不编造脚本）；
        - 其余：查 :data:`zkagent.core.llm.PROVIDER_REGISTRY`；缺模型 id 或端点时显式报错。

        :raises IndustryPackError: 演示任务无法命中。
        :raises ProviderConfigError: 供应商未知 / 配置不完整。
        """
        requested = (provider or self.settings.provider or "auto").strip().lower()

        resolved = requested
        if requested == "auto":
            # 合并进程环境与 .env：两者都算「密钥就位」
            resolved = (
                auto_select_provider({**os.environ, **settings_key_env(self.settings)}) or "fake"
            )

        if resolved == "fake":
            # 评估用例可以自带脚本（自包含、不依赖行业包演示场景）；否则按任务匹配
            chosen = (
                script if script else (find_demo_script(self.scenarios, task) if task else None)
            )
            if chosen is None:
                available = "、".join(demo_task_list(self.scenarios)) or "（行业包未提供演示脚本）"
                raise IndustryPackError(
                    "Fake 模型需要确定性脚本：请用 --task 指定行业包中的演示任务，"
                    f"或改用 --provider <供应商>。可用的演示任务：{available}"
                )
            return FakeProvider(chosen).chat_model()

        return RegistryProvider(
            resolved,
            model or self.settings.model_id,
            base_url=base_url or self.settings.base_url,
        ).chat_model()

    # ------------------------------------------------------------------ 知识侧装配

    def build_embedder_bundle(self, *, force_hash: bool = False) -> EmbedderBundle:
        """构造嵌入实现（委托给模块级 :func:`build_embedder_bundle`）。"""
        return build_embedder_bundle(self.settings, force_hash=force_hash)

    def build_retriever(
        self, *, force_hash: bool = False, with_reranker: bool | None = None
    ) -> tuple[LlamaIndexRetriever, EmbedderBundle]:
        """装配知识侧检索器（**每个行业包一套独立索引目录**，互不共享）。

        返回检索器与其嵌入实现包：嵌入模式（``local_model`` / ``fallback_hash``）
        必须随检索结果一起上报，否则降级口径会被误当成正常口径。
        """
        bundle = self.build_embedder_bundle(force_hash=force_hash)
        use_reranker = self.settings.rag_rerank if with_reranker is None else with_reranker
        reranker = (
            CrossEncoderReranker(
                self.settings.rag_reranker_model_id,
                max_candidates=self.settings.rag_rerank_max_candidates,
            )
            if use_reranker
            else None
        )
        # M3：行业包可提供标定阈值；显式配置优先，其次包级标定，最后默认值
        calibrated = None
        if self.settings.rag_min_similarity is None:
            calibration = pack_file(self.paths.industry_pack_dir, "evals/gate_calibration.json")
            if calibration.is_file():
                try:
                    data = json.loads(calibration.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    data = {}
                if data.get("embedding_mode") == bundle.mode and data.get("threshold") is not None:
                    calibrated = float(data["threshold"])
        retriever = LlamaIndexRetriever(
            industry_id=self.pack.id,
            knowledge_dir=pack_file(self.paths.industry_pack_dir, self.pack.knowledge_dir),
            index_dir=self.paths.index_dir_for(self.pack.id),
            embedder=bundle.embedder,
            embedding_mode=bundle.mode,
            chunk_size=self.settings.rag_chunk_size,
            chunk_overlap=self.settings.rag_chunk_overlap,
            top_k=self.settings.rag_top_k,
            dense_min_similarity=self.settings.rag_min_similarity or calibrated,
            reranker=reranker,
        )
        return retriever, bundle

    def evaluate_retrieval(
        self,
        *,
        k: int = 5,
        modes: list[str] | None = None,
        force_hash: bool = False,
    ) -> dict[str, Any]:
        """跑检索指标对照（Recall@k / MRR@k / nDCG@k），返回可打印的报告。

        gold 标注来自行业包的 ``evals/retrieval.yaml``；**四个配置一次跑完**，
        便于直接看出 rerank / 融合各自的收益。
        """
        retriever, bundle = self.build_retriever(
            force_hash=force_hash, with_reranker=True
        )
        retriever.load()
        eval_path = pack_file(self.paths.industry_pack_dir, self.pack.eval_retrieval)
        eval_set = load_eval_set(eval_path)
        if not eval_set.queries:
            raise RagError(f"检索评测用例为空：{eval_path}")

        runs: list[dict[str, Any]] = []
        for mode in modes or ["sparse", "dense", "hybrid", "hybrid+rerank"]:
            report = evaluate_set(retriever, eval_set.queries, k=k, mode=mode)
            rerank_outcome = retriever.last_rerank
            report["rerank_degraded"] = bool(
                getattr(rerank_outcome, "degraded", False) if rerank_outcome else False
            )
            runs.append(report)

        return {
            "industry_id": self.pack.id,
            "k": k,
            "embedder": bundle.describe(),
            "embedding_mode": bundle.mode,
            "degraded": bundle.degraded,
            "gold_file": str(eval_path),
            "runs": runs,
        }

    def index_knowledge(
        self, *, rebuild: bool = False, dry_run: bool = False, force_hash: bool = False
    ) -> dict[str, Any]:
        """构建 / 增量更新知识索引，返回可直接打印的报告字典。"""
        retriever, bundle = self.build_retriever(force_hash=force_hash)
        report = retriever.index(rebuild=rebuild, dry_run=dry_run).to_dict()
        report["embedder"] = bundle.describe()
        report["degraded"] = bundle.degraded
        report.update(self.knowledge_coverage(retriever.knowledge_dir))
        return report

    @staticmethod
    def knowledge_coverage(knowledge_dir: Path | str) -> dict[str, Any]:
        """按**用户链路**统计文档数，并列出未归位文档（不加载嵌入模型，纯文件扫描）。

        这条对账信息的意义：按链路组织知识后，「哪条链路是空的」必须能被直接看见，
        而不是靠人翻目录。未归位文档单独列出——它们不参与任何链路过滤。
        """
        docs = scan_documents(knowledge_dir)
        metas = load_knowledge_meta(knowledge_dir, known_doc_ids=sorted(docs))
        counts: dict[str, int] = {}
        # 区分两种「没有链路」：身份卡是**刻意的跨链路通用**，其余才是真的漏归位
        shared: list[str] = []
        unclassified: list[str] = []
        for doc_id, meta in metas.items():
            if not meta.journey:
                (shared if meta.doc_type == "identity" else unclassified).append(doc_id)
                continue
            counts[meta.journey] = counts.get(meta.journey, 0) + 1
        coverage = {
            journey: {"docs": counts.get(journey, 0), "label": label}
            for journey, label in JOURNEY_LABELS.items()
        }
        payload: dict[str, Any] = {"journey_coverage": coverage}
        if shared:
            payload["shared_docs"] = len(shared)
        if unclassified:
            payload["unclassified_docs"] = sorted(unclassified)
        return payload
