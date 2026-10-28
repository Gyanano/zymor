from __future__ import annotations

import math
import os
from dataclasses import dataclass
from typing import Any, Protocol

from zymor.config import Config
from zymor.errors import ZymorError
from zymor.model.node import MemoryNode
from zymor.store.index import Index
from zymor.store.nodes import read_node
from zymor.tokenizer import estimate_tokens
from zymor.vault import Vault

DEFAULT_MODEL = "text-embedding-3-small"
DEFAULT_MIN_SCORE = 0.2
DEFAULT_BATCH_SIZE = 64
MAX_EMBED_CHARS = 6000


class EmbeddingError(ZymorError):
    """向量服务不可用或返回格式错误。"""


class EmbeddingClient(Protocol):
    def embed(self, texts: list[str]) -> list[list[float]]:
        """入参：文本列表。出参：与文本顺序一致的向量列表。"""
        ...


@dataclass(frozen=True)
class EmbeddingSettings:
    enabled: bool
    model: str
    base_url: str
    api_key_env: str
    timeout_s: float
    min_score: float

    @classmethod
    def from_config(cls, config: Config) -> EmbeddingSettings:
        """入参：配置。出参：向量检索设置。"""
        base_url = config.get("embedding.base_url", None) or config.get("llm.base_url")
        return cls(
            enabled=bool(config.get("embedding.enabled", False)),
            model=str(config.get("embedding.model", DEFAULT_MODEL)),
            base_url=str(base_url),
            api_key_env=str(config.get("embedding.api_key_env", "OPENAI_API_KEY")),
            timeout_s=float(config.get("embedding.timeout_s", 30)),
            min_score=float(config.get("embedding.min_score", DEFAULT_MIN_SCORE)),
        )


@dataclass
class OpenAIEmbeddingClient:
    settings: EmbeddingSettings

    def embed(self, texts: list[str]) -> list[list[float]]:
        """入参：文本列表。出参：OpenAI embeddings 向量。"""
        if not texts:
            return []
        api_key = os.environ.get(self.settings.api_key_env)
        if not api_key:
            raise EmbeddingError(
                f"缺少 API key 环境变量 {self.settings.api_key_env}",
                hint="设置 API key 或在 [embedding] 配置 api_key_env",
            )
        try:
            from openai import OpenAI
        except ImportError as error:
            raise EmbeddingError("未安装 openai 依赖", hint="pip install openai") from error
        try:
            client = OpenAI(
                base_url=self.settings.base_url,
                api_key=api_key,
                timeout=self.settings.timeout_s,
                max_retries=0,
            )
            response = client.embeddings.create(
                model=self.settings.model,
                input=[text[:MAX_EMBED_CHARS] for text in texts],
            )
        except Exception as error:
            raise EmbeddingError(f"向量请求失败: {error}") from error
        ordered = sorted(response.data, key=lambda item: item.index)
        return [[float(value) for value in item.embedding] for item in ordered]


def cosine(left: list[float], right: list[float]) -> float:
    """入参：两个向量。出参：余弦相似度；零向量返回 0。"""
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    return dot / (left_norm * right_norm)


def node_embedding_text(node: MemoryNode) -> str:
    """入参：节点。出参：用于向量化的文本。"""
    text = "\n".join([node.title, node.summary, node.body, " ".join(node.tags)])
    return text[:MAX_EMBED_CHARS]


def _all_nodes(vault: Vault) -> list[MemoryNode]:
    index = Index(vault.root)
    nodes: list[MemoryNode] = []
    for layer in ("L2", "L3", "L4"):
        for row in index.layer_rows(
            layer, statuses=["hypothesis", "verified", "disputed"], limit=10000
        ):
            try:
                node, _ = read_node(vault, str(row["id"]))
            except ZymorError:
                continue
            nodes.append(node)
    return nodes


def build_embeddings(
    vault: Vault,
    config: Config,
    *,
    client: EmbeddingClient | None = None,
) -> int:
    """入参：vault、配置与可选客户端。出参：写入的向量数。为节点构建向量索引。"""
    settings = EmbeddingSettings.from_config(config)
    embedding_client = client or OpenAIEmbeddingClient(settings)
    nodes = _all_nodes(vault)
    index = Index(vault.root)
    batch_size = int(config.get("embedding.batch_size", DEFAULT_BATCH_SIZE))
    written = 0
    for start in range(0, len(nodes), batch_size):
        batch = nodes[start : start + batch_size]
        vectors = embedding_client.embed([node_embedding_text(node) for node in batch])
        if len(vectors) != len(batch):
            raise EmbeddingError("向量数量与节点数量不一致")
        for node, vector in zip(batch, vectors, strict=True):
            index.upsert_embedding(node.id, settings.model, vector)
            written += 1
    return written


