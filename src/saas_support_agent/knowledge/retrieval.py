"""Milvus Lite snapshots, trusted filters and verifiable evidence."""

from contextlib import closing
from datetime import date
import json
from math import isfinite
import re
from uuid import uuid4

from saas_support_agent.core.models import OperationResult, RuleViolation
from saas_support_agent.knowledge.models import SNAPSHOTS, digest
from saas_support_agent.knowledge.policies import read_chunks


COLLECTION = "merchant_policy"
DENSE = {"metric_type": "COSINE", "params": {}}
SPARSE = {"metric_type": "BM25", "params": {}}
MODES = ("dense", "hybrid", "rerank")
INDEX_VERSION = 1


def _fingerprint():
    return {role: list(value[1:]) for role, value in SNAPSHOTS.items()}


def _open_index(path):
    from pymilvus import MilvusClient

    if not path.exists():
        raise FileNotFoundError("政策索引不存在")
    return MilvusClient(str(path))


def build_index(index_root, catalog, models):
    from pymilvus import DataType, Function, FunctionType, MilvusClient

    catalog_sha = digest(catalog)
    chunks = read_chunks(catalog)
    vectors = models.documents([chunk["text"] for chunk in chunks])
    if len(vectors) != len(chunks) or any(
        len(v) != models.dimension or not all(isfinite(x) for x in v) for v in vectors
    ):
        raise ValueError("向量数量、维度或值不正确")
    generation = uuid4().hex
    index_root.mkdir(parents=True, exist_ok=True)
    target = index_root / generation
    target.mkdir()
    # 仅完整写入并重开校验的快照能成为 current，失败不影响上一版本。
    with closing(MilvusClient(str(target / "policy.db"))) as client:
        schema = client.create_schema(auto_id=False)
        schema.add_field("id", DataType.INT64, is_primary=True)
        schema.add_field("text", DataType.VARCHAR, max_length=4096, enable_analyzer=True,
                         analyzer_params={"tokenizer": "jieba"})
        schema.add_field("dense", DataType.FLOAT_VECTOR, dim=models.dimension)
        schema.add_field("sparse", DataType.SPARSE_FLOAT_VECTOR)
        schema.add_function(Function(name="text_bm25", function_type=FunctionType.BM25,
                                     input_field_names=["text"], output_field_names=["sparse"]))
        indexes = client.prepare_index_params()
        indexes.add_index("dense", index_type="FLAT", metric_type="COSINE")
        indexes.add_index("sparse", index_type="SPARSE_INVERTED_INDEX", metric_type="BM25")
        client.create_collection(COLLECTION, schema=schema, index_params=indexes)
        client.insert(COLLECTION, [{"id": c["id"], "text": c["text"], "dense": v} for c, v in zip(chunks, vectors)])
        client.flush(COLLECTION)
    with closing(_open_index(target / "policy.db")) as client:
        client.load_collection(COLLECTION)
        rows = client.query(COLLECTION, filter="", output_fields=["id"], limit=len(chunks) + 1)
        if {row["id"] for row in rows} != {chunk["id"] for chunk in chunks}:
            raise ValueError("索引写入校验失败")
    if digest(catalog) != catalog_sha:
        raise ValueError("索引期间政策目录发生变化")
    (target / "manifest.json").write_text(
        json.dumps({"format": INDEX_VERSION, "models": _fingerprint(), "catalog_sha": catalog_sha, "chunks": chunks}, ensure_ascii=False),
        encoding="utf-8",
    )
    pending = index_root / f".current-{generation}"
    pending.write_text(generation, encoding="ascii")
    pending.replace(index_root / "current")
    return len(chunks)


