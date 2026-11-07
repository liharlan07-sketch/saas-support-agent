"""3C 固定样本、排名诊断及真实本机 HTTP 计时；不改索引，不调用 LLM。"""
from argparse import ArgumentParser
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import date
import json
from math import ceil
from pathlib import Path
import platform
import resource
from secrets import token_urlsafe
import socket
from statistics import mean, median
from tempfile import TemporaryDirectory
from threading import Event, Thread
from time import perf_counter, sleep
from urllib.request import Request, urlopen

from saas_support_agent.api.app import create_app
from saas_support_agent.knowledge.evaluation import cases, evaluate
from saas_support_agent.knowledge.models import LocalModels, SNAPSHOTS, digest
from saas_support_agent.knowledge.retrieval import PolicyIndex, PolicyKnowledge, COLLECTION, SPARSE
from saas_support_agent.merchant_demo import DEMO_IDENTITY, seed_demo
from saas_support_agent.merchant_support import MerchantSupport


def stats(values):
    return {'n': len(values), 'p50_ms': round(median(values), 2),
            'p95_ms': round(sorted(values)[ceil(len(values) * .95) - 1], 2)}


def inspect_query(index, knowledge, query):
    allowed = knowledge.scope(DEMO_IDENTITY, query)
    dense = index.search(query, allowed, 'dense')
    where = 'id in [' + ','.join(map(str, sorted(allowed))) + ']'
    sparse = index.client.search(COLLECTION, [query], anns_field='sparse', filter=where,
                                 search_params=SPARSE, limit=10)[0]
    fused = index.search(query, allowed, 'hybrid')
    ranked = index.search(query, allowed, 'rerank')
    return {'query': query,
            'dense_ids': [row['source_id'] for row, _ in dense],
            'bm25': [{'source_id': index.chunks[hit['id']]['source_id'], 'score': hit['distance']} for hit in sparse],
            'rrf_ids': [row['source_id'] for row, _ in fused],
            'rerank': [{'source_id': row['source_id'], 'score': score} for row, score in ranked]}


def heading_experiment(index, knowledge, samples):
    """只改变实验中的重排输入，不改来源、索引或正式检索代码。"""
    recalls, hits, misses = [], [], []
    for case in samples:
        query = case['query']
        rows = [row for row, _ in index.search(query, knowledge.scope(DEMO_IDENTITY, query), 'hybrid')]
        scores = index.models.scores(query, [row['section'] + '\n' + row['text'] for row in rows])
        ids = [row['source_id'] for row, score in sorted(zip(rows, scores), key=lambda pair: -pair[1])]
        expected = set(case['evidence'])
        recalls.append(len(expected.intersection(ids[:5])) / len(expected))
        hits.append(bool(ids) and ids[0] in expected)
        if not hits[-1]:
            misses.append({'query': query, 'first': ids[0] if ids else None})
    return {'queries': len(samples), 'recall_at_5': round(mean(recalls), 4), 'hit_at_1': sum(hits), 'misses': misses}


def http_measurements(agent, models):
    import uvicorn
    token = token_urlsafe(32)
    app = create_app(agent, {token: DEMO_IDENTITY.user_id})
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    base = 'http://127.0.0.1:' + str(sock.getsockname()[1])
    server = uvicorn.Server(uvicorn.Config(app, log_level='error', access_log=False))
    thread = Thread(target=lambda: server.run(sockets=[sock]), daemon=True)
    thread.start()

    def post(path, body):
        start = perf_counter()
        request = Request(base + path, data=json.dumps(body).encode(),
                          headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
        with urlopen(request, timeout=60) as response:
            result = json.load(response)
        return result, (perf_counter() - start) * 1000

    try:
        for _ in range(200):
            if server.started:
                break
            if not thread.is_alive():
                raise RuntimeError('HTTP server stopped before readiness')
            sleep(.025)
        else:
            raise RuntimeError('HTTP readiness timeout')
        first, _ = post('/v1/sessions', {'workspace_id': DEMO_IDENTITY.workspace_id})
        second, _ = post('/v1/sessions', {'workspace_id': DEMO_IDENTITY.workspace_id})
        roots = ['/v1/sessions/' + s['session_id'] for s in (first, second)]
        def message(root, text):
            return post(root + '/messages', {'text': text})
        question = '退货超期能不能受理？'
        message(roots[0], question)
        rag_times, query_times = [], []
        for _ in range(10):
            result, elapsed = message(roots[0], question)
            assert result['evidence'] and result['status'] == 'answered'
            rag_times.append(elapsed)
            result, elapsed = message(roots[1], '查询订单 ORD-1001')
            assert result['data']['order']['status'] == 'delivered'
            query_times.append(elapsed)
        blocked = []
        original = models.scores
        entered = Event()
        def observed(*args, **kwargs):
            entered.set()
            return original(*args, **kwargs)
        models.scores = observed
        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                for _ in range(5):
                    entered.clear()
                    rag = pool.submit(message, roots[0], question)
                    if not entered.wait(10):
                        raise RuntimeError('rerank observation timeout')
                    response, elapsed = message(roots[1], '查询订单 ORD-1001')
                    assert response['status'] == 'answered'
                    blocked.append(elapsed)
                    rag.result()
        finally:
            models.scores = original
        start = perf_counter()
        policy, _ = message(roots[0], question)
        message(roots[0], '查询订单 ORD-1001')
        draft, _ = message(roots[0], '创建售后工单 ORD-1001，原因：超期人工核对')
        written, _ = post(roots[0] + '/task/confirm', draft['draft']['confirmation'])
        assert written['status'] == 'succeeded'
        ticket_id = written['data']['ticket']['ticket_id']
        result, _ = message(roots[0], '查询工单 ' + ticket_id)
        assert result['data']['ticket']['status'] == 'waiting_for_agent'
        with closing(agent.notifications.service.database.connect()) as connection:
            counts = {table: connection.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0]
                      for table in ('support_tickets', 'ticket_orders', 'operation_receipts')}
        assert set(counts.values()) == {1}
        return {'rag_response': stats(rag_times), 'order_response_alone': stats(query_times),
                'order_response_while_other_session_reranks': stats(blocked),
                'five_request_journey_ms': round((perf_counter() - start) * 1000, 2),
                'database_counts': counts, 'source_id': policy['evidence'][0]['source_id'],
                'scope': 'warm models, real localhost TCP, synthetic SQLite, no user think time or LLM'}
    finally:
        server.should_exit = True
        thread.join(10)
        sock.close()
        if thread.is_alive():
            raise RuntimeError('HTTP shutdown did not finish')


