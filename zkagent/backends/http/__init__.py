"""HTTP 业务后端适配器（P7 第二后端）。"""

from zkagent.backends.http.backend import HttpBackend
from zkagent.backends.http.facts import BackendFactsProvider

__all__ = ["HttpBackend", "BackendFactsProvider"]
