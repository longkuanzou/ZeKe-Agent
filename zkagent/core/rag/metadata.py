"""文档级元数据与可见性判定——**框架无关**（只依赖 ``yaml`` 与 ``pydantic``）。

对应 DESIGN §6.3.4「权限过滤」：

1. 文档的 ``doc_type`` / ``acl_tags`` / ``roles`` 由行业包的 ``knowledge/meta.yaml``
   **声明式**给出，不进代码；
2. ``acl_tags`` 为空 ⇒ 公开；非空 ⇒ 调用方必须持有**全部**标签才可见（取交集为空的
   旧口径会让「公开文档」永远检索不到，故此处收紧为子集判定）；
3. 调用方的标签只能来自**服务端**（``ToolContext.permissions`` 投影），
   模型自报的标签不被信任——这是「不得用提示词代替权限校验」在检索侧的落地。

> 与 DESIGN §6.3.4 的差异：原口径写「与文档 acl_tags 求交，非空才可命中」，
> 无法表达公开文档。这里改为「空即公开 + 子集判定」，并在测试中固化。

## 用户链路（``journey``）——知识按「用户旅程」组织

知识库按**用户链路**分目录，一篇文档**只属于一条链路**（跨链路内容拆篇，
同一事实只在一篇定义，其它篇用「见 X」引用）：

| 目录 | 链路 | 典型内容 |
| --- | --- | --- |
| ``presale/`` | 售前咨询 | 选购建议、功能支持矩阵、适配条件（**不含具体参数**） |
| ``install/`` | 安装咨询 | 安装条件、预约流程、收费口径 |
| ``usage/`` | 使用咨询 | 首次使用、日常操作、保养清洁 |
| ``troubleshoot/`` | 故障排查 | 现象 → 排查步骤 → 转人工边界 |
| ``after_sales/`` | 售后政策 | 退换、保修、运费、发票、会员、投诉 |
| ``repair_request/`` | 报修建单 | 报修流程、信息采集、备件与进度 |
| ``internal/`` | 内部口径 | 坐席内部规则（``acl_tags`` 受限，不属对外链路） |

**``journey`` 由 ``doc_id`` 的首段目录推导，不在 ``meta.yaml`` 里重复声明**——
路径即事实来源，避免「声明与目录不一致」的双份真相。首段不属于上述目录时
``journey`` 为 ``None``，视为**跨链路通用文档**（如型号身份卡）：它不会被链路过滤排除，
但也不计入任何链路。行业包用 ``tests/rag/test_journey_taxonomy.py`` 与打包器
强制「除显式通用的身份卡外，每篇都必须归位」。

## 第二种元数据来源：Markdown front-matter

有些知识库（如 ``appliance-kb/08_index/chunks/``）把元数据写在**文档自己的 front-matter** 里，
路径按分片类型分组（``fact/`` / ``faq/`` / ``policy/`` …）。这类库不应被要求复制一份 ``meta.yaml``，
否则就是两份真相。因此加载时按**优先级**合并：

1. ``meta.yaml`` 中显式声明的字段（最高，用于覆盖个别文档）；
2. 文档 front-matter（``doc_type`` / ``scenario`` / ``appliance`` / ``product_id`` /
   ``source`` / ``source_page`` / ``verified``）；
3. 路径推导（``journey``）与 ``defaults``（其余字段）。

`scenario` 遵循 ``s1_presale`` … ``s6_service`` 的命名，映射到本项目的六条链路；
**目录优先**：目录能推出链路时不再看 ``scenario``。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field

from zkagent.core.rag.errors import RagError

__all__ = [
    "META_FILENAMES",
    "KB_PERMISSION_PREFIX",
    "JOURNEYS",
    "CONSULT_JOURNEYS",
    "JOURNEY_LABELS",
    "SCENARIO_TO_JOURNEY",
    "FRONT_MATTER_DELIMITER",
    "KnowledgeMetaError",
    "DocDefaults",
    "DocMeta",
    "KnowledgeMeta",
    "journey_of_doc_id",
    "journey_of_scenario",
    "parse_front_matter",
    "load_knowledge_meta",
    "is_visible",
    "exclusion_reason",
    "filter_visible",
    "project_acl_tags",
]

#: 用户链路全集（``internal`` 是坐席内部口径，不对应对外链路）。
JOURNEYS: tuple[str, ...] = (
    "presale",
    "install",
    "usage",
    "troubleshoot",
    "after_sales",
    "repair_request",
    "internal",
)

#: 对外六条链路（模型可指定的过滤范围，不含 ``internal``）。
CONSULT_JOURNEYS: tuple[str, ...] = JOURNEYS[:-1]

#: 链路的中文名（CLI 与报告展示用；**只影响展示，不影响匹配**）。
JOURNEY_LABELS: dict[str, str] = {
    "presale": "售前咨询",
    "install": "安装咨询",
    "usage": "使用咨询",
    "troubleshoot": "故障排查",
    "after_sales": "售后政策",
    "repair_request": "报修建单",
    "internal": "内部口径",
}

#: ``scenario``（外部知识库的场景命名）→ 本项目用户链路。
SCENARIO_TO_JOURNEY: dict[str, str] = {
    "s1_presale": "presale",
    "s2_install": "install",
    "s3_usage": "usage",
    "s4_troubleshoot": "troubleshoot",
    "s5_aftersale": "after_sales",
    "s6_service": "repair_request",
}

#: front-matter 分隔符（文件必须以它开头才解析）。
FRONT_MATTER_DELIMITER = "---"

#: 元数据文件候选名（存在其一即可；都不存在时视为全公开）。
META_FILENAMES: tuple[str, ...] = ("meta.yaml", "meta.yml")

#: 权限 → 可见标签的投影前缀：``kb:internal`` 表示可读 ``acl_tags=[internal]`` 的文档。
KB_PERMISSION_PREFIX = "kb:"


class KnowledgeMetaError(RagError):
    """元数据文件结构非法（如引用了不存在的 doc_id）——**必须显式失败**。"""


class DocDefaults(BaseModel):
    """未在 ``docs`` 中单独声明的文档所使用的默认元数据（不含 ``doc_id``）。"""

    doc_type: str = "faq"
    acl_tags: list[str] = Field(default_factory=list)
    roles: list[str] = Field(default_factory=list)


class DocMeta(BaseModel):
    """单篇文档的元数据。

    前四个字段用于**过滤**；后五个字段用于**溯源与展示**（不参与过滤，但决定「能不能信」）。
    """

    doc_id: str
    doc_type: str = "faq"
    acl_tags: list[str] = Field(default_factory=list, description="为空表示公开")
    roles: list[str] = Field(default_factory=list)
    journey: str | None = Field(
        default=None,
        description="用户链路；由 doc_id 首段目录推导（见模块文档），不在 meta.yaml 重复声明",
    )
    #: 品类键（外部知识库的分区名，如 ``air_conditioner``）；来自 front-matter。
    appliance: str | None = Field(default=None, description="品类键，如 air_conditioner")
    #: 一条文档适用多个品类时用列表（政策文件常写 ``applies_to.appliance: [5 个]``）。
    #: 单一事实来源改造后，政策不再按品类拆成多个切片，靠这个字段参与品类过滤，
    #: 否则「跨品类通用」会退化成「任何品类都能召回」（实测售前 top-5 被政策占满）。
    appliances: list[str] = Field(default_factory=list, description="适用品类集合（可空）")
    #: 型号级文档所属的商品 id；全局文档为 ``None``。
    product_id: str | None = Field(default=None, description="型号级文档的 product_id")
    #: 品牌级文档所属品牌（``midea`` / ``haier`` …）。品牌政策是品牌级而非型号级
    #: （``product_id`` 为空），没有这个字段就只能靠语义猜，容易串到兄弟品牌（M25）。
    brand: str | None = Field(default=None, description="品牌键，如 midea")
    #: 一条文档适用多个品牌时用列表（FAQ 的 ``scope.brand`` 可能多值）；
    #: 过滤语义是「命中任一即保留」，空列表表示无品牌归属。
    brands: list[str] = Field(default_factory=list, description="适用品牌集合（可空）")
    #: 原始出处（文件 + 锚点 / 页码），用于「答错能查是谁的错」。
    source: str | None = Field(default=None, description="原始出处的相对路径与锚点")
    source_page: int | None = Field(default=None, description="说明书页码（无则 None）")
    #: 依据是否经过人工核对；``False`` 表示**不得当作已确认事实**，回答需注明。
    verified: bool | None = Field(default=None, description="依据是否已完成人工核对")


class KnowledgeMeta(BaseModel):
    """``knowledge/meta.yaml`` 的强类型表示。"""

    version: int = 1
    defaults: DocDefaults = Field(default_factory=DocDefaults)
    docs: list[DocMeta] = Field(default_factory=list)

    def by_doc_id(self) -> dict[str, DocMeta]:
        """``doc_id -> DocMeta``（未声明的文档使用 ``defaults``）。"""
        return {entry.doc_id: entry for entry in self.docs}


def journey_of_doc_id(doc_id: str) -> str | None:
    """从 ``doc_id`` 的首段目录推导用户链路；不在链路目录下返回 ``None``（未分类）。

    路径即事实来源：``after_sales/warranty/warranty_policy.md`` → ``after_sales``。
    这样「目录分组」与「检索过滤」永远一致，不会出现 meta.yaml 写着 A 而文件在 B 的情况。
    """
    normalized = doc_id.replace("\\", "/")
    if "/" not in normalized:
        return None
    head = normalized.split("/", 1)[0]
    return head if head in JOURNEYS else None


def journey_of_scenario(scenario: Any) -> str | None:
    """把外部知识库的 ``scenario``（``s1_presale`` …）映射为链路；未知返回 ``None``。"""
    if not scenario:
        return None
    return SCENARIO_TO_JOURNEY.get(str(scenario).strip())


def parse_front_matter(text: str) -> tuple[dict[str, Any], str]:
    """解析文档开头的 YAML front-matter，返回 ``(metadata, body)``。

    - 不以 ``---`` 开头：返回 ``({}, 原文)``；
    - 有开始没有结束分隔符 / YAML 非法 / 顶层不是映射：抛 :class:`KnowledgeMetaError`。

    **正文会去掉 front-matter**：否则元数据会进入嵌入与 BM25，稀释真实内容的权重。
    """
    if not text.lstrip("\ufeff").startswith(FRONT_MATTER_DELIMITER):
        return {}, text

    lines = text.lstrip("\ufeff").splitlines(keepends=True)
    end = next(
        (index for index in range(1, len(lines)) if lines[index].strip() == FRONT_MATTER_DELIMITER),
        None,
    )
    if end is None:
        raise KnowledgeMetaError("front-matter 缺少结束分隔符 ---")

    try:
        data: Any = yaml.safe_load("".join(lines[1:end])) or {}
    except yaml.YAMLError as exc:
        raise KnowledgeMetaError(f"front-matter YAML 解析失败：{exc}") from exc
    if not isinstance(data, dict):
        raise KnowledgeMetaError("front-matter 顶层必须是映射")
    return data, "".join(lines[end + 1 :])


def _read_front_matter(directory: Path, doc_id: str) -> dict[str, Any]:
    """读取单篇文档的 front-matter；文件缺失视为无元数据。"""
    path = directory / doc_id
    if not path.is_file():
        return {}
    try:
        data, _ = parse_front_matter(path.read_text(encoding="utf-8"))
    except OSError:
        return {}
    except KnowledgeMetaError as exc:
        raise KnowledgeMetaError(f"{doc_id}：{exc}") from exc
    return data


def _build_meta(
    doc_id: str,
    *,
    defaults: DocDefaults,
    declared: DocMeta | None,
    front: dict[str, Any],
) -> DocMeta:
    """按「meta.yaml > front-matter > defaults」合并出最终元数据。

    ``journey`` 单独处理：目录（若可推导）优先于 ``scenario``，因为路径是本项目的既定事实来源。
    """
    if declared is not None:
        # meta.yaml 一旦声明该文档，就由它决定类型与可见性（显式配置覆盖文档自述）
        doc_type = declared.doc_type
        acl_tags = list(declared.acl_tags)
        roles = list(declared.roles)
    else:
        doc_type = str(front.get("doc_type") or defaults.doc_type)
        acl_tags = [str(item) for item in (front.get("acl_tags") or defaults.acl_tags)]
        roles = [str(item) for item in (front.get("roles") or defaults.roles)]

    journey = declared.journey if declared is not None and declared.journey else None
    journey = journey or journey_of_doc_id(doc_id) or journey_of_scenario(front.get("scenario"))

    def _pick(name: str) -> Any:
        """按优先级取字段：``meta.yaml`` 显式声明 > front-matter。

        :param name: 字段名（如 ``source`` / ``source_page`` / ``verified``）。
        :returns: 命中的值；两处都没有则返回 ``None``。
        """
        if declared is not None:
            value = getattr(declared, name)
            if value is not None:
                return value
        return front.get(name)

    page = _pick("source_page")
    verified = _pick("verified")
    appliance = _pick("appliance")
    product_id = _pick("product_id")
    if not product_id:
        # 上游 FAQ 用 ``scope_models`` 表达「只适用于某型号」。不投影成 ``product_id``，
        # 这类分片就永远不受 ``product_ids`` 过滤（M22：19 篇 FAQ 污染了 20% 的 top-5）。
        # 只投影**单型号**：多型号需要「命中任一即保留」的多值过滤，本字段表达不了，
        # 留空（宁可不过滤，也不错误收窄到一个型号）。
        scope = front.get("scope_models") or []
        if isinstance(scope, str):
            scope = [scope]
        scope = [str(item).strip() for item in scope if str(item).strip()]
        if len(scope) == 1:
            product_id = scope[0]
    brand = _pick("brand")
    brands = [str(item).strip() for item in (front.get("brands") or []) if str(item).strip()]
    if not brand and len(brands) == 1:
        brand = brands[0]
    # 政策切片把适用品类写在 `applies_to_appliance`（build_index 派生），单品类文档
    # 仍用 `appliance`；两者都参与过滤，语义是「命中任一品类即保留」。
    appliances = [
        str(item).strip()
        for item in (front.get("applies_to_appliance") or [])
        if str(item).strip()
    ]
    source = _pick("source")

    return DocMeta(
        doc_id=doc_id,
        doc_type=doc_type,
        acl_tags=acl_tags,
        roles=roles,
        journey=journey,
        appliance=str(appliance) if appliance else None,
        appliances=appliances,
        product_id=str(product_id) if product_id else None,
        brand=str(brand).strip() if brand else None,
        brands=brands,
        source=str(source) if source else None,
        source_page=(
            int(page)
            if isinstance(page, (int, str)) and str(page).strip().isdigit()
            else None
        ),
        verified=verified if isinstance(verified, bool) else None,
    )


def load_knowledge_meta(
    knowledge_dir: Path | str,
    *,
    known_doc_ids: list[str] | None = None,
    read_front_matter: bool = True,
) -> dict[str, DocMeta]:
    """加载文档元数据，返回 ``doc_id -> DocMeta``。

    数据来源与优先级见模块文档：

    1. ``knowledge/meta.yaml``（存在则校验结构；显式条目覆盖文档自述）；
    2. 文档自己的 Markdown front-matter（``read_front_matter=True`` 时读取）；
    3. 路径推导（``journey``）与 ``defaults``。

    - ``meta.yaml`` 不存在**不再是错误**：整库可以完全靠 front-matter 描述；
    - 传 ``known_doc_ids`` 时校验「声明了但磁盘不存在」的条目并报错（拼写错误的代价很高）。
    """
    directory = Path(knowledge_dir)
    meta_path = next(
        (directory / name for name in META_FILENAMES if (directory / name).is_file()), None
    )

    defaults = DocDefaults()
    declared: dict[str, DocMeta] = {}

    if meta_path is not None:
        try:
            raw: Any = yaml.safe_load(meta_path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            raise KnowledgeMetaError(f"元数据 YAML 解析失败：{meta_path}：{exc}") from exc
        if not isinstance(raw, dict):
            raise KnowledgeMetaError(f"元数据顶层必须是映射：{meta_path}")

        try:
            meta = KnowledgeMeta.model_validate(raw)
        except Exception as exc:
            raise KnowledgeMetaError(f"元数据结构非法：{meta_path}：{exc}") from exc

        defaults = meta.defaults
        declared = meta.by_doc_id()

        if known_doc_ids is not None:
            on_disk = set(known_doc_ids)
            missing = sorted(entry.doc_id for entry in meta.docs if entry.doc_id not in on_disk)
            if missing:
                raise KnowledgeMetaError(
                    f"元数据声明的文档在知识目录中不存在（检查路径拼写）：{missing}"
                )

        # 显式声明的 journey 必须与目录一致：允许「不写」，但「写错」要当场报错
        for entry in meta.docs:
            inferred = journey_of_doc_id(entry.doc_id)
            if entry.journey is not None and inferred is not None and entry.journey != inferred:
                raise KnowledgeMetaError(
                    f"文档 {entry.doc_id} 声明的 journey={entry.journey!r} 与目录推导的 "
                    f"{inferred!r} 不一致；journey 由目录决定，不要在 meta.yaml 里另写一份"
                )

    doc_ids = sorted(known_doc_ids) if known_doc_ids is not None else sorted(declared)
    result: dict[str, DocMeta] = {}
    for doc_id in doc_ids:
        front = _read_front_matter(directory, doc_id) if read_front_matter else {}
        result[doc_id] = _build_meta(
            doc_id,
            defaults=defaults,
            declared=declared.get(doc_id),
            front=front,
        )
    return result


def project_acl_tags(permissions: list[str] | None) -> list[str]:
    """把服务端权限投影为可见标签：``kb:internal`` → ``internal``。

    只有带 ``kb:`` 前缀的权限才影响知识库可见性，避免把业务权限误当文档标签。
    """
    tags: list[str] = []
    for permission in permissions or []:
        if permission.startswith(KB_PERMISSION_PREFIX):
            tag = permission[len(KB_PERMISSION_PREFIX) :].strip()
            if tag and tag not in tags:
                tags.append(tag)
    return tags


def is_visible(meta: DocMeta, caller_tags: list[str]) -> bool:
    """文档是否对给定标签集合可见：``acl_tags`` 为空即公开，否则需**全部**持有。"""
    if not meta.acl_tags:
        return True
    return set(meta.acl_tags).issubset(set(caller_tags))


def exclusion_reason(
    meta: DocMeta,
    *,
    caller_tags: list[str],
    doc_types: list[str] | None = None,
    journeys: list[str] | None = None,
    appliances: list[str] | None = None,
    product_ids: list[str] | None = None,
    brands: list[str] | None = None,
) -> str | None:
    """返回该文档被排除的原因；``None`` 表示可见。

    **过滤判定只有这一处实现**：``filter_visible`` 与检索追踪（``search(trace=...)``）
    共用它，否则「实际过滤」与「界面解释为什么被丢弃」迟早会不一致。
    """
    if not is_visible(meta, caller_tags):
        return "acl"
    allowed_types = set(doc_types or [])
    if allowed_types and meta.doc_type not in allowed_types:
        return "doc_type"
    allowed_journeys = set(journeys or [])
    # ``journey is None`` = **跨链路通用**（如型号身份卡）：设了链路也不该消失。
    # 这与「行业包内每篇都要归位」不冲突：归位要求由 tests/rag/test_journey_taxonomy.py
    # 与打包器（只允许 identity 为通用）把关，而不是靠检索时静默丢弃。
    if allowed_journeys and meta.journey is not None and meta.journey not in allowed_journeys:
        return "journey"
    # 品类 / 型号过滤是「限定范围」而非「排除通用」：没有归属的文档仍然适用
    allowed_appliances = set(appliances or [])
    if allowed_appliances:
        # 单品类（`appliance`）与多品类（`appliances`）合并判断：政策的适用品类是列表，
        # 只认单值字段会让「跨品类的平台政策」在任意品类查询里都始终保留。
        doc_appliances = ({meta.appliance} if meta.appliance else set()) | set(meta.appliances or [])
        if doc_appliances and not (doc_appliances & allowed_appliances):
            return "appliance"
    allowed_products = set(product_ids or [])
    if allowed_products and meta.product_id and meta.product_id not in allowed_products:
        return "product_id"
    # 品牌同理是「限定范围」：没有品牌归属的文档（平台政策、通用 FAQ）始终适用；
    # 只有明确属于**其他**品牌的文档才排除。多品牌文档按「命中任一」保留。
    allowed_brands = set(brands or [])
    if allowed_brands:
        doc_brands = ({meta.brand} if meta.brand else set()) | set(meta.brands or [])
        if doc_brands and not (doc_brands & allowed_brands):
            return "brand"
    return None


def filter_visible(
    doc_metas: dict[str, DocMeta],
    *,
    caller_tags: list[str],
    doc_types: list[str] | None = None,
    journeys: list[str] | None = None,
    appliances: list[str] | None = None,
    product_ids: list[str] | None = None,
    brands: list[str] | None = None,
) -> set[str]:
    """返回可见 ``doc_id`` 集合。

    :param caller_tags: **服务端**投影得到的可见标签（不接受模型自报）。
    :param doc_types: 可选的类型白名单（``policy`` / ``manual`` / ``faq``）。
    :param journeys: 可选的**用户链路**白名单（收窄检索范围，不涉及权限）。
        ``journey is None`` 的文档视为**跨链路通用**（如型号身份卡），不因设置链路而消失。
    :param appliances: 可选的**品类**白名单（``air_conditioner`` 等）。无品类归属的
        「通用文档」（平台政策、跨品类 FAQ）**始终保留**——它们是跨品类共用的规则。
    :param product_ids: 可选的**型号**白名单。同理，无型号归属的通用文档始终保留。
    :param brands: 可选的**品牌**白名单。品牌政策是品牌级（``product_id`` 为空），
        这一维只排除「明确属于其他品牌」的文档；无品牌归属的文档保留。
    """
    return {
        doc_id
        for doc_id, meta in doc_metas.items()
        if exclusion_reason(
            meta,
            caller_tags=caller_tags,
            doc_types=doc_types,
            journeys=journeys,
            appliances=appliances,
            product_ids=product_ids,
            brands=brands,
        )
        is None
    }
