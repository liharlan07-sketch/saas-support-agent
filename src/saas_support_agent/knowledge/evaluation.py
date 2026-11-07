"""Compare the same index, identities and judgments for each retrieval mode."""

from argparse import ArgumentParser
from contextlib import closing
from datetime import date
import json
from math import ceil
from pathlib import Path
import resource
from statistics import mean, median
from tempfile import TemporaryDirectory
from time import perf_counter

from saas_support_agent.knowledge.models import LocalModels
from saas_support_agent.knowledge.retrieval import MODES, PolicyIndex, PolicyKnowledge
from saas_support_agent.merchant_demo import DEMO_IDENTITY, seed_demo
from saas_support_agent.merchant_support import MerchantSupport
from saas_support_agent.repositories.database import Database
from saas_support_agent.services.notification_service import NotificationService


def cases(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def timed(operation):
    start = perf_counter()
    result = operation()
    return result, (perf_counter() - start) * 1000


def evaluate(index, knowledge, agent, samples, boundaries):
    # 预热同一批算子，分模式计时，不混入模型初始化或权限查询。
    identity = DEMO_IDENTITY
    index.search(samples[0]["query"], knowledge.scope(identity), "rerank")
    report = {}
    for mode in MODES:
        scores, first, latencies, misses = [], [], [], []
        for case in samples:
            question, expected = case["query"], set(case["evidence"])
            allowed = knowledge.scope(identity, question)
            if not expected <= {index.chunks[key]["source_id"] for key in allowed}:
                raise ValueError(f"金标准证据不在有效范围内：{question}")
            results, elapsed = timed(lambda: index.search(question, allowed, mode))
            ids = [row["source_id"] for row, _ in results]
            assert all(row["id"] in allowed for row, _ in results)
            scores.append(len(expected.intersection(ids[:5])) / len(expected))
            first.append(bool(ids) and ids[0] in expected)
            latencies.append(elapsed)
            if not first[-1]:
                misses.append({"question": question, "first": ids[0] if ids else None})
        report[mode] = {
            "recall_at_5": round(mean(scores), 4), "hit_at_1": sum(first),
            "p50_ms": round(median(latencies), 2),
            "p95_ms": round(sorted(latencies)[ceil(len(latencies) * .95) - 1], 2),
            "misses_at_1": misses,
        }
    profile = {}
    scope = knowledge.scope(identity)
    for width in (3, 5, 10):
        sample = samples[:5]
        latency = [timed(lambda: index.search(case["query"], scope, "rerank", width))[1] for case in sample]
        profile[str(width)] = round(median(latency), 2)
    checks = []
    for case in boundaries:
        actual = agent.handle(identity, case["query"])
        checks.append({"question": case["query"], "expected": case["status"], "actual": actual.status,
                       "source_ids": [row["source_id"] for row in actual.evidence]})
    return report, profile, checks


def main():
    parser = ArgumentParser(description="本地政策检索固定标注集")
    parser.add_argument("--rag-root", type=Path, default=Path("runtime/rag"))
    parser.add_argument("--catalog", type=Path, default=Path("data/merchant_policies/catalog.json"))
    parser.add_argument("--cases", type=Path, default=Path("data/merchant_rag_eval"))
    args = parser.parse_args()
    with TemporaryDirectory() as temp:
        db = Database(Path(temp) / "evaluation.sqlite3")
        db.initialize()
        seed_demo(db)
        models = LocalModels(args.rag_root / "models")
        with closing(PolicyIndex(args.rag_root / "index", args.catalog, models)) as index:
            knowledge = PolicyKnowledge(index, NotificationService(db), today=lambda: date(2026, 11, 6))
            agent = MerchantSupport(db, knowledge=knowledge)
            samples = cases(args.cases / "heldout.jsonl")
            report, profile, boundaries = evaluate(index, knowledge, agent, samples, cases(args.cases / "boundaries.jsonl"))
            print(json.dumps({
                "queries": len(samples), "index_chunks": len(index.chunks), "models_load_s": round(models.load_seconds, 3),
                "retrieval": report, "rerank_candidate_profile_p50_ms": profile,
                "boundary_checks": boundaries, "peak_rss_native": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
            }, ensure_ascii=False, indent=2))
            return 1 if any(row["actual"] != row["expected"] or row["source_ids"] for row in boundaries) else 0


if __name__ == "__main__":
    raise SystemExit(main())