class PolicyIndex:
    def __init__(self, index_root, catalog, models):
        generation = (index_root / "current").read_text(encoding="ascii")
        if len(generation) != 32 or any(c not in "0123456789abcdef" for c in generation):
            raise ValueError("无效的索引快照标识")
        target = index_root / generation
        manifest = json.loads((target / "manifest.json").read_text(encoding="utf-8"))
        if manifest["format"] != INDEX_VERSION or manifest["models"] != _fingerprint():
            raise ValueError("索引版本或模型与运行配置不匹配，请重建")
        self.chunks = {c["id"]: c for c in manifest["chunks"]}
        self.catalog = catalog.resolve()
        self.catalog_sha = manifest["catalog_sha"]
        self.source_root = self.catalog.parent
        self.models = models
        self.client = _open_index(target / "policy.db")
        try:
            self.client.load_collection(COLLECTION)
        except Exception:
            self.close()
            raise

    def close(self):
        self.client.close()

    def eligible(self, workspace, shops, today):
        if digest(self.catalog) != self.catalog_sha:
            raise ValueError("政策范围或版本目录已变化，请重建索引")
        # 在发送给 Milvus 之前把范围解析为可信整数 ID，两路共享过滤条件。
        current = today.isoformat()
        return {
            key for key, row in self.chunks.items()
            if row["effective_from"] <= current
            and (not row["effective_until"] or current < row["effective_until"])
            and row["workspace_id"] in (None, workspace)
            and (not row["shop_id"] or row["shop_id"] in shops)
        }

    def search(self, question, eligible, mode="rerank", candidates=10):
        from pymilvus import AnnSearchRequest, RRFRanker

        if mode not in MODES or not 1 <= candidates <= 20:
            raise ValueError("无效的检索模式或候选数")
        if not eligible:
            return []
        where = f"id in [{','.join(str(key) for key in sorted(eligible))}]"
        vector = self.models.query(question)
        if mode == "dense":
            hits = self.client.search(COLLECTION, [vector], anns_field="dense", filter=where,
                                      search_params=DENSE, limit=candidates)[0]
        else:
            hits = self.client.hybrid_search(
                COLLECTION, [
                    AnnSearchRequest([vector], "dense", DENSE, candidates, expr=where),
                    AnnSearchRequest([question], "sparse", SPARSE, candidates, expr=where),
                ], RRFRanker(60), limit=candidates,
            )[0]
        rows = [self.chunks[hit["id"]] for hit in hits if hit["id"] in eligible]
        if mode != "rerank" or not rows:
            return [(row, None) for row in rows]
        scores = self.models.scores(question, [row["text"] for row in rows])
        if len(scores) != len(rows) or not all(isfinite(score) for score in scores):
            raise ValueError("重排返回无效分数")
        return sorted(zip(rows, scores), key=lambda item: -item[1])

    def cite(self, row):
        source = (self.source_root / row["file"]).resolve()
        if not source.is_relative_to(self.source_root) or digest(source) != row["checksum"]:
            raise ValueError("来源文件与索引快照不一致")
        return {key: row[key] for key in ("source_id", "title", "version", "section", "page", "file", "text")}


class PolicyKnowledge:
    def __init__(self, index, service, *, today=date.today, min_score=0.0, generator=None):
        self.index = index
        self.service = service
        self.today = today
        self.min_score = min_score
        self.generator = generator

    def scope(self, identity, question=""):
        self.service.validate_identity(identity)
        with closing(self.service.database.connect()) as connection:
            connection.execute("BEGIN")
            self.service.memberships.require_active(connection, identity)
            rows = connection.execute(
                """SELECT a.shop_id FROM shop_access a JOIN shops s USING (workspace_id, shop_id)
                   WHERE a.workspace_id=? AND a.user_id=? AND s.status='active'""",
                (identity.workspace_id, identity.user_id),
            ).fetchall()
        shops = {row["shop_id"] for row in rows}
        requested = set(re.findall(r"(?<![A-Z0-9-])SHOP-[A-Z0-9]{1,24}(?![A-Z0-9-])", question.upper()))
        if requested - shops:
            raise RuleViolation("denied", "scope_or_permission_denied", "无法访问请求的店铺政策。")
        return self.index.eligible(identity.workspace_id, requested or shops, self.today())

    def answer(self, identity, question):
        try:
            allowed = self.scope(identity, question)
            rows = self.index.search(question, allowed)
            # 推理耗时期间可能撤权或过期，返回前再核对当前范围。
            allowed = self.scope(identity, question)
            evidence = tuple(self.index.cite(row) for row, score in rows[:3]
                             if row["id"] in allowed and score >= self.min_score)
        except RuleViolation as error:
            return OperationResult(identity.request_id, error.status, error.message, "Knowledge",
                                   error_code=error.code)
        except Exception:
            # 索引/模型故障不同于没有证据，且不能回退到可能过期的硬编码条款。
            return OperationResult(identity.request_id, "failed", "政策检索暂不可用，请核对索引和模型后重试。",
                                   "Knowledge", error_code="knowledge_unavailable")
        if not evidence:
            return OperationResult(identity.request_id, "no_evidence", "没有找到可靠的现行政策证据。",
                                   "Knowledge", error_code="no_evidence")
        message = "以下是适用的示例政策原文摘录：\n" + "\n".join(
            f"[{i}] {item['text']}" for i, item in enumerate(evidence, 1)
        )
        metadata = {}
        if self.generator is not None:
            from saas_support_agent.conversation.model import InvalidModelOutput, ModelUnavailable
            try:
                message = self.generator.generate(question, evidence)
                metadata["answer_mode"] = "model_with_evidence"
            except (InvalidModelOutput, ModelUnavailable):
                metadata["answer_mode"] = "extractive_fallback"
            # 生成耗时不延长证据有效期；失败回退也必须重新检查，不能泄露旧证据。
            try:
                allowed = self.scope(identity, question)
                cited_ids = {item["source_id"] for item in evidence}
                current = tuple(self.index.cite(row) for row, _ in rows[:3]
                                if row["id"] in allowed and row["source_id"] in cited_ids)
                if current != evidence:
                    return OperationResult(identity.request_id, "no_evidence", "政策范围已变化，请重新咨询。",
                                           "Knowledge", error_code="no_evidence")
            except RuleViolation as error:
                return OperationResult(identity.request_id, error.status, error.message, "Knowledge", error_code=error.code)
            except Exception:
                return OperationResult(identity.request_id, "failed", "政策证据暂不可用。",
                                       "Knowledge", error_code="knowledge_unavailable")
        return OperationResult(identity.request_id, "answered", message, "Knowledge", data=metadata, evidence=evidence)
