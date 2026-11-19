"""跑 ``homeapp`` 的业务场景用例（真实模型，端到端）。

与 ``zkagent rag eval-retrieval`` 的分工：那边只测检索，这边测「工具选对了没、数字说对了没、
该拒的拒了没」。用例定义见 ``apps/pack_templates/homeapp/evals/business_cases.yaml``
（打包后会出现在 ``zkagent/industry_packs/homeapp/evals/``）。

用法：

```bash
python -m scripts.run_business_cases --industry homeapp            # 全跑
python -m scripts.run_business_cases --only biz-presale-param      # 单条
python -m scripts.run_business_cases --dry-run                     # 只校验用例，不调模型
```

退出码：0 = 自动用例全过；1 = 有失败；2 = 环境/配置错误。

``manual_review: true`` 的用例只打印回答，不计入通过率——它们的「正确答案」依赖语境
（例如知识库只写「按保修卡为准」，模型措辞无法用关键词判定）。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from zkagent.config import Container, IndustryPackError, default_industry_packs_dir, load_settings

__all__ = ["main"]

DEFAULT_PACK = "homeapp"


def _norm(text: str) -> str:
    """归一化后再比对：模型写「10 公斤」「一级能效」也要能命中。"""
    normalized = text.replace(" ", "").replace("\u3000", "").lower()
    for source, target in (
        ("公斤", "kg"),
        ("千克", "kg"),
        ("一级", "1级"),
        ("二级", "2级"),
        ("三级", "3级"),
    ):
        normalized = normalized.replace(source, target)
    return normalized


def _load_cases(pack_dir: Path) -> list[dict[str, Any]]:
    path = pack_dir / "evals" / "business_cases.yaml"
    if not path.is_file():
        raise FileNotFoundError(f"缺少业务用例文件：{path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return list(data.get("cases") or [])


#: 用例里出现这些数据引用时，需要先过身份核验——把手机尾号直接写进提问，
#: 模拟「用户已在对话里报过尾号」，否则模型会停在「请提供尾号」而测不到后续工具。
_IDENTITY_PREFIXES = ("orders:", "tickets:", "refunds:", "invoices:", "logistics:")


def _factors(pack_dir: Path) -> dict[str, str]:
    """从种子 ``users.yaml`` 取每个用户的手机尾号（后四位即校验码）。"""
    path = pack_dir / "seed_data" / "users.yaml"
    if not path.is_file():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return {
        row["user_id"]: str(row["phone_masked"])[-4:]
        for row in (data.get("users") or [])
    }


def _judge(case: dict[str, Any], text: str, tools: list[str], status: str) -> list[str]:
    """返回失败原因列表；空列表 = 通过。"""
    expect = case.get("expect") or {}
    failures: list[str] = []

    any_tool = expect.get("any_tool") or []
    if any_tool and not set(any_tool) & set(tools):
        failures.append(f"未调用期望工具（期望其一：{any_tool}，实际：{tools}）")

    normalized = _norm(text)
    for needle in expect.get("must_include") or []:
        if _norm(needle) not in normalized:
            failures.append(f"缺少必答内容：{needle!r}")

    any_of = expect.get("must_include_any") or []
    if any_of and not any(_norm(item) in normalized for item in any_of):
        failures.append(f"未命中任一必答内容：{any_of}")

    for needle in expect.get("must_not_include") or []:
        if _norm(needle) in normalized:
            failures.append(f"出现禁止内容：{needle!r}")

    if expect.get("confirmation") and status != "awaiting_confirmation":
        failures.append(f"写操作应先确认，实际状态：{status}")

    return failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="跑 homeapp 业务场景用例（真实模型）")
    parser.add_argument("--industry", default=DEFAULT_PACK)
    parser.add_argument("--only", default=None, help="只跑指定用例 id")
    parser.add_argument("--dry-run", action="store_true", help="只校验用例结构，不调模型")
    parser.add_argument("--provider", default=None)
    parser.add_argument("--model", default=None)
    parser.add_argument("--max-steps", type=int, default=6)
    args = parser.parse_args(argv)

    pack_dir = default_industry_packs_dir() / args.industry
    try:
        cases = _load_cases(pack_dir)
    except FileNotFoundError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    if args.only:
        cases = [case for case in cases if case["id"] == args.only]
        if not cases:
            print(json.dumps({"ok": False, "error": f"没有该用例：{args.only}"}, ensure_ascii=False))
            return 2

    if args.dry_run:
        print(json.dumps({"ok": True, "industry": args.industry, "cases": len(cases),
                          "ids": [case["id"] for case in cases]}, ensure_ascii=False, indent=2))
        return 0

    settings = load_settings(
        industry_id=args.industry,
        provider=args.provider or "auto",
        model_id=args.model,
    )
    try:
        container = Container.build(settings)
    except Exception as exc:  # noqa: BLE001 - 启动失败要给出可读原因，而不是堆栈
        print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False),
              file=sys.stderr)
        return 2

    # 每轮独立会话：复用同一个 session_id 会从 checkpoint 恢复上一轮的对话，
    # 导致「这次没调工具也能答对」——那测的就不是本轮能力了。
    run_stamp = datetime.now().strftime("%H%M%S")
    factors = _factors(pack_dir)
    results: list[dict[str, Any]] = []
    failed = 0
    for case in cases:
        user_id = case.get("as_user", "H1")
        session_id = f"biz-{case['id']}-{run_stamp}"
        ask = case["ask"]
        needs_identity = any(
            str(ref).startswith(_IDENTITY_PREFIXES) for ref in (case.get("data") or [])
        )
        factor = factors.get(user_id)
        if needs_identity and factor:
            ask = f"我是本账号用户，手机尾号 {factor}。{ask}"
        runner, ctx = container.make_runner(
            user_id=user_id,
            session_id=session_id,
            provider=args.provider,
            task=ask,
            model=args.model,
            tool_tags=None,
            max_steps=args.max_steps,
        )
        turn = runner.run(session_id, ask, ctx)
        tools = [call.get("name", "") for call in turn.tool_calls]
        failures = _judge(case, turn.final_text, tools, turn.status)

        manual = bool(case.get("manual_review"))
        passed = not failures
        if not passed and not manual:
            failed += 1
        results.append(
            {
                "id": case["id"],
                "journey": case["journey"],
                "as_user": user_id,
                "status": turn.status,
                "tools": tools,
                "passed": passed,
                "manual_review": manual,
                "failures": failures,
                "answer": turn.final_text,
            }
        )
        flag = "PASS" if passed else ("MANUAL" if manual else "FAIL")
        print(f"[{flag}] {case['id']}  tools={tools}  status={turn.status}")
        if failures:
            print(f"        {'; '.join(failures)}")

    summary = {
        "ok": failed == 0,
        "total": len(results),
        "auto_failed": failed,
        "manual": sum(1 for item in results if item["manual_review"]),
        "passed": sum(1 for item in results if item["passed"]),
        "results": results,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