def semantic_search(
    scopes: list[tuple[str, Vault]],
    query: str,
    settings: EmbeddingSettings,
    *,
    client: EmbeddingClient,
    top_k: int = 8,
    token_budget: int = 1500,
) -> dict[str, Any]:
    """入参：scope、查询、设置、客户端与预算。出参：语义检索命中字典。"""
    if not query.strip():
        return {"hits": [], "warnings": ["查询为空"], "tokens_estimate": 0}
    query_vector = client.embed([query])[0]
    hits: list[dict[str, Any]] = []
    warnings: list[str] = []
    for label, vault in scopes:
        index = Index(vault.root)
        try:
            rows = index.embedding_rows(settings.model)
        except ZymorError as error:
            warnings.append(f"{label}: {error.message}")
            continue
        for row in rows:
            try:
                node, _ = read_node(vault, str(row["node_id"]))
            except ZymorError:
                continue
            if node.status == "deprecated":
                continue
            score = cosine(query_vector, row["vector"])
            if score < settings.min_score:
                continue
            hits.append(
                {
                    "id": node.id,
                    "title": node.title,
                    "summary": node.summary,
                    "status": node.status,
                    "layer": node.layer,
                    "category": node.category,
                    "scope": label,
                    "score": round(score, 6),
                }
            )
    hits.sort(key=lambda item: item["score"], reverse=True)
    kept: list[dict[str, Any]] = []
    used = 0
    for hit in hits[:top_k]:
        cost = estimate_tokens(f"{hit['title']} {hit['summary']}") + 1
        if used + cost > token_budget:
            break
        kept.append(hit)
        used += cost
    return {"hits": kept, "warnings": warnings, "tokens_estimate": used}


def hybrid_search(
    scopes: list[tuple[str, Vault]],
    query: str,
    *,
    config: Config,
    client: EmbeddingClient,
    top_k: int = 8,
    token_budget: int = 1500,
) -> dict[str, Any]:
    """入参：scope、查询、配置、客户端与预算。出参：词法 + 向量合并后的命中。"""
    from zymor.retrieval import RetrievalSettings, SearchFilters, memory_search

    settings = EmbeddingSettings.from_config(config)
    lexical = memory_search(
        scopes,
        query,
        settings=RetrievalSettings.from_config(config),
        filters=SearchFilters(),
        top_k=top_k,
        token_budget=token_budget,
    )
    semantic = semantic_search(
        scopes,
        query,
        settings,
        client=client,
        top_k=top_k,
        token_budget=token_budget,
    )
    merged: dict[str, dict[str, Any]] = {}
    for hit in lexical.hits:
        merged[hit.id] = {
            "id": hit.id,
            "title": hit.title,
            "summary": hit.summary,
            "status": hit.status,
            "layer": hit.layer,
            "category": hit.category,
            "scope": hit.scope,
            "path": hit.path,
            "score": hit.score,
            "why": list(hit.why),
            "stale": hit.stale,
            "stale_reasons": list(hit.stale_reasons),
        }
    for semantic_hit in semantic["hits"]:
        node_id = str(semantic_hit["id"])
        if node_id in merged:
            current = merged[node_id]
            current["score"] = max(float(current["score"]), float(semantic_hit["score"]))
            current["why"].append("semantic")
            continue
        merged[node_id] = {
            **semantic_hit,
            "path": "",
            "why": ["semantic"],
            "stale": False,
            "stale_reasons": [],
        }
    ordered = sorted(merged.values(), key=lambda item: float(item["score"]), reverse=True)
    kept: list[dict[str, Any]] = []
    used = 0
    for item in ordered[:top_k]:
        cost = estimate_tokens(f"{item['title']} {item['summary']}") + 1
        if used + cost > token_budget:
            break
        kept.append(item)
        used += cost
    warnings = [*lexical.warnings, *semantic["warnings"]]
    return {"hits": kept, "warnings": warnings, "tokens_estimate": used}
