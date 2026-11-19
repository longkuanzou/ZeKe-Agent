"""知识侧（RAG）：检索实现、索引清单与检索指标。

**框架边界**：``llamaindex_engine.py`` 是本目录唯一允许 import ``llama_index`` 的文件；
本目录其余模块（``manifest.py`` / ``metrics.py`` / ``retriever_factory.py`` /
``knowledge_tool.py``）必须保持框架无关，由 ``tests/test_framework_boundary.py`` 静态强制。
"""
