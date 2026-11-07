"""HTTP 边界使用临时 SQLite、合成身份和真实原业务服务。"""
from contextlib import closing
from pathlib import Path
import os
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

try:
    from fastapi.testclient import TestClient
    from saas_support_agent.api.app import create_app
except ImportError:
    TestClient = None

from saas_support_agent.merchant_demo import seed_demo
from saas_support_agent.merchant_support import MerchantSupport
from saas_support_agent.repositories.database import Database

TOKEN = "local-test-merchant-token-0001"
OTHER = "local-test-other-user-token-02"


@unittest.skipIf(TestClient is None, "需要可选 api 依赖")
class HttpApiTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.db = Database(Path(temp.name) / "test.sqlite3")
        self.db.initialize()
        seed_demo(self.db)
        self.sql("INSERT INTO users VALUES ('other','Other','active')")
        self.sql("INSERT INTO memberships VALUES ('m2','demo-workspace','other','member','active',1)")
        self.support = MerchantSupport(self.db)
        self.client = self.enterContext(TestClient(create_app(self.support, {TOKEN: 'demo-merchant', OTHER: 'other'})))
        self.headers = {"Authorization": f"Bearer {TOKEN}"}
        self.sid = self.client.post('/v1/sessions', headers=self.headers,
                                    json={'workspace_id': 'demo-workspace'}).json()['session_id']
        self.base = f'/v1/sessions/{self.sid}'

    def sql(self, sql):
        with closing(self.db.connect()) as conn:
            return conn.execute(sql).fetchall()

    def message(self, text):
        return self.client.post(self.base + '/messages', headers=self.headers, json={'text': text})

    def control(self, command, body):
        return self.client.post(self.base + '/task/' + command, headers=self.headers, json=body)

    def draft(self):
        result = self.message('创建售后工单 ORD-1001，原因：超期需要人工核对')
        self.assertEqual(result.status_code, 200, result.text)
        return result.json()['draft']['confirmation']

    def test_end_to_end_and_duplicate_confirmation(self):
        self.assertTrue(self.message('退货超期能不能受理？').json()['evidence'])
        self.assertEqual(self.message('查询订单 ORD-1001').json()['data']['order']['status'], 'delivered')
        binding = self.draft()
        written = self.control('confirm', binding).json()
        self.assertEqual(written['status'], 'succeeded')
        again = self.control('confirm', binding).json()
        self.assertEqual(again['receipt']['receipt_id'], written['receipt']['receipt_id'])
        ticket = written['data']['ticket']['ticket_id']
        self.assertEqual(self.message(f'查询工单 {ticket}').json()['data']['ticket']['status'], 'waiting_for_agent')
        self.assertEqual(self.sql('SELECT COUNT(*) FROM support_tickets')[0][0], 1)
        self.assertEqual(self.sql('SELECT COUNT(*) FROM operation_receipts')[0][0], 1)

    def test_auth_session_and_workspace_boundaries(self):
        self.assertEqual(self.client.get(self.base + '/task').status_code, 401)
        self.assertEqual(self.client.get(self.base + '/task', headers={'Authorization': f'Bearer {OTHER}'}).status_code, 404)
        self.assertEqual(self.client.post('/v1/sessions', headers=self.headers,
                                         json={'workspace_id': 'not-mine'}).status_code, 403)
        self.assertEqual(self.client.post('/v1/sessions', headers=self.headers,
                                         json={'workspace_id': 'demo-workspace', 'user_id': 'other'}).status_code, 422)
        self.assertEqual(self.message('查询订单 SHOP-2 ORD-2001').status_code, 403)
        self.sql("UPDATE memberships SET status='inactive' WHERE user_id='demo-merchant'")
        self.assertEqual(self.client.get(self.base + '/task', headers=self.headers).status_code, 403)

    def test_chat_cannot_confirm_and_binding_cannot_be_forged(self):
        binding = self.draft()
        for text in ('确认', '/确认 ' + binding['operation_id']):
            self.assertNotEqual(self.message(text).json()['status'], 'succeeded')
        self.assertEqual(self.control('confirm', {**binding, 'revision': binding['revision'] + 1}).status_code, 409)
        self.assertEqual(self.control('confirm', {**binding, 'payload_hash': '0' * 64}).status_code, 409)
        self.assertEqual(self.sql('SELECT COUNT(*) FROM support_tickets')[0][0], 0)

    def test_query_invalidates_display_resume_then_confirm(self):
        binding = self.draft()
        self.message('查询订单 ORD-1001')
        self.assertEqual(self.control('confirm', binding).status_code, 409)
        fresh = self.control('resume', {'task_id': binding['task_id']}).json()['draft']['confirmation']
        self.assertEqual(self.control('confirm', fresh).json()['status'], 'succeeded')

    def test_fill_slots_revision_cancel_and_pending_write(self):
        result = self.message('创建售后工单').json()
        task_id = result['task']['task_id']
        response = self.client.patch(self.base + '/task/slots', headers=self.headers,
                                     json={'task_id': task_id, 'slots': {'order_id': 'ORD-1001', 'description': '超期核对'}})
        binding = response.json()['draft']['confirmation']
        self.assertEqual(self.message('关闭任务指派邮件通知').status_code, 409)
        revised = self.client.patch(self.base + '/task/slots', headers=self.headers,
                                    json={'task_id': task_id, 'slots': {'description': '修改后的描述'}})
        self.assertGreater(revised.json()['draft']['confirmation']['revision'], binding['revision'])
        self.assertEqual(self.control('confirm', binding).status_code, 409)
        self.assertEqual(self.control('cancel', {'task_id': task_id}).json()['status'], 'cancelled')
        self.assertEqual(self.sql('SELECT COUNT(*) FROM support_tickets')[0][0], 0)

    def test_business_change_requires_new_confirmation(self):
        binding = self.draft()
        self.sql("UPDATE orders SET version=version+1 WHERE order_id='ORD-1001'")
        response = self.control('confirm', binding)
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(self.sql('SELECT COUNT(*) FROM support_tickets')[0][0], 0)
        fresh = response.json()['draft']['confirmation']
        self.assertEqual(self.control('confirm', fresh).json()['status'], 'succeeded')

    def test_transaction_rollback_no_success(self):
        binding = self.draft()
        with patch.object(self.support.tickets.service.receipts, 'save', side_effect=sqlite3.OperationalError('injected')):
            result = self.control('confirm', binding)
        self.assertNotEqual(result.json()['status'], 'succeeded')
        self.assertEqual(self.sql('SELECT COUNT(*) FROM support_tickets')[0][0], 0)
        self.assertEqual(self.sql('SELECT COUNT(*) FROM ticket_orders')[0][0], 0)
        self.assertEqual(self.sql('SELECT COUNT(*) FROM operation_receipts')[0][0], 0)

    def test_notification_update_and_receipt(self):
        binding = self.message('关闭任务指派邮件通知').json()['draft']['confirmation']
        written = self.control('confirm', binding).json()
        self.assertEqual(written['status'], 'succeeded')
        self.assertFalse(self.message('查询任务指派邮件通知').json()['data']['enabled'])
        self.assertEqual(written['receipt']['operation_id'], binding['operation_id'])

    def test_no_evidence_failure_and_safe_validation(self):
        self.assertEqual(self.message('咨询海外税费？').json()['status'], 'no_evidence')
        response = self.client.post(self.base + '/messages', headers=self.headers, json={'text': 123, 'secret': 'private'})
        self.assertEqual(response.status_code, 422)
        self.assertNotIn('private', response.text)
        self.assertIn('X-Request-ID', response.headers)
        with patch.object(self.support, 'http_request', side_effect=RuntimeError('private-path')):
            response = self.message('查询订单 ORD-1001')
        self.assertEqual(response.status_code, 503)
        self.assertNotIn('private-path', response.text)

    def test_session_separation_and_cross_session_confirmation(self):
        binding = self.draft()
        other = self.client.post('/v1/sessions', headers=self.headers,
                                 json={'workspace_id': 'demo-workspace'}).json()['session_id']
        response = self.client.post(f'/v1/sessions/{other}/task/confirm', headers=self.headers, json=binding)
        self.assertEqual(response.status_code, 409)
        self.assertIsNone(self.client.get(f'/v1/sessions/{other}/task', headers=self.headers).json()['task'])
        self.assertEqual(self.sql('SELECT COUNT(*) FROM support_tickets')[0][0], 0)

    def test_shop_revocation_between_prepare_and_confirm(self):
        binding = self.draft()
        self.sql('DELETE FROM shop_access')
        self.assertEqual(self.control('confirm', binding).status_code, 403)
        self.assertEqual(self.sql('SELECT COUNT(*) FROM support_tickets')[0][0], 0)

    def test_lost_commit_acknowledgement_reconciles_without_duplicate(self):
        binding = self.draft()
        def lost_ack(connection):
            Database.commit(connection)
            raise sqlite3.OperationalError('lost acknowledgement')
        with patch.object(self.db, 'commit', side_effect=lost_ack):
            result = self.control('confirm', binding)
        self.assertEqual(result.json()['status'], 'succeeded', result.text)
        self.assertEqual(self.control('confirm', binding).json()['receipt'], result.json()['receipt'])
        self.assertEqual(self.sql('SELECT COUNT(*) FROM support_tickets')[0][0], 1)

    def test_expired_confirmation_requires_review(self):
        from datetime import timedelta
        binding = self.draft()
        service = self.support.tickets.service
        now = service.clock()
        service.clock = lambda: now + timedelta(days=1)
        response = self.control('confirm', binding)
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()['error_code'], 'confirmation_expired')
        self.assertEqual(self.sql('SELECT COUNT(*) FROM support_tickets')[0][0], 0)

    def test_pending_resolution_blocks_cancel_until_reconciliation(self):
        binding = self.draft()
        service = self.support.tickets.service
        from saas_support_agent.core.models import OperationResult
        def lose_ack(connection):
            Database.commit(connection)
            raise sqlite3.OperationalError('lost acknowledgement')
        def unavailable(identity, pending):
            return OperationResult(identity.request_id, 'pending_resolution', '等待核对',
                                   error_code='reconciliation_unavailable')
        with patch.object(self.db, 'commit', side_effect=lose_ack), patch.object(service, 'reconcile', side_effect=unavailable):
            response = self.control('confirm', binding)
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(self.control('cancel', {'task_id': binding['task_id']}).status_code, 409)
        recovered = self.control('reconcile', {'task_id': binding['task_id']})
        self.assertEqual(recovered.json()['status'], 'succeeded', recovered.text)
        self.assertEqual(recovered.json()['receipt']['operation_id'], binding['operation_id'])
        self.assertEqual(self.sql('SELECT COUNT(*) FROM support_tickets')[0][0], 1)

    def test_concurrent_same_confirmation_writes_once(self):
        from concurrent.futures import ThreadPoolExecutor
        binding = self.draft()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.control('confirm', binding).json(), range(2)))
        self.assertTrue(all(r['status'] == 'succeeded' for r in results))
        self.assertEqual(results[0]['receipt']['receipt_id'], results[1]['receipt']['receipt_id'])
        self.assertEqual(self.sql('SELECT COUNT(*) FROM support_tickets')[0][0], 1)

    @unittest.skipUnless(os.getenv('SAAS_RAG_INTEGRATION') == '1', '需要本地 RAG 模型和索引')
    def test_real_rag_over_http_and_fault_mapping(self):
        from datetime import date
        from saas_support_agent.knowledge.models import LocalModels
        from saas_support_agent.knowledge.retrieval import PolicyIndex, PolicyKnowledge
        models = LocalModels(Path('runtime/rag/models'))
        index = PolicyIndex(Path('runtime/rag/index'), Path('data/merchant_policies/catalog.json'), models)
        self.addCleanup(index.close)
        self.support.knowledge = PolicyKnowledge(index, self.support.notifications.service, today=lambda: date(2026, 11, 6))
        answer = self.message('退货超期能不能受理？').json()
        self.assertEqual(answer['evidence'][0]['file'], 'returns-v2.md')
        self.assertEqual(self.message('咨询 SHOP-2 紫盒验收码？').status_code, 403)
        self.assertEqual(self.message('咨询火星天气预报？').json()['status'], 'no_evidence')
        with patch.object(index, 'search', side_effect=OSError('injected')):
            response = self.message('退货超期能不能受理？')
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()['error_code'], 'knowledge_unavailable')
