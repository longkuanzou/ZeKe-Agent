"""LLM 摘要的「接线」测试：什么时候把 summarizer 注入 ``ContextEngine``。

分工：
- `test_compression.py` 验的是**注入了之后行为对不对**（成功替换、失败回退、超长收缩）；
- 本文件验的是**什么时候注入**——`Container._install_summarizer` 的三条分支：
  开关未开、开关开了但 provider 是 fake、开关开了且是真 provider。

这三条分支此前没有测试兜底：若哪天把 ``provider_id == "fake"`` 那个判断删掉，
Fake 模式的离线用例会突然开始依赖真实模型，而当时不会有一条断言先红。
"""

from __future__ import annotations

from zkagent.config import Container, load_settings
from zkagent.core.types import ChatModelHandle


def _build_container(tmp_path, **overrides) -> Container:
    """用临时目录装配一个 Container（不触网、不写演示库）。"""
    settings = load_settings(
        project_root=tmp_path,
        db_path=tmp_path / "business.db",
        checkpoints_db_path=tmp_path / "checkpoints.db",
        trace_dir=tmp_path / "traces",
        **overrides,
    )
    return Container.build(settings)


def test_summarizer_is_not_installed_by_default(tmp_path) -> None:
    """验证：``ZKAGENT_CONTEXT_LLM_SUMMARY`` 未开启（默认）时不注入，保证默认完全离线。"""
    container = _build_container(tmp_path)

    assert container.context_engine._summarizer is None  # noqa: SLF001 - 验的就是这处装配


def test_fake_provider_never_installs_summarizer(tmp_path) -> None:
    """验证：开关打开但 ``provider=fake`` 时仍不注入——确定性的离线用例不得依赖真实模型。"""
    container = _build_container(tmp_path, context_llm_summary=True, provider="fake")

    assert container.context_engine._summarizer is None  # noqa: SLF001


def test_real_provider_installs_summarizer(tmp_path, monkeypatch) -> None:
    """验证：开关打开且 provider 非 fake 时注入 callable。

    这里不真的调用模型（``resolve_model_handle`` 被替换成返回句柄），
    只断言「装配发生过」以及别名生效。
    """
    monkeypatch.setattr(
        Container,
        "resolve_model_handle",
        lambda self, provider=None: ChatModelHandle(  # noqa: ARG005 - 签名对齐被测代码
            provider_id="deepseek", model_id="deepseek-chat"
        ),
    )

    container = _build_container(tmp_path, context_llm_summary=True)

    assert callable(container.context_engine._summarizer)  # noqa: SLF001


def test_config_error_keeps_summarizer_absent(tmp_path, monkeypatch) -> None:
    """验证：解包模型句柄失败（缺密钥 / 行业包问题）时静默跳过，不阻断装配。

    摘要只是可选增强，它的装配失败不应该让整个 Container 起不来。
    """
    from zkagent.core.llm import ProviderConfigError

    def boom(self, provider=None):  # noqa: ARG001 - 模拟缺密钥
        raise ProviderConfigError("缺少密钥")

    monkeypatch.setattr(Container, "resolve_model_handle", boom)

    container = _build_container(tmp_path, context_llm_summary=True)

    assert container.context_engine._summarizer is None  # noqa: SLF001