def answerability(knowledge, samples):
    results = []
    for case in samples:
        response = knowledge.answer(DEMO_IDENTITY, case['query'])
        actual = [item['source_id'] for item in response.evidence]
        results.append({'query': case['query'], 'expected': case['evidence'],
                        'status': response.status, 'returned_sources': actual,
                        'all_expected_returned': set(case['evidence']) <= set(actual)})
    return {'n': len(results), 'answered': sum(r['status'] == 'answered' for r in results),
            'no_evidence': sum(r['status'] == 'no_evidence' for r in results),
            'all_expected_returned': sum(r['all_expected_returned'] for r in results), 'rows': results}


def main():
    from saas_support_agent.repositories.database import Database
    parser = ArgumentParser()
    parser.add_argument('--output', type=Path, default=Path('runtime/release-evaluation.json'))
    args = parser.parse_args()
    catalog = Path('data/merchant_policies/catalog.json')
    originals = Path('data/merchant_rag_eval/heldout.jsonl')
    probes = Path('data/merchant_rag_eval/temporal_probes.jsonl')
    boundary_path = Path('data/merchant_rag_eval/boundaries.jsonl')
    models = LocalModels(Path('runtime/rag/models'))
    with TemporaryDirectory() as temp, closing(PolicyIndex(Path('runtime/rag/index'), catalog, models)) as index:
        db = Database(Path(temp) / 'evaluation.sqlite3')
        db.initialize()
        seed_demo(db)
        agent = MerchantSupport(db)
        knowledge = PolicyKnowledge(index, agent.notifications.service, today=lambda: date(2026, 11, 6))
        agent.knowledge = knowledge
        base, profile, boundaries = evaluate(index, knowledge, agent, cases(originals), cases(boundary_path))
        extra, _, _ = evaluate(index, knowledge, agent, cases(probes), [])
        diagnostics = [inspect_query(index, knowledge, c['query']) for c in [cases(originals)[2], *cases(probes)]]
        experiment = {'original': heading_experiment(index, knowledge, cases(originals)),
                      'probes': heading_experiment(index, knowledge, cases(probes))}
        answers = {'original': answerability(knowledge, cases(originals)),
                   'probes': answerability(knowledge, cases(probes))}
        http = http_measurements(agent, models)
        report = {'date': '2026-11-07', 'policy_as_of': '2026-11-06', 'python': platform.python_version(),
                  'machine': platform.machine(), 'models': SNAPSHOTS,
                  'dataset_sha256': {str(p): digest(p) for p in (originals, probes, boundary_path)},
                  'index_generation': Path('runtime/rag/index/current').read_text().strip(),
                  'original_queries': 24, 'probe_queries': 12,
                  'probe_scope': 'exploratory synthetic tests added after observing failure; not an independent heldout set',
                  'original': base, 'probes': extra, 'answerability': answers, 'boundaries': boundaries, 'diagnostics': diagnostics,
                  'heading_rerank_experiment_not_applied': experiment, 'http': http,
                  'models_load_s': round(models.load_seconds, 3),
                  'peak_rss_bytes_macos': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                  'candidate_width_p50_ms': profile}
        if any(row['actual'] != row['expected'] or row['source_ids'] for row in boundaries):
            raise RuntimeError('boundary regression')
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open('x', encoding='utf-8') as stream:
            json.dump(report, stream, ensure_ascii=False, indent=2)
        print(json.dumps({k: report[k] for k in ('original','probes','heading_rerank_experiment_not_applied','http')}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
