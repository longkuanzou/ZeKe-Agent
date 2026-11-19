"""``zkagent`` 命令行入口（标准库 argparse，零额外依赖）。

子命令：

- ``zkagent db reset``      重建业务库（删表重建）
- ``zkagent db seed``       按行业包 ``seed_data/`` 灌入种子数据
- ``zkagent db inspect``    查看某张表的内容
- ``zkagent tools list``    列出（可按 tag 过滤的）已注册工具
- ``zkagent providers list`` 列出内置模型供应商与本机密钥是否就位（**不显示密钥值**）
- ``zkagent chat``          与 Agent 对话（``--task`` 单轮；不带 ``--task`` 进入多轮交互）
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from zkagent.backends.mock.db import REQUIRED_TABLES, Database
from zkagent.backends.mock.seed import seed_database
from zkagent.channels.cli import CliChannel
from zkagent.config import (
    Container,
    IndustryPackError,
    load_industry_pack,
    load_settings,
    pack_file,
    provider_catalog_with_keys,
    resolve_paths,
)
from zkagent.core.evals.loader import EvalCaseError, load_cases, validate_cases
from zkagent.core.evals.report import render_markdown, write_report
from zkagent.core.evals.runner import EvalRunner, run_context_compression
from zkagent.core.llm import (
    AUTO_PRIORITY,
    ProviderCallError,
    ProviderConfigError,
    provider_ids,
)
from zkagent.core.observability.cost import summarize as summarize_usage
from zkagent.core.policy.approvals import is_expired
from zkagent.core.policy.errors import ApprovalError
from zkagent.core.rag.build_status import read_status, write_status
from zkagent.core.rag.errors import RagError
from zkagent.core.types import OutboundMessage, PolicyDecision

__all__ = ["main", "build_parser"]


def _error_payload(exc: BaseException) -> dict[str, object]:
    """统一的错误 JSON：带 ``error_code``，不把异常栈甩给用户。"""
    return {
        "ok": False,
        "error_code": getattr(exc, "code", type(exc).__name__),
        "error": str(exc),
    }


def build_parser() -> argparse.ArgumentParser:
    """构造命令行解析器。"""
    parser = argparse.ArgumentParser(prog="zkagent", description="ZeKe-Agent（知客 agent）")
    parser.add_argument("--industry", help="行业包 id（默认取环境变量 ZKAGENT_INDUSTRY 或 appliance）")
    parser.add_argument(
        "--backend",
        choices=["mock", "http"],
        default=None,
        help="业务后端：mock（本地 SQLite，默认）或 http（legacy 业务服务）",
    )
    parser.add_argument(
        "--http-backend-url",
        default=None,
        help="HTTP 业务后端地址（--backend http 时使用，默认 http://localhost:8100）",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    db = sub.add_parser("db", help="业务库维护")
    db_sub = db.add_subparsers(dest="db_command", required=True)

    db_sub.add_parser("reset", help="删表重建业务库")
    db_sub.add_parser("seed", help="灌入行业包种子数据")
    db_sub.add_parser("check", help="检查表是否齐全（缺表时给出升级命令）")

    inspect = db_sub.add_parser("inspect", help="查看表内容")
    inspect.add_argument("--table", required=True, help="表名")
    inspect.add_argument("--limit", type=int, default=20, help="最多显示多少行（默认 20）")

    tools = sub.add_parser("tools", help="工具查看")
    tools_sub = tools.add_subparsers(dest="tools_command", required=True)
    tools_list = tools_sub.add_parser("list", help="列出已注册工具")
    tools_list.add_argument("--tag", action="append", default=[], help="按 tag 过滤（可重复）")
    tools_list.add_argument("--max", type=int, default=None, help="结果上限")
    tools_list.add_argument(
        "--inject-preview",
        action="store_true",
        help="按当前 max_tools / always_include 预览实际注入的子集（打印全集与注入数量）",
    )

    chat = sub.add_parser("chat", help="与 Agent 对话")
    chat.add_argument("--task", help="单轮任务文本（Fake 模型下需命中行业包演示场景）")
    chat.add_argument("--user", default=None, help="当前用户标识（工具归属校验用）")
    chat.add_argument(
        "--session",
        default=None,
        help="会话 id（决定检查点线程；resume 时默认沿用审批记录里的会话）",
    )
    chat.add_argument(
        "--permissions",
        action="append",
        default=[],
        help="调用方权限（开发期发放路径，如 kb:internal；可重复）",
    )
    chat.add_argument(
        "--intent",
        default=None,
        help="意图关键字（仅会话构建时生效；多轮中工具已绑定，不会逐轮重选）",
    )
    chat.add_argument(
        "--provider",
        default=None,
        choices=["auto", "fake", *provider_ids()],
        help="模型供应商；auto（默认）：按环境变量里哪个密钥就位自动选择，全都缺失则用 fake",
    )
    chat.add_argument(
        "--model",
        default=None,
        help="模型 id；留空使用该供应商默认模型（openai / anthropic / ollama / custom 必须显式给出）",
    )
    chat.add_argument(
        "--base-url",
        default=None,
        help="模型端点覆盖：自建网关 / 本地 vLLM / LM Studio 等 OpenAI 兼容端点",
    )
    chat.add_argument("--max-steps", type=int, default=None, help="单轮最大步数（默认取配置）")
    chat.add_argument("--tag", action="append", default=[], help="只注入指定 tag 的工具（可重复）")
    chat.add_argument(
        "--resume",
        default=None,
        metavar="APPROVAL_ID",
        help="从人工审批中断点恢复执行（先 approvals approve/reject 再 resume）",
    )

    approvals = sub.add_parser("approvals", help="HITL 审批")
    approvals_sub = approvals.add_subparsers(dest="approvals_command", required=True)
    approvals_list = approvals_sub.add_parser("list", help="列出待审批")
    approvals_list.add_argument("--limit", type=int, default=20)
    for name, help_text in (("approve", "批准"), ("reject", "拒绝")):
        item = approvals_sub.add_parser(name, help=f"{help_text}指定审批单")
        item.add_argument("approval_id", help="审批单号（AP-…）")
        item.add_argument("--actor", default=None, help="审批人（未配置 HITL_APPROVERS 时为开发模式）")
        item.add_argument("--token", default=None, help="审批令牌（HITL_APPROVERS 中配置）")
        item.add_argument("--reason", default="", help="拒绝原因（reject 时可选）")
        item.add_argument(
            "--permissions",
            action="append",
            default=[],
            help="审批人权限（可重复；未配置 HITL_APPROVER_PERMISSIONS 时使用）",
        )

    providers = sub.add_parser("providers", help="模型供应商")
    providers_sub = providers.add_subparsers(dest="providers_command", required=True)
    providers_sub.add_parser("list", help="列出内置供应商与本机密钥是否就位")

    rag = sub.add_parser("rag", help="知识库索引")
    rag_sub = rag.add_subparsers(dest="rag_command", required=True)
    rag_index = rag_sub.add_parser("index", help="构建 / 增量更新知识索引")
    rag_index.add_argument("--rebuild", action="store_true", help="强制全量重建（删掉旧索引）")
    rag_index.add_argument(
        "--dry-run", action="store_true", help="只报告将新增/修改/删除的数量，不写任何文件"
    )
    rag_index.add_argument(
        "--hash-embedder",
        action="store_true",
        help="强制使用确定性 hash 兜底向量（离线 / 快速模式；指标口径会降级，报告会标注）",
    )
    rag_index.add_argument(
        "--background",
        action="store_true",
        help="后台构建：立即返回，状态写入 build_status.json（用 `rag status` 查看）",
    )
    rag_index.add_argument("--status-writer", action="store_true", help=argparse.SUPPRESS)
    rag_sub.add_parser("status", help="查看后台索引构建状态")
    rag_sub.add_parser("info", help="查看已加载索引的概览（文档数 / 链路分布 / 模型）")

    rag_eval = rag_sub.add_parser(
        "eval-retrieval", help="检索指标对照：Recall@k / MRR@k / nDCG@k"
    )
    rag_eval.add_argument("--k", type=int, default=5, help="评测截断 k（默认 5）")
    rag_eval.add_argument(
        "--modes",
        action="append",
        default=[],
        help="只跑指定模式（sparse/dense/hybrid/hybrid+rerank，可重复；默认四种全跑）",
    )
    rag_eval.add_argument(
        "--hash-embedder",
        action="store_true",
        help="用 hash 兜底嵌入（离线快速；**指标口径会降级**，不得与真实模型口径混比）",
    )
    rag_eval.add_argument(
        "--by-journey",
        action="store_true",
        help="按用户链路分组汇总（只看每条链路的指标，不输出逐条用例）",
    )
    rag_eval.add_argument("--verbose", action="store_true", help="输出逐条用例明细")

    evaluation = sub.add_parser("eval", help="端到端评估")
    eval_sub = evaluation.add_subparsers(dest="eval_command", required=True)
    eval_run = eval_sub.add_parser("run", help="跑评估用例并生成报告")
    eval_run.add_argument("--report", default="out/report.md", help="Markdown 报告路径")
    eval_run.add_argument("--k", type=int, default=1, help="每条用例重复次数（Pass^k）")
    eval_run.add_argument(
        "--provider", default="fake", help="模型供应商；默认 fake（确定性、离线）"
    )
    eval_run.add_argument("--model", default=None, help="模型 id（可选）")
    eval_run.add_argument(
        "--no-isolate",
        action="store_true",
        help="使用演示库跑评估（默认创建临时库，避免污染演示数据）",
    )
    eval_run.add_argument(
        "--permissions",
        action="append",
        default=[],
        help="评估时注入的调用方权限（可重复）",
    )
    eval_comp = eval_sub.add_parser(
        "context-compression", help="压缩前后对照（字符 / token / 保持率）"
    )
    eval_comp.add_argument("--turns", type=int, default=25)
    eval_comp.add_argument("--report", default="out/compression.md")

    traces = sub.add_parser("traces", help="Trace 查看")
    traces_sub = traces.add_subparsers(dest="traces_command", required=True)
    traces_show = traces_sub.add_parser("show", help="按 trace_id 打印事件与失败线索")
    traces_show.add_argument("trace_id")
    traces_show.add_argument("--dir", default=None, help="覆盖 trace 目录（默认 data/traces）")

    return parser


def _settings_overrides(args: argparse.Namespace) -> dict[str, object]:
    overrides: dict[str, object] = {}
    if getattr(args, "industry", None):
        overrides["industry_id"] = args.industry
    if getattr(args, "backend", None):
        overrides["backend"] = args.backend
    if getattr(args, "http_backend_url", None):
        overrides["http_backend_url"] = args.http_backend_url
    permissions = getattr(args, "permissions", None)
    if permissions:
        overrides["permissions"] = permissions
    return overrides


def _cmd_db(args: argparse.Namespace) -> int:
    settings = load_settings(**_settings_overrides(args))
    paths = resolve_paths(settings)
    pack = load_industry_pack(paths.industry_pack_dir)
    db = Database(paths.db_path)

    if args.db_command == "reset":
        db.reset()
        print(json.dumps({"ok": True, "action": "db reset", "db": str(paths.db_path),
                          "tables": db.table_names()}, ensure_ascii=False))
        return 0

    if args.db_command == "seed":
        db.initialize()
        seed_dir = pack_file(paths.industry_pack_dir, pack.seed_data)
        counts = seed_database(db, seed_dir)
        print(json.dumps({"ok": True, "action": "db seed", "industry": pack.id,
                          "seeded": counts}, ensure_ascii=False))
        return 0

    if args.db_command == "check":
        present = set(db.table_names())
        missing = [table for table in REQUIRED_TABLES if table not in present]
        payload = {
            "ok": not missing,
            "industry": pack.id,
            "db": str(paths.db_path),
            "present": len(present),
            "missing": missing,
        }
        if missing:
            payload["hint"] = (
                f"zkagent --industry {pack.id} db seed"
                "（旧库不会自动补列，必要时先 db reset）"
            )
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0 if not missing else 1

    if args.db_command == "inspect":
        try:
            rows = db.rows(args.table, limit=args.limit)
        except ValueError as exc:
            print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
            return 2
        payload = {"ok": True, "table": args.table, "count": db.count(args.table), "rows": rows}
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
        return 0

    raise AssertionError(f"未处理的子命令：{args.db_command}")  # pragma: no cover


def _cmd_tools(args: argparse.Namespace) -> int:
    """列出（可按 tag 过滤的）已注册工具。

    **必须走组合后端**（业务后端 + RAG 工具）而不是单独的 ``MockBackend``：
    ``knowledge_search`` 由 Core 的 RAG 提供，只看业务后端会误报
    「启用的工具未提供」（P3 改 `tools.yaml` 时曾因此打破 P2 Gate）。
    ``Container.build`` 本身会在「声明启用但没有任何来源提供」时显式报错，
    因此这里不需要重复做缺失检查——那份检查现在由组合后的清单统一负责。
    """
    if args.tools_command != "list":
        raise AssertionError(f"未处理的子命令：{args.tools_command}")  # pragma: no cover

    container = Container.build(load_settings(**_settings_overrides(args)))
    registry = container.registry
    tools_config = container.tools_config

    tags = args.tag or None
    limit = args.max if args.max is not None else tools_config.injection.max_tools
    selected = registry.select(
        tags=tags,
        max_tools=limit,
        always_include=tools_config.injection.always_include,
    )
    payload = {
        "ok": True,
        "industry": container.pack.id,
        "registered": len(registry),
        "selected": len(selected),
        "injection_max_tools": tools_config.injection.max_tools,
        "tools": [
            {"name": spec.name, "risk": spec.risk, "tags": spec.tags,
             "requires_idempotency_key": spec.requires_idempotency_key,
             "confirmation_required": spec.confirmation_required}
            for spec in selected
        ],
    }
    if args.inject_preview:
        # 核对「全集 / 注入」：max_tools 必须真的生效，且核心工具不能缺席
        enabled = len(tools_config.enabled)
        always = tools_config.injection.always_include
        selected_names = {spec.name for spec in selected}
        absent = [name for name in always if name not in selected_names]
        # 显式把上限压到核心清单以下时，缺席是预算使然，不是配置漏配
        truncated_core = absent if limit < len(always) else []
        payload["inject_preview"] = {
            "enabled": enabled,
            "injected": len(selected),
            "ratio": round(len(selected) / max(1, enabled), 3),
            "always_include": always,
            "missing_core": [] if truncated_core else absent,
            "truncated_core": truncated_core,
        }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def _cmd_providers(args: argparse.Namespace) -> int:
    """列出内置供应商与本机密钥是否就位（**绝不打印密钥值**）。"""
    if args.providers_command != "list":
        raise AssertionError(f"未处理的子命令：{args.providers_command}")  # pragma: no cover

    settings = load_settings(**_settings_overrides(args))
    catalog = provider_catalog_with_keys(settings)
    # auto 只会在「密钥就位」的供应商里挑（顺序见 AUTO_PRIORITY）；本地端点不参与
    auto_candidates = [
        entry["provider_id"]
        for entry in catalog
        if entry["key_present"] and entry["provider_id"] in AUTO_PRIORITY
    ]
    keyless = [entry["provider_id"] for entry in catalog if not entry["requires_api_key"]]
    print(json.dumps({
        "ok": True,
        "default_provider": settings.provider,
        "auto_order": list(AUTO_PRIORITY),
        "auto_candidates": auto_candidates,
        "keyless_providers": keyless,
        "providers": catalog,
    }, ensure_ascii=False, indent=2))
    return 0


def _start_background_index(container: Container, args: argparse.Namespace) -> int:
    """启动子进程构建索引，父进程立即返回（M14）。"""
    index_dir = container.paths.index_dir_for(container.pack.id)
    index_dir.mkdir(parents=True, exist_ok=True)
    write_status(
        index_dir,
        status="queued",
        industry=container.pack.id,
        rebuild=bool(args.rebuild),
        builder=args.hash_embedder and "hash" or "local",
    )
    command = [
        sys.executable,
        "-m",
        "apps.cli",
        "--industry",
        container.pack.id,
        "rag",
        "index",
        "--status-writer",
    ]
    if args.rebuild:
        command.append("--rebuild")
    if args.hash_embedder:
        command.append("--hash-embedder")
    log_path = index_dir / "build.log"
    with log_path.open("ab") as log:
        process = subprocess.Popen(
            command,
            cwd=str(container.paths.project_root),
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    write_status(index_dir, status="queued", pid=process.pid, log=str(log_path))
    print(
        json.dumps(
            {
                "ok": True,
                "background": True,
                "pid": process.pid,
                "status_file": str(index_dir / "build_status.json"),
                "log": str(log_path),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def _cmd_rag(args: argparse.Namespace) -> int:
    """知识索引：构建 / 增量更新 / 检索指标对照（P3）。"""
    container = Container.build(load_settings(**_settings_overrides(args)))

    if args.rag_command == "index":
        if args.background:
            return _start_background_index(container, args)
        if args.dry_run and args.hash_embedder:
            print(
                "注意：--hash-embedder 与 --dry-run 同用会把全部文档报成 added"
                "（换嵌入模型触发全量重算，不是知识全改了）",
                file=sys.stderr,
            )
        index_dir = container.paths.index_dir_for(container.pack.id)
        if args.status_writer:
            write_status(index_dir, status="running", industry=container.pack.id, pid=os.getpid())
        try:
            report = container.index_knowledge(
                rebuild=args.rebuild, dry_run=args.dry_run, force_hash=args.hash_embedder
            )
        except Exception as exc:
            if args.status_writer:
                write_status(index_dir, status="failed", error=f"{type(exc).__name__}: {exc}")
            raise
        if args.status_writer:
            write_status(
                index_dir,
                status="succeeded",
                documents=sum(
                    len(report.get(key) or [])
                    for key in ("added", "modified", "unchanged")
                ),
                chunks=report.get("chunk_total"),
                removed=len(report.get("removed") or []),
                degraded=report.get("degraded"),
            )
        print(json.dumps({"ok": True, **report}, ensure_ascii=False, indent=2))
        if report["degraded"]:
            # 降级不是失败，但必须让人看见：否则降级口径会被误当成正常口径
            print(f"注意：嵌入已降级 —— {report['embedder']}", file=sys.stderr)
        return 0

    if args.rag_command == "status":
        data = read_status(container.paths.index_dir_for(container.pack.id))
        if data is None:
            print(json.dumps({"ok": False, "error": "没有后台构建记录"}, ensure_ascii=False))
            return 1
        print(json.dumps({"ok": True, **data}, ensure_ascii=False, indent=2))
        return 0

    if args.rag_command == "info":
        retriever, bundle = container.build_retriever()
        retriever.load()
        payload = dict(retriever.stats())
        payload.update(
            {
                "ok": True,
                "degraded": bundle.mode != "local_model",
                "embedder": bundle.embedder.model_id,
            }
        )
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    if args.rag_command == "eval-retrieval":
        report = container.evaluate_retrieval(
            k=args.k, modes=args.modes or None, force_hash=args.hash_embedder
        )
        runs = []
        for run in report["runs"]:
            compact = {key: value for key, value in run.items() if key != "per_case"}
            # --by-journey：只保留汇总 + 链路分组，逐条明细反而会淹没分组结论
            if args.verbose and not args.by_journey:
                compact["per_case"] = run["per_case"]
            runs.append(compact)
        payload = {key: value for key, value in report.items() if key != "runs"}
        print(json.dumps({"ok": True, **payload, "runs": runs}, ensure_ascii=False, indent=2))
        if report["degraded"]:
            print(
                f"注意：指标为**降级口径**，不得与真实模型口径混比 —— {report['embedder']}",
                file=sys.stderr,
            )
        return 0

    raise AssertionError(f"未处理的子命令：{args.rag_command}")  # pragma: no cover


def _cmd_chat(args: argparse.Namespace) -> int:
    """单轮任务、多轮交互，或从人工审批恢复。"""
    container = Container.build(load_settings(**_settings_overrides(args)))
    if args.resume:
        return _resume_from_approval(container, args)

    session = args.session or "cli-session"
    channel = CliChannel(session_id=session, user_id=args.user)
    tags = args.tag or None

    def one_turn(text: str) -> int:
        try:
            runner, ctx = container.make_runner(
                user_id=args.user,
                session_id=session,
                provider=args.provider,
                task=text,
                model=args.model,
                base_url=args.base_url,
                tool_tags=tags,
                max_steps=args.max_steps,
                intent=getattr(args, "intent", None),
            )
        except (IndustryPackError, ProviderConfigError, ProviderCallError) as exc:
            print(json.dumps(_error_payload(exc), ensure_ascii=False), file=sys.stderr)
            return 4
        result = runner.run(session, text, ctx)
        container.tracer.flush(result.trace_id or ctx.trace_id)
        channel.send(
            OutboundMessage(
                session_id=session,
                text=channel.render_turn(result),
                awaiting_confirmation=result.status == "awaiting_confirmation",
                metadata={"status": result.status, "trace_id": result.trace_id},
            )
        )
        return 0 if result.status in {"completed", "awaiting_confirmation"} else 1

    if args.task:
        return one_turn(args.task)

    print("进入多轮对话；输入 exit 或 quit 结束。", file=sys.stderr)
    while True:
        try:
            inbound = channel.receive()
        except EOFError:
            break
        if not inbound.text:
            continue
        one_turn(inbound.text)
    return 0


def _approver_identity(actor: str | None, token: str | None) -> tuple[str, str]:
    """解析审批人身份：**只信环境变量**，绝不接受对话里自称的管理员。

    - 配置了 ``HITL_APPROVERS=alice:<token>,bob:<token>``：令牌必须匹配；
    - 未配置：进入单机开发模式，``approver_mode=dev_unauthenticated``，
      调用方必须把该值原样展示——不能让人误以为已鉴权。
    """
    raw = os.environ.get("HITL_APPROVERS", "").strip()
    if not raw:
        return (actor or "local-dev"), "dev_unauthenticated"

    pairs: dict[str, str] = {}
    for item in raw.split(","):
        name, _, secret = item.partition(":")
        if name.strip() and secret:
            pairs[name.strip()] = secret
    if not actor or not token or pairs.get(actor) != token:
        raise ApprovalError("APPROVER_UNAUTHORIZED", "审批人未通过令牌校验")
    return actor, "token"


def _approver_permissions(
    actor: str, requested: list[str] | None, settings
) -> set[str]:
    """审批人权限：显式 `--permissions` 优先，其次 `HITL_APPROVER_PERMISSIONS` 映射。"""
    if requested:
        return {item for item in requested if item}
    raw = str(getattr(settings, "hitl_approver_permissions", "") or "").strip()
    if not raw:
        return set()
    for entry in raw.split(","):
        name, _, permissions = entry.partition("=")
        if name.strip() != actor:
            continue
        normalized = permissions.replace(";", "|").replace(",", "|")
        return {item.strip() for item in normalized.split("|") if item.strip()}
    return set()


def _cmd_approvals(args: argparse.Namespace) -> int:
    """列出 / 批准 / 拒绝审批单。"""
    container = Container.build(load_settings(**_settings_overrides(args)))
    store = container.approval_store

    if args.approvals_command == "list":
        rows = store.list_pending(limit=args.limit)
        payload = {
            "ok": True,
            "count": len(rows),
            "hint": "批准后执行 `zkagent chat --resume <approval_id>` 才会真正执行",
            "items": [
                {
                    "approval_id": item.approval_id,
                    "action": item.action,
                    "reason": item.reason,
                    "rule_id": item.rule_id,
                    "requester_id": item.requester_id,
                    "session_id": item.session_id,
                    "created_at": item.created_at,
                    "params_fingerprint": item.params_fingerprint,
                }
                for item in rows
            ],
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    record = store.get(args.approval_id)
    if record is None:
        raise ApprovalError("APPROVAL_NOT_FOUND", args.approval_id)
    if record.status != "pending":
        raise ApprovalError("APPROVAL_ALREADY_RESOLVED", f"{record.approval_id} 当前为 {record.status}")

    ttl = container.policy_engine.settings.approval_ttl_hours
    if is_expired(record, ttl):
        raise ApprovalError("APPROVAL_EXPIRED", f"{record.approval_id} 已超过 {ttl} 小时")

    actor, mode = _approver_identity(args.actor, args.token)
    if container.policy_engine.settings.four_eyes and actor == record.requester_id:
        raise ApprovalError("SELF_APPROVAL_FORBIDDEN", "审批人不得与申请人同人")

    approve = args.approvals_command == "approve"
    if approve and record.required_permissions:
        missing = set(record.required_permissions) - _approver_permissions(
            actor, getattr(args, "permissions", None), container.settings
        )
        if missing:
            raise ApprovalError(
                "APPROVER_UNAUTHORIZED",
                f"审批人 {actor} 缺少权限：{', '.join(sorted(missing))}",
            )
    updated = store.resolve(
        record.approval_id,
        approve=approve,
        approver_id=actor,
        approver_mode=mode,
        approved_payload_json=record.payload_json if approve else None,
    )
    print(
        json.dumps(
            {
                "ok": True,
                "approval_id": updated.approval_id,
                "status": updated.status,
                "approver_id": updated.approver_id,
                "approver_mode": updated.approver_mode,
                "execution_status": updated.execution_status,
                "next": (
                    f"zkagent chat --resume {updated.approval_id}" if approve else None
                ),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def _resume_from_approval(container: Container, args: argparse.Namespace) -> int:
    """审批通过（或拒绝）后恢复图执行；审批状态与业务结果分别如实回显。"""
    store = container.approval_store
    record = store.get(args.resume)
    if record is None:
        raise ApprovalError("APPROVAL_NOT_FOUND", args.resume)
    if record.status == "pending":
        print(
            json.dumps(
                {"ok": False, "error": "审批尚未完成，请先执行 approvals approve/reject"},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 2

    session = args.session or record.session_id or "cli-session"
    runner, _ctx = container.make_runner(
        user_id=record.requester_id,
        session_id=session,
        provider=args.provider,
        model=args.model,
        max_steps=args.max_steps,
    )
    decision = PolicyDecision(
        action=record.action,
        allowed=record.status == "approved",
        decision="require_confirmation",
        reason=record.reason,
        rule_id=record.rule_id,
        approval_id=record.approval_id,
        approver_id=record.approver_id,
        approver_mode=record.approver_mode,
    )
    result = runner.resume(session, decision)
    container.tracer.flush(result.trace_id or "")
    final = store.get(record.approval_id)
    print(
        json.dumps(
            {
                "ok": result.status in {"completed", "awaiting_confirmation"},
                "status": result.status,
                "approval_id": record.approval_id,
                "execution_status": final.execution_status if final is not None else None,
                "execution_error_code": final.execution_error_code if final is not None else None,
                "final_text": result.final_text,
                "tool_calls": result.tool_calls,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if result.status in {"completed", "awaiting_confirmation"} else 1


def _embedding_mode(container: Container) -> str:
    """从索引清单读嵌入模型 id；索引不存在时返回空串（报告标注 n/a）。"""
    path = container.paths.index_root / container.pack.id / "manifest.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    return str(data.get("embedding_model_id") or data.get("embed_model_id") or "")


def _cmd_eval(args: argparse.Namespace) -> int:
    """跑端到端评估并出报告（默认 Fake，离线可复现）。"""
    container = Container.build(load_settings(**_settings_overrides(args)))

    if args.eval_command == "context-compression":
        data = run_context_compression(turns=args.turns)
        lines = [
            "# 压缩前后对照",
            "",
            "| 指标 | 值 |",
            "| --- | --- |",
            f"| 轮数 | {data['turns']} |",
            f"| 压缩前字符 | {data['chars_before']} |",
            f"| 压缩后字符 | {data['chars_after']} |",
            f"| 保留比例 | {data['ratio']:.2%} |",
            f"| 估算 token（前 → 后） | {data['estimated_tokens_before']} → {data['estimated_tokens_after']} |",
            f"| 被摘要轮数 | {data['summarized_turns']} |",
            f"| 关键信息保持 | {'通过' if data['retention_passed'] else '失败'} |",
            "",
        ]
        target = Path(args.report)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("\n".join(lines), encoding="utf-8")
        target.with_suffix(".json").write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(json.dumps({"ok": data["retention_passed"], **data, "report": str(target)},
                         ensure_ascii=False, indent=2))
        return 0 if data["retention_passed"] else 1

    cases_path = container.paths.industry_pack_dir / "evals" / "cases.yaml"
    cases = load_cases(cases_path)
    validate_cases(cases, minimum=20)

    runner = EvalRunner(
        container, provider=args.provider, k=args.k, isolate=not args.no_isolate
    )
    try:
        results = runner.run_all(cases)
    finally:
        runner.close()
    # 报告里的 model_id 要是**实际模型**（如 deepseek-chat），否则价格表查不到，
    # 会把真实调用的成本显示成 0（不是「免费」，而是「没算」）。
    try:
        model_id = args.model or container.resolve_model_handle(
            provider=args.provider
        ).model_id
    except (ProviderConfigError, IndustryPackError):
        model_id = args.model or args.provider
    usage = summarize_usage(
        results, model_id=model_id, estimated=(args.provider == "fake")
    )
    markdown = render_markdown(
        results,
        usage=usage,
        industry=container.pack.id,
        provider=args.provider,
        model_id=model_id,
        k=args.k,
        embedding_mode=_embedding_mode(container),
        db_path=runner.db_path,
    )
    md_path, json_path = write_report(
        args.report,
        markdown,
        results,
        usage=usage,
        meta={
            "industry": container.pack.id,
            "provider": args.provider,
            "model_id": model_id,
            "k": args.k,
            "embedding_mode": _embedding_mode(container),
            "db_path": runner.db_path,
            "isolated": not args.no_isolate,
        },
    )
    passed = sum(1 for item in results if item.passed)
    print(
        json.dumps(
            {
                "ok": passed == len(results),
                "industry": container.pack.id,
                "cases": len(results),
                "passed": passed,
                "pass_at_1": round(
                    sum(1 for item in results if item.attempts and item.attempts[0])
                    / max(1, len(results)),
                    4,
                ),
                "pass_at_k": round(passed / max(1, len(results)), 4),
                "usage": usage.model_dump(),
                "report": str(md_path),
                "report_json": str(json_path),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if passed == len(results) else 1


def _cmd_traces(args: argparse.Namespace) -> int:
    """读取落盘 trace，打印节点/工具事件与失败线索。"""
    container = Container.build(load_settings(**_settings_overrides(args)))
    base = Path(args.dir) if args.dir else container.paths.trace_dir
    path = base / f"{args.trace_id}.json"
    if not path.is_file():
        print(json.dumps({"ok": False, "error": f"trace 不存在：{path}"}, ensure_ascii=False))
        return 1

    payload = json.loads(path.read_text(encoding="utf-8"))
    events = payload.get("events") or []
    for event in events:
        fields = " ".join(
            f"{key}={value}"
            for key, value in event.items()
            if key not in {"at", "event"} and value not in (None, "")
        )
        print(f"[{event.get('at', '-')}] {event.get('event', '-')} {fields}".rstrip())

    failed = [
        event
        for event in events
        if event.get("event") == "tool_call_end" and event.get("ok") is False
    ]
    print()
    if failed:
        print("失败线索：")
        for event in failed:
            print(
                f"  - 工具 {event.get('tool')} 失败：{event.get('error_code') or 'UNKNOWN'}"
            )
    else:
        print("未发现失败的 tool_call_end 事件。")
    return 0


def main(argv: list[str] | None = None) -> int:
    """命令行入口。"""
    args = build_parser().parse_args(argv)
    try:
        if args.command == "db":
            return _cmd_db(args)
        if args.command == "tools":
            return _cmd_tools(args)
        if args.command == "providers":
            return _cmd_providers(args)
        if args.command == "rag":
            return _cmd_rag(args)
        if args.command == "chat":
            return _cmd_chat(args)
        if args.command == "approvals":
            return _cmd_approvals(args)
        if args.command == "eval":
            return _cmd_eval(args)
        if args.command == "traces":
            return _cmd_traces(args)
    except ApprovalError as exc:
        print(
            json.dumps({"ok": False, "error_code": exc.code, "error": exc.message},
                       ensure_ascii=False),
            file=sys.stderr,
        )
        return 3
    except (EvalCaseError, IndustryPackError, ProviderConfigError, ProviderCallError, RagError) as exc:
        print(json.dumps(_error_payload(exc), ensure_ascii=False), file=sys.stderr)
        return 4
    except FileNotFoundError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 4
    raise AssertionError(f"未处理的命令：{args.command}")  # pragma: no cover


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
