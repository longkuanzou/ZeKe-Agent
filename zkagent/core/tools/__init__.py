"""zkagent.core.tools —— 工具注册与执行（不感知任何业务语义）。

- ``schema``：错误码取值域与 ToolSpec 校验辅助。
- ``registry``：注册与按 tag / 权限 / 意图过滤。
- ``executor``：唯一允许做幂等、重试、超时与错误码归一的地方。
"""

__all__: list[str] = []
