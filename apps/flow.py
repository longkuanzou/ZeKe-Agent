"""交互式数据流面板：一个问题的每一段处理都能看见。

## 为什么是一个独立进程

``zkagent chat`` 是给人用的命令行；这个面板是给**观察**用的：它要把
「实体识别 → 链路路由 → 召回 → 融合 → 相关性闸门 → 过滤 → 精排 → 上下文 → 生成 → 引用校验」
每一段的**真实中间值**吐到页面上。因此它不复用 Agent 图，而是把同一条流水线拆开显式执行。

## 数据来源（全部真实，不造演示数据）

| 段 | 来源 |
| --- | --- |
| 知识 | 行业包 ``homeapp``（由 ``appliance-kb`` 打包而来，1622 片，front-matter 自带出处） |
| 召回 | ``LlamaIndexRetriever.search(trace=...)``：稀疏 / 稠密 / RRF / 闸门 / 过滤 / 精排 |
| 路由 | ``routing/prototypes.yaml`` 的链路原型问句 + 当前嵌入模型做最近邻 |
| 实体 | ``routing/entities.yaml``（型号别名已剔除跨型号歧义） |
| 生成 | 真实模型（``auto`` 按密钥选择），流式输出 |
| 校验 | ``evals/answer_checks.json`` 的 must_include / must_not_include + 引用是否来自本轮检索 |

## 启动

```bash
python -m apps.flow --port 8000            # 打开 http://127.0.0.1:8000
python -m apps.flow --build-index          # 索引缺失时先构建（约数分钟）
```
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml

from zkagent.config import (
    Container,
    IndustryPackError,
    load_settings,
    pack_file,
    resolve_paths,
)
from zkagent.core.llm import ProviderConfigError, auto_select_provider
from zkagent.core.orchestrator.langgraph_adapter import stream_chat
from zkagent.core.rag.manifest import load_manifest, scan_documents
from zkagent.core.rag.metadata import JOURNEY_LABELS

__all__ = ["Panel", "main"]

#: 页面文件（单文件、零外部依赖，便于离线打开）。
WEB_DIR = Path(__file__).with_name("web")

#: 引用标记：回答里的 ``[1]`` ``[2]``。
CITATION_PATTERN = re.compile(r"\[(\d{1,2})\]")

#: 送进模型的上下文上限（字符）。超过就按片段整块截断，不切半个片段。
MAX_CONTEXT_CHARS = 6000


# --------------------------------------------------------------------- 装配


def _load_yaml(path: Path) -> Any:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


@dataclass
class Panel:
    """面板的服务端状态：索引、路由原型、实体表、评测校验集、模型句柄。"""

    pack_id: str = "homeapp"
    settings: Any = None
    container: Any = None
    retriever: Any = None
    bundle: Any = None
    system_prompt: str = ""
    prototypes: list[dict[str, Any]] = field(default_factory=list)
    prototype_vectors: list[list[float]] = field(default_factory=list)
    intents: list[dict[str, Any]] = field(default_factory=list)
    entities: dict[str, Any] = field(default_factory=dict)
    checks: list[dict[str, Any]] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)
    index_meta: dict[str, Any] = field(default_factory=dict)
    llm: dict[str, Any] = field(default_factory=dict)
    rerank_enabled: bool = False
    lock: threading.Lock = field(default_factory=threading.Lock)

    # ------------------------------------------------------------------ 装配

    @classmethod
    def build(
        cls,
        *,
        pack_id: str = "homeapp",
        rerank: bool = False,
        build_index: bool = False,
    ) -> "Panel":
        settings = load_settings(industry_id=pack_id)
        container = Container.build(settings)
        pack_dir = resolve_paths(settings, create=False).industry_pack_dir
        if not pack_dir.is_dir():
            raise SystemExit(
                f"行业包不存在：{pack_dir}\n先执行：python -m apps.build_kb_pack --kb appliance-kb --pack {pack_id}"
            )

        retriever, bundle = container.build_retriever(with_reranker=rerank)
        manifest = load_manifest(retriever.index_dir)
        if manifest is None:
            if not build_index:
                raise SystemExit(
                    f"索引不存在：{retriever.index_dir}\n"
                    f"先执行：python -m apps.flow --build-index   （或 zkagent rag index --industry {pack_id}）"
                )
            print("索引不存在，开始构建（首次约数分钟）……", file=sys.stderr)
            report = retriever.index(rebuild=True)
            print(f"索引构建完成：{report.chunk_total} 个分块", file=sys.stderr)
            manifest = load_manifest(retriever.index_dir)

        retriever.load()
        stats = retriever.stats()

        prototypes_doc = _load_yaml(pack_file(pack_dir, "routing/prototypes.yaml"))
        prototypes = list(prototypes_doc.get("prototypes") or [])
        vectors = bundle.embedder.embed([item["question"] for item in prototypes]) if prototypes else []

        entities = _load_yaml(pack_file(pack_dir, "routing/entities.yaml"))
        lexicon_path = pack_file(pack_dir, "routing/intent_lexicon.yaml")
        lexicon = _load_yaml(lexicon_path) if lexicon_path.is_file() else {"intents": []}
        intents = list(lexicon.get("intents") or [])
        checks_path = pack_file(pack_dir, "evals/answer_checks.json")
        checks = json.loads(checks_path.read_text(encoding="utf-8")).get("items", [])

        system_prompt = pack_file(pack_dir, "prompts/system.md").read_text(encoding="utf-8")

        provider = auto_select_provider(dict(os.environ)) or "fake"
        panel = cls(
            pack_id=pack_id,
            settings=settings,
            container=container,
            retriever=retriever,
            bundle=bundle,
            system_prompt=system_prompt,
            prototypes=prototypes,
            prototype_vectors=[list(vec) for vec in vectors],
            intents=intents,
            entities=entities,
            checks=checks,
            stats=stats,
            rerank_enabled=rerank,
            llm={
                "provider": provider,
                "model": settings.model_id or "(供应商默认)",
                "ready": provider != "fake",
            },
            index_meta={
                "built_at": manifest.built_at if manifest else None,
                "docs": len(manifest.docs) if manifest else 0,
            },
        )
        panel._refresh_staleness()
        return panel

    # ------------------------------------------------------------------ 元信息

    def _refresh_staleness(self) -> None:
        """索引是否落后于知识目录（改了文档但没重建）。"""
        try:
            current = scan_documents(self.retriever.knowledge_dir)
            manifest = self.retriever.manifest
            indexed = {entry.doc_id: entry.content_sha256 for entry in manifest.docs} if manifest else {}
            changed = [
                doc_id
                for doc_id, digest in current.items()
                if indexed.get(doc_id) != digest
            ]
            removed = [doc_id for doc_id in indexed if doc_id not in current]
            self.index_meta["stale"] = bool(changed or removed)
            self.index_meta["stale_count"] = len(changed) + len(removed)
            self.index_meta["docs_on_disk"] = len(current)
        except Exception:  # 面板不该因为一致性检查而不可用
            self.index_meta["stale"] = None

    def meta(self) -> dict[str, Any]:
        """页面初始化数据。"""
        appliances = []
        for item in self.entities.get("appliances") or []:
            appliances.append(
                {
                    "key": item.get("key"),
                    "label": item.get("label") or item.get("key"),
                    "models": item.get("models", 0),
                    "words": item.get("words") or [],
                }
            )
        models = [
            {
                "product_id": item.get("product_id"),
                "label": item.get("label"),
                "appliance": item.get("appliance"),
                "model": item.get("model"),
                "colloquial": item.get("colloquial"),
            }
            for item in self.entities.get("models") or []
        ]
        journeys = []
        for key, count in (self.stats.get("journeys") or {}).items():
            # "unclassified" 在面板上叫「通用文档」：它们是身份卡，跨链路都该可见
            label = "通用文档（身份卡）" if key == "unclassified" else JOURNEY_LABELS.get(key, key)
            journeys.append({"key": key, "label": label, "docs": count})

        examples: dict[str, list[str]] = {}
        for item in self.prototypes:
            examples.setdefault(item["journey"], [])
            if len(examples[item["journey"]]) < 4:
                examples[item["journey"]].append(item["question"])

        return {
            "pack": {"id": self.pack_id, "docs": self.stats.get("docs"), "chunks": self.stats.get("chunks")},
            "embedder": {
                "mode": self.stats.get("embedding_mode"),
                "model": self.stats.get("embed_model_id"),
                "dim": self.stats.get("embed_dim"),
            },
            "llm": self.llm,
            "index": self.index_meta,
            "rerank": self.rerank_enabled,
            "journeys": journeys,
            "appliances": appliances,
            "doc_types": self.stats.get("doc_types") or {},
            "models": models,
            "examples": examples,
        }

    # ------------------------------------------------------------------ 实体与路由

    def match_entities(self, query: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """型号与品类识别：**只做字符串命中**，命中不了就不猜。"""
        lowered = query.lower()
        models: list[dict[str, Any]] = []
        for item in self.entities.get("models") or []:
            hits = [alias for alias in item.get("aliases") or [] if alias.lower() in lowered]
            if hits:
                best = max(hits, key=len)
                models.append(
                    {
                        "product_id": item.get("product_id"),
                        "label": item.get("label"),
                        "appliance": item.get("appliance"),
                        "matched": best,
                    }
                )
        models.sort(key=lambda item: len(item["matched"]), reverse=True)

        appliances: list[dict[str, Any]] = []
        for item in self.entities.get("appliances") or []:
            hits = [word for word in item.get("words") or [] if word.lower() in lowered]
            if hits:
                appliances.append(
                    {
                        "key": item.get("key"),
                        "label": item.get("label") or item.get("key"),
                        "matched": max(hits, key=len),
                    }
                )
        return models, appliances

    def keyword_intent(self, query: str) -> dict[str, Any] | None:
        """在意图词表里找**最长命中词**：词越长越具体（「不排水」优于「水」）。"""
        lowered = query.lower()
        best: dict[str, Any] | None = None
        for intent in self.intents:
            for phrase in intent.get("phrases") or []:
                if phrase in lowered and (best is None or len(phrase) > len(best["phrase"])):
                    best = {
                        "phrase": phrase,
                        "intent": intent.get("intent"),
                        "journey": intent.get("journey"),
                    }
        if best is not None:
            best["label"] = JOURNEY_LABELS.get(str(best["journey"]), str(best["journey"]))
        return best

    def route(self, query: str, *, manual: str | None = None) -> dict[str, Any]:
        """场景路由：**关键词证据优先，语义最近邻兜底**。

        为什么不是纯语义：中文问句几乎都带品类名，向量相似度会被它带偏
        （实测「不排水怎么处理」被判成售后政策）。KB 的 ``intent_map.yaml`` 已经列了
        用户说法，命中词是最便宜的强信号；两者都给不出确定答案时，**不限定链路**——
        误收窄的代价（把正确答案过滤掉）远高于不收窄。
        """
        vector = self.bundle.embedder.embed([query])[0]

        per_journey: dict[str, dict[str, Any]] = {}
        for item, proto_vector in zip(self.prototypes, self.prototype_vectors, strict=False):
            score = _cosine(vector, proto_vector)
            journey = item["journey"]
            current = per_journey.get(journey)
            if current is None or score > current["score"]:
                per_journey[journey] = {
                    "journey": journey,
                    "label": JOURNEY_LABELS.get(journey, journey),
                    "score": round(score, 4),
                    "question": item.get("question"),
                    "intent": item.get("intent"),
                }

        scores = sorted(per_journey.values(), key=lambda entry: entry["score"], reverse=True)
        top = scores[0] if scores else None
        margin = round(scores[0]["score"] - scores[1]["score"], 4) if len(scores) > 1 else None
        keyword = self.keyword_intent(query)

        if manual:
            chosen, confidence, reason = manual, "manual", "手动指定链路"
        elif keyword is not None:
            chosen = str(keyword["journey"])
            confidence = "high"
            reason = f"命中意图词「{keyword['phrase']}」→ {keyword['intent']}"
        elif top is not None and top["score"] >= 0.65 and (margin is None or margin >= 0.06):
            chosen, confidence, reason = top["journey"], "medium", "语义最近邻（≥0.65 且与次优差距 ≥0.06）"
        else:
            chosen, confidence, reason = None, "low", "候选得分接近，不限定链路（避免误收窄）"

        return {
            "scores": scores[:6],
            "chosen": chosen,
            "confidence": confidence,
            "reason": reason,
            "keyword": keyword,
            "margin": margin,
        }

    # ------------------------------------------------------------------ 主流程

    def run(self, query: str, options: dict[str, Any]) -> Iterator[dict[str, Any]]:
        """执行一次问答，按阶段产出事件（NDJSON）。"""
        started = time.perf_counter()
        timings: dict[str, float] = {}
        top_k = int(options.get("top_k") or 5)
        mode = str(options.get("mode") or "hybrid")
        use_journey = bool(options.get("use_journey", True))
        use_llm = bool(options.get("use_llm", True))

        # ---- 1. 实体 ----
        step = time.perf_counter()
        models, appliances = self.match_entities(query)
        chosen_models = models[:2]
        model_ids = [item["product_id"] for item in chosen_models]
        appliance_keys = [item["key"] for item in appliances]
        timings["understand"] = round((time.perf_counter() - step) * 1000, 1)
        yield {
            "event": "understand",
            "query": query,
            "models": chosen_models,
            "appliances": appliances,
            "hints": [
                "问题里没有出现可识别的型号，参数类问题应当先追问型号" if not chosen_models else None,
            ],
            "elapsed_ms": timings["understand"],
        }

        # ---- 2. 路由 ----
        step = time.perf_counter()
        manual = options.get("journey")
        route = self.route(query, manual=manual)
        journey_choice = route["chosen"] if (manual or use_journey) else None
        timings["route"] = round((time.perf_counter() - step) * 1000, 1)
        yield {
            "event": "route",
            "scores": route["scores"],
            "chosen": journey_choice,
            "chosen_label": JOURNEY_LABELS.get(journey_choice, journey_choice) if journey_choice else "不限定",
            "confidence": route["confidence"],
            "reason": route["reason"],
            "keyword": route["keyword"],
            "margin": route["margin"],
            "enabled": use_journey or bool(manual),
            "elapsed_ms": timings["route"],
        }

        filters = {
            "acl_tags": [],
            "journeys": [journey_choice] if journey_choice else [],
            "appliances": [str(options["appliance"])] if options.get("appliance") else appliance_keys,
            "product_ids": [str(options["product_id"])] if options.get("product_id") else model_ids,
        }

        # ---- 3-7. 检索（召回 / 融合 / 闸门 / 过滤 / 精排 一次算出，分事件下发）----
        trace: dict[str, Any] = {}
        step = time.perf_counter()
        chunks = self.retriever.search(query, top_k=top_k, filters=filters, mode=mode, trace=trace)
        timings["retrieve"] = round((time.perf_counter() - step) * 1000, 1)

        yield {
            "event": "recall",
            "mode": trace.get("mode"),
            "candidates": trace.get("candidates"),
            "dense": trace.get("dense", []),
            "sparse": trace.get("sparse", []),
            "timings_ms": trace.get("timings_ms", {}),
            "elapsed_ms": timings["retrieve"],
        }
        yield {"event": "fuse", "fused": trace.get("fused", [])}
        yield {"event": "gate", **trace.get("gate", {})}
        yield {"event": "filter", "filter": trace.get("filter", {})}
        if trace.get("rerank") is not None:
            yield {"event": "rerank", **trace["rerank"]}

        # 路由失手会导致「明明有依据却检索为空」：这里显式回退一次，并把回退摊开给用户看
        fallback = None
        if not chunks and filters["journeys"]:
            fallback_filters = {**filters, "journeys": []}
            fallback_trace: dict[str, Any] = {}
            chunks = self.retriever.search(
                query, top_k=top_k, filters=fallback_filters, mode=mode, trace=fallback_trace
            )
            trace = fallback_trace
            fallback = {
                "reason": "按链路收窄后无结果，已回退到全库",
                "journey": journey_choice,
                "recovered": len(chunks),
            }
            yield {"event": "fallback", **fallback, "filter": trace.get("filter", {})}

        # ---- 8. 上下文 ----
        step = time.perf_counter()
        metas = [self.retriever.doc_meta(chunk.source) for chunk in chunks]
        payload_chunks = []
        for index, (chunk, meta) in enumerate(zip(chunks, metas, strict=False), start=1):
            payload_chunks.append(
                {
                    "index": index,
                    "doc_id": chunk.source,
                    "score": round(chunk.score, 6),
                    "text": chunk.text,
                    "journey": chunk.metadata.get("journey"),
                    "doc_type": chunk.metadata.get("doc_type"),
                    "appliance": getattr(meta, "appliance", None),
                    "product_id": getattr(meta, "product_id", None),
                    "origin": getattr(meta, "source", None),
                    "source_page": getattr(meta, "source_page", None),
                    "verified": getattr(meta, "verified", None),
                }
            )
        model_hint = self._model_label(filters["product_ids"], chosen_models)
        appliance_hint = self._appliance_label(filters["appliances"])
        context_stats: dict[str, int] = {}
        messages = self._build_messages(
            query,
            payload_chunks,
            model_hint=model_hint,
            appliance_hint=appliance_hint,
            stats=context_stats,
        )
        raw_context_chars = sum(len(item["text"]) for item in payload_chunks)
        context_chars = context_stats.get("context_chars", raw_context_chars)
        timings["context"] = round((time.perf_counter() - step) * 1000, 1)
        yield {
            "event": "context",
            "chunks": payload_chunks,
            "raw_context_chars": raw_context_chars,
            "context_chars": context_chars,
            "included_chunks": context_stats.get("included_chunks", len(payload_chunks)),
            "messages": messages,
            "elapsed_ms": timings["context"],
        }

        # ---- 9. 生成 ----
        answer = ""
        if not payload_chunks:
            answer = "知识库里没有检索到能支撑这个问题的依据。我不会凭印象作答——建议转人工确认。"
            yield {"event": "token", "text": answer}
            yield {"event": "llm", "skipped": "no_evidence", "provider": None}
        elif not use_llm:
            yield {"event": "llm", "skipped": "retrieval_only", "provider": None}
        else:
            step = time.perf_counter()
            try:
                handle = self.container.resolve_model_handle(task=None)
                pieces: list[str] = []
                for piece in stream_chat(handle, messages):
                    pieces.append(piece)
                    yield {"event": "token", "text": piece}
                answer = "".join(pieces)
                timings["llm"] = round((time.perf_counter() - step) * 1000, 1)
                yield {
                    "event": "llm",
                    "provider": self.llm.get("provider"),
                    "elapsed_ms": timings["llm"],
                }
            except (ProviderConfigError, IndustryPackError) as exc:
                answer = f"（模型未就绪，仅返回检索结果：{exc}）"
                yield {"event": "error", "stage": "llm", "message": str(exc)}

        # ---- 10. 校验 ----
        check = self._verify(query, answer, payload_chunks, filters["product_ids"])
        yield {"event": "check", **check}

        timings["total"] = round((time.perf_counter() - started) * 1000, 1)
        yield {
            "event": "done",
            "answer": answer,
            "timings_ms": timings,
            "fallback": fallback,
        }

    # ------------------------------------------------------------------ 内部

    def _model_label(
        self, product_ids: list[str], matched: list[dict[str, Any]]
    ) -> str | None:
        """当前会话已确定的型号（用于提示词，避免模型重复追问）。"""
        if matched:
            return str(matched[0].get("label") or matched[0].get("product_id"))
        if product_ids:
            for item in self.entities.get("models") or []:
                if item.get("product_id") == product_ids[0]:
                    return str(item.get("label") or product_ids[0])
            return str(product_ids[0])
        return None

    def _appliance_label(self, appliance_keys: list[str]) -> str | None:
        if not appliance_keys:
            return None
        for item in self.entities.get("appliances") or []:
            if item.get("key") == appliance_keys[0]:
                return str(item.get("label") or appliance_keys[0])
        return str(appliance_keys[0])

    def _build_messages(
        self,
        query: str,
        chunks: list[dict[str, Any]],
        *,
        model_hint: str | None = None,
        appliance_hint: str | None = None,
        stats: dict[str, int] | None = None,
    ) -> list[dict[str, str]]:
        """拼装上下文：编号 + 出处 + 依据等级，**顺序即引用编号**。"""
        blocks: list[str] = []
        used = 0
        for item in chunks:
            head = f"[{item['index']}] 来源：{item['doc_id']}"
            if item.get("origin"):
                head += f"（原文：{item['origin']}"
                head += f"，第 {item['source_page']} 页）" if item.get("source_page") else "）"
            if item.get("verified") is False:
                head += "【未核对】"
            elif item.get("verified") is True:
                head += "【已核对】"
            block = f"{head}\n{item['text'].strip()}"
            if used + len(block) > MAX_CONTEXT_CHARS and blocks:
                break
            blocks.append(block)
            used += len(block)

        known: list[str] = []
        if model_hint:
            known.append(f"已知型号：{model_hint}")
        if appliance_hint:
            known.append(f"已知品类：{appliance_hint}")
        known_line = ("；".join(known) + "。已确定的型号不必再追问。\n\n") if known else ""

        user = (
            f"用户问题：{query}\n\n"
            f"{known_line}"
            f"以下是知识库检索片段（共 {len(blocks)} 段）。只依据这些片段回答，"
            "每个事实后用 [编号] 标注来源；片段里没有的不要补。\n\n"
            + "\n\n".join(blocks)
        )
        if stats is not None:
            stats["context_chars"] = used
            stats["included_chunks"] = len(blocks)
        return [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": user},
        ]

    def _verify(
        self,
        query: str,
        answer: str,
        chunks: list[dict[str, Any]],
        product_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """引用与答案校验：**回答里的引用必须来自本轮片段**。"""
        cited = sorted({int(match) for match in CITATION_PATTERN.findall(answer or "")})
        valid_range = {item["index"] for item in chunks}
        invalid = [index for index in cited if index not in valid_range]
        sources = [
            {
                "index": item["index"],
                "doc_id": item["doc_id"],
                "source_page": item.get("source_page"),
                "verified": item.get("verified"),
                "origin": item.get("origin"),
            }
            for item in chunks
            if item["index"] in cited
        ]
        unverified = [
            item["index"] for item in chunks if item.get("verified") is False
        ]

        # 同一句问题在不同型号下是不同的题（「这台制冷量多少？」有多个标准答案），
        # 因此先用型号收窄，再按问题文本取第一条。
        same_question = [
            item for item in self.checks if _normalize(item["question"]) == _normalize(query)
        ]
        if product_ids:
            narrowed = [item for item in same_question if item.get("product_id") in set(product_ids)]
            same_question = narrowed or same_question
        check_item = same_question[0] if same_question else None
        answer_check: dict[str, Any] | None = None
        if check_item is not None:
            must = [str(token) for token in check_item.get("must_include") or []]
            forbidden = [str(token) for token in check_item.get("must_not_include") or []]
            compact = _compact(answer)
            missing = [token for token in must if _compact(token) not in compact]
            violated = [token for token in forbidden if _compact(token) in compact]
            answer_check = {
                "id": check_item.get("id"),
                "reference": check_item.get("reference_answer"),
                "basis": check_item.get("basis"),
                "must_include": must,
                "missing": missing,
                "forbidden_hit": violated,
                "passed": not missing and not violated,
            }

        warnings: list[str] = []
        if chunks and not cited and answer and not answer.startswith("（模型未就绪"):
            warnings.append("回答没有标注任何引用编号，无法核对依据")
        if invalid:
            warnings.append(f"引用了不存在的编号：{invalid}")
        if unverified:
            warnings.append(f"片段 {unverified} 的依据尚未人工核对，回答需注明")

        return {
            "cited": cited,
            "sources": sources,
            "invalid_citations": invalid,
            "unverified_used": unverified,
            "answer_check": answer_check,
            "warnings": warnings,
        }


# --------------------------------------------------------------------- 工具函数


def _normalize(text: str) -> str:
    return re.sub(r"\s+", "", str(text or ""))


def _compact(text: str) -> str:
    """去掉空白与标点，只留可比较的字符（数值校验时避免因格式差异误判）。"""
    return re.sub(r"[\s，。、；：！？,.;:!?（）()\[\]【】\"'`]", "", str(text or ""))


def _cosine(left: list[float], right: list[float]) -> float:
    if not left or not right:
        return 0.0
    dot = sum(a * b for a, b in zip(left, right, strict=False))
    norm_left = math.sqrt(sum(a * a for a in left))
    norm_right = math.sqrt(sum(b * b for b in right))
    if norm_left == 0 or norm_right == 0:
        return 0.0
    return dot / (norm_left * norm_right)


# --------------------------------------------------------------------- HTTP


def _make_handler(panel: Panel):
    class Handler(BaseHTTPRequestHandler):
        server_version = "ZeKeFlow/1.0"

        def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

        # ------------------------------------------------------------ GET

        def do_GET(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            if path in ("/", "/index.html"):
                html = (WEB_DIR / "flow.html").read_text(encoding="utf-8")
                self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")
                return
            if path == "/api/meta":
                self._send_json(200, panel.meta())
                return
            if path == "/api/health":
                self._send_json(200, {"ok": True, "pack": panel.pack_id})
                return
            self._send_json(404, {"ok": False, "error": f"未知路径：{path}"})

        # ------------------------------------------------------------ POST

        def do_POST(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            if path != "/api/ask":
                self._send_json(404, {"ok": False, "error": f"未知路径：{path}"})
                return
            length = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                self._send_json(400, {"ok": False, "error": "请求体不是合法 JSON"})
                return

            query = str(body.get("query") or "").strip()
            if not query:
                self._send_json(400, {"ok": False, "error": "query 不能为空"})
                return
            options = body.get("options") or {}

            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            try:
                with panel.lock:  # 单机面板：串行执行，避免并发把内存打满
                    for event in panel.run(query, options):
                        self.wfile.write(
                            (json.dumps(event, ensure_ascii=False) + "\n").encode("utf-8")
                        )
                        self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                return
            except Exception as exc:  # 面板级兜底：把异常变成可读事件而不是断流
                try:
                    self.wfile.write(
                        (
                            json.dumps(
                                {"event": "error", "stage": "server", "message": f"{type(exc).__name__}: {exc}"},
                                ensure_ascii=False,
                            )
                            + "\n"
                        ).encode("utf-8")
                    )
                    self.wfile.flush()
                except Exception:
                    return

        # ------------------------------------------------------------ 工具

        def _send(self, status: int, payload: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def _send_json(self, status: int, payload: Any) -> None:
            self._send(
                status,
                json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                "application/json; charset=utf-8",
            )

    return Handler


def serve(panel: Panel, host: str, port: int) -> None:
    server = ThreadingHTTPServer((host, port), _make_handler(panel))
    print(f"数据流面板已启动：http://{host}:{port}", file=sys.stderr)
    print(f"  行业包：{panel.pack_id}  文档：{panel.stats.get('docs')}  分块：{panel.stats.get('chunks')}", file=sys.stderr)
    print(f"  模型：{panel.llm.get('provider')}（ready={panel.llm.get('ready')}）", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止", file=sys.stderr)
    finally:
        server.server_close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="交互式知识检索数据流面板")
    parser.add_argument("--pack", dest="pack_id", default="homeapp", help="行业包 id")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--rerank", action="store_true", help="加载精排模型（约 1GB，首次较慢）")
    parser.add_argument("--build-index", action="store_true", help="索引缺失时自动构建")
    args = parser.parse_args(argv)

    panel = Panel.build(pack_id=args.pack_id, rerank=args.rerank, build_index=args.build_index)
    serve(panel, args.host, args.port)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
