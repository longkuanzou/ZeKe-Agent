"""工具注册与按需筛选。

**禁止把全部工具描述塞给模型**（DESIGN §9 工程约束 1）：调用方必须先按
tag / 权限 / 意图筛选，再用 ``max_tools`` 截断，只有筛选后的子集才允许注入。

本模块**不感知任何业务语义**，也不做幂等与重试（那是 ``ToolExecutor`` 的职责）。
"""

from __future__ import annotations

from zkagent.core.types import IndustryToolsConfig, ToolSpec

__all__ = ["ToolRegistry", "apply_industry_overrides"]


class ToolRegistry:
    """工具注册表：注册、查询与筛选。"""

    def __init__(self) -> None:
        self._specs: dict[str, ToolSpec] = {}

    # ------------------------------------------------------------------ 注册与查询

    def register(self, spec: ToolSpec) -> None:
        """注册一个工具；重名直接报错（避免静默覆盖导致行为漂移）。"""
        if spec.name in self._specs:
            raise ValueError(f"工具重复注册：{spec.name}")
        self._specs[spec.name] = spec.model_copy(deep=True)

    def register_many(self, specs: list[ToolSpec]) -> None:
        """批量注册。"""
        for spec in specs:
            self.register(spec)

    def get(self, name: str) -> ToolSpec | None:
        """按名字取回工具声明（返回副本）。"""
        spec = self._specs.get(name)
        return spec.model_copy(deep=True) if spec is not None else None

    def all(self) -> list[ToolSpec]:
        """返回全部工具声明（按名称排序，保证输出稳定）。"""
        return [self._specs[name].model_copy(deep=True) for name in sorted(self._specs)]

    def names(self) -> list[str]:
        """返回全部工具名（已排序）。"""
        return sorted(self._specs)

    def __len__(self) -> int:
        return len(self._specs)

    def __contains__(self, name: object) -> bool:
        return name in self._specs

    # ------------------------------------------------------------------ 筛选

    def select(
        self,
        *,
        tags: list[str] | None = None,
        permissions: list[str] | None = None,
        intent: str | None = None,
        max_tools: int | None = None,
        always_include: list[str] | None = None,
    ) -> list[ToolSpec]:
        """按标签、调用方权限与意图筛选工具，并截断到 ``max_tools``。

        :param tags: 任一所给标签命中即保留（``None`` 表示不按标签过滤）。
        :param permissions: 调用方持有的权限集合；工具要求的权限必须**全部**具备。
        :param intent: 意图关键字；命中工具名或任一标签即保留。
        :param max_tools: 结果上限（按名称排序后截断，保证可复现）。
        :param always_include: 核心工具清单，**不受 tags / intent / max_tools 影响**
            （仍受权限约束）。顺序即优先级；未注册的名字直接报错，避免拼写错误
            悄悄把核心工具裁掉。
        """
        pinned: list[ToolSpec] = []
        pinned_names: set[str] = set()
        for name in always_include or []:
            spec = self._specs.get(name)
            if spec is None:
                raise ValueError(f"always_include 中的工具未注册：{name}")
            if permissions is not None and not set(spec.permissions) <= set(permissions):
                continue
            pinned.append(spec.model_copy(deep=True))
            pinned_names.add(name)

        selected: list[ToolSpec] = []
        for name in sorted(self._specs):
            if name in pinned_names:
                continue
            spec = self._specs[name]
            if tags and not set(tags) & set(spec.tags):
                continue
            if permissions is not None and not set(spec.permissions) <= set(permissions):
                continue
            if intent and intent not in spec.name and intent not in spec.tags:
                continue
            selected.append(spec.model_copy(deep=True))

        combined = [*pinned, *selected]
        if max_tools is None:
            return combined
        limit = max(0, max_tools)
        if len(pinned) >= limit:
            # 预算小于核心清单时，仍按核心优先级截断：上限是硬约束，核心只是排序靠前
            return pinned[:limit]
        return combined[:limit]


def apply_industry_overrides(
    specs: list[ToolSpec],
    config: IndustryToolsConfig,
) -> list[ToolSpec]:
    """把行业包的 ``tools.yaml`` 应用到后端声明的工具上。

    处理三件事：
    1. 只保留 ``enabled`` 中列出的工具；
    2. 用 ``tags`` 补充 / 覆盖标签（用于按需注入过滤）；
    3. 用 ``overrides`` 覆盖 ``risk`` / ``timeout_s`` / ``confirmation_required`` /
       ``requires_idempotency_key`` / ``description`` 等元数据。

    ``enabled`` 中出现但后端未声明的工具会被忽略，并由 :func:`missing_enabled_tools`
    报告出来——**不得静默放过**。
    """
    declared = {spec.name: spec for spec in specs}
    result: list[ToolSpec] = []
    for name in config.enabled:
        base = declared.get(name)
        if base is None:
            continue
        spec = base.model_copy(deep=True)
        if name in config.tags:
            spec.tags = list(dict.fromkeys([*spec.tags, *config.tags[name]]))
        for field, value in config.overrides.get(name, {}).items():
            if field not in ToolSpec.model_fields:
                raise ValueError(f"tools.yaml 的 overrides 含未知字段：{name}.{field}")
            setattr(spec, field, value)
        result.append(spec)
    return result


def missing_enabled_tools(specs: list[ToolSpec], config: IndustryToolsConfig) -> list[str]:
    """返回 ``tools.yaml`` 声明启用、但后端未提供的工具名。"""
    declared = {spec.name for spec in specs}
    return sorted(set(config.enabled) - declared)
