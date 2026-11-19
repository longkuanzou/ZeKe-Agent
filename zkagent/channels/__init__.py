"""zkagent 渠道适配器：消息收发与渲染。

- ``cli.py``：命令行渠道（``card=False``）。
- HTTP 渠道属 P7。
"""

from zkagent.channels.cli import CliChannel

__all__ = ["CliChannel"]
