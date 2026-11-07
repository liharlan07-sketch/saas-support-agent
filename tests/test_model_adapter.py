"""模型替身验收：HTTPX MockTransport 不接触真实模型或外部网络。"""
from contextlib import closing
from datetime import date
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

try:
    import httpx
except ImportError:
    httpx = None

from saas_support_agent.conversation.model import (
    HttpJsonModel, InvalidModelOutput, ModelConfig, ModelUnavailable, configured_model, decode_object,
)
from saas_support_agent.conversation.understanding import ModelUnderstanding
from saas_support_agent.knowledge.generation import EvidenceGenerator
from saas_support_agent.knowledge.retrieval import PolicyKnowledge
from saas_support_agent.merchant_demo import seed_demo
from saas_support_agent.merchant_support import MerchantSupport
from saas_support_agent.core.models import TrustedIdentity
from saas_support_agent.repositories.database import Database

IDENTITY = TrustedIdentity('demo-merchant', 'demo-workspace', 'session', 'request')
EVIDENCE = {'source_id': 'returns:2.0:2', 'text': '超期退货可以创建工单，由人工核对；不代表批准退款。',
            'title': '合成政策', 'version': '2.0', 'file': 'returns-v2.md', 'page': None, 'section': '超期'}


def parsed(intent, slots=None, confidence=0.9):
    return json.dumps({'intent': intent, 'slots': slots or {}, 'confidence': confidence})


def generated(**changes):
    sentence = {'text': '可以提交工单等待人工核对。', 'source_id': EVIDENCE['source_id'],
                'quote': EVIDENCE['text']}
    sentence.update(changes)
    return json.dumps({'sentences': [sentence]})


class FakeModel:
    def __init__(self, output):
        self.output = output
        self.calls = []

    def complete(self, system, payload):
        self.calls.append((system, payload))
        if isinstance(self.output, Exception):
            raise self.output
        return self.output


class FakeIndex:
    """仅供边界注入，不作为检索质量或真实模型证据。"""
    def __init__(self):
        self.allowed = {1}
        self.fail_cite = False

    def eligible(self, workspace, shops, today):
        return self.allowed

    def search(self, *args):
        return [({'id': 1, **EVIDENCE}, 1.0)]

    def cite(self, row):
        if self.fail_cite:
            raise ValueError('changed source')
        return dict(EVIDENCE)


class ModelSafetyTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.db = Database(Path(temp.name) / 'test.sqlite3')
        self.db.initialize()
        seed_demo(self.db)
        self.model = FakeModel(parsed('unsupported'))
        self.agent = MerchantSupport(self.db, understanding=ModelUnderstanding(self.model))

    def count(self):
        with closing(self.db.connect()) as connection:
            return connection.execute('SELECT COUNT(*) FROM support_tickets').fetchone()[0]

    def send(self, output, text='请处理当前请求'):
        self.model.output = output
        return self.agent.handle(IDENTITY, text, allow_control=False)

    def test_query_uses_database_and_missing_slots_clarify(self):
        result = self.send(parsed('order_query', {'order_id': 'ORD-1001'}))
        self.assertEqual(result.data['order']['status'], 'delivered')
        result = self.send(parsed('order_query', {'shop_id': 'SHOP-1'}))
        self.assertEqual(result.missing_slots, ('order_id',))
        result = self.send(parsed('notification_query', {'channel': 'email'}))
        self.assertEqual(result.missing_slots, ('notification_type',))

    def test_model_draft_only_explicit_confirmation_executes(self):
        first = self.send(parsed('ticket_start', {'order_id': 'ORD-1001'}))
        self.assertIn('description', first.missing_slots)
        result = self.send(parsed('ticket_edit', {'description': '请求超期人工核对'}))
        self.assertEqual(result.status, 'needs_confirmation')
        self.assertEqual(self.count(), 0)
        task_payload = self.model.calls[-1][1]['task']
        self.assertEqual(set(task_payload), {'type', 'status', 'slots', 'missing_slots'})
        self.assertNotIn('payload_hash', json.dumps(task_payload))
        shown = self.agent._displayed[self.agent._key(IDENTITY)]
        binding = {k: getattr(shown, k) for k in ('task_id', 'pending_action_id', 'operation_id', 'revision', 'payload_hash')}
        written, _, _ = self.agent.http_request(IDENTITY, 'confirm', confirmation=binding)
        self.assertEqual(written.status, 'succeeded')
        self.assertEqual(self.count(), 1)

    def test_unknown_tools_identity_fields_and_confirmation_rejected(self):
        outputs = [parsed(name) for name in ('confirm', 'execute', 'refund', 'permission_update', 'cancel')]
        outputs += [parsed('ticket_start', {'user_id': 'admin'}), parsed('order_query', {'workspace_id': 'other'}),
                    json.dumps({'intent': 'policy_query', 'slots': {}, 'confidence': 1, 'tool_calls': []})]
        for value in outputs:
            with self.subTest(value=value):
                self.assertEqual(self.send(value).error_code, 'invalid_model_output')
                self.assertEqual(self.count(), 0)

    def test_wrong_slot_types_low_confidence_and_malformed_json(self):
        outputs = [parsed('notification_update', {'enabled': 'true'}), parsed('order_query', {'order_id': 1}),
                   parsed('order_query', {'order_id': 'ORD-1 OR 1=1'}), parsed('unsupported', confidence=True),
                   '```json\n{}\n```', '[]', '{"intent":"confirm","intent":"policy_query"}',
                   '{"intent":"policy_query","slots":{},"confidence":NaN}']
        for value in outputs:
            with self.subTest(value=value):
                self.assertEqual(self.send(value).error_code, 'invalid_model_output')
        self.assertEqual(self.send(parsed('ticket_start', {'order_id': 'ORD-1001'}, 0.1)).error_code, 'uncertain_intent')
        self.assertEqual(self.count(), 0)

    def test_notification_slots_continue_existing_task_without_execution(self):
        first = self.send(parsed('notification_update', {'channel': 'email'}))
        self.assertEqual(first.status, 'needs_clarification')
        result = self.send(parsed('notification_edit', {'notification_type': 'task_assigned', 'enabled': False}))
        self.assertEqual(result.status, 'needs_confirmation')
        self.assertEqual(result.pending_action.payload.channel, 'email')
        old = result.pending_action
        revised = self.send(parsed('notification_edit', {'enabled': True}))
        self.assertGreater(revised.pending_action.revision, old.revision)
        with closing(self.db.connect()) as connection:
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM operation_receipts').fetchone()[0], 0)

    def test_cross_shop_and_revoked_membership(self):
        self.assertEqual(self.send(parsed('order_query', {'shop_id': 'SHOP-2', 'order_id': 'ORD-2001'})).status, 'denied')
        self.assertEqual(self.send(parsed('ticket_start', {'order_id': 'ORD-2001', 'description': 'test'})).status, 'denied')
        before = len(self.model.calls)
        with closing(self.db.connect()) as connection:
            connection.execute("UPDATE memberships SET status='inactive'")
        self.assertEqual(self.send(parsed('policy_query')).status, 'denied')
        self.assertEqual(len(self.model.calls), before)

    def test_model_outage_falls_back_only_to_known_offline_sentences(self):
        result = self.send(ModelUnavailable(), '查询订单 ORD-1001')
        self.assertEqual(result.data['order']['order_id'], 'ORD-1001')
        self.assertEqual(result.data['understanding_mode'], 'offline_fallback')
        self.assertEqual(self.send(ModelUnavailable(), '替我搞定这事').error_code, 'model_unavailable')
        self.assertNotEqual(self.send(ModelUnavailable(), '/确认 00000000-0000-0000-0000-000000000000').status, 'succeeded')
        self.assertEqual(self.count(), 0)

    def knowledge(self, output):
        self.generator_model = FakeModel(output)
        self.index = FakeIndex()
        return PolicyKnowledge(self.index, self.agent.notifications.service,
                               generator=EvidenceGenerator(self.generator_model))

    def test_generation_uses_only_current_sources(self):
        knowledge = self.knowledge(generated())
        result = knowledge.answer(IDENTITY, '退货超期怎么处理？')
        self.assertEqual(result.data['answer_mode'], 'model_with_evidence')
        self.assertEqual(result.evidence, (EVIDENCE,))
        self.assertIn('[1]', result.message)
        self.assertEqual(self.generator_model.calls[0][1]['evidence'], [EVIDENCE])

    def test_false_citations_and_timeout_fall_back_to_original_extract(self):
        for output in (generated(source_id='invented'), generated(quote='并不存在的政策'),
                       generated(text='可以退款 [999]'), '{}', ModelUnavailable()):
            with self.subTest(output=repr(output)):
                result = self.knowledge(output).answer(IDENTITY, '政策？')
                self.assertEqual(result.data['answer_mode'], 'extractive_fallback')
                self.assertIn(EVIDENCE['text'], result.message)
                self.assertNotIn('invented', result.message)

    def test_no_evidence_and_index_failure_do_not_generate(self):
        knowledge = self.knowledge(generated())
        self.index.allowed = set()
        self.assertEqual(knowledge.answer(IDENTITY, '政策？').status, 'no_evidence')
        self.assertFalse(self.generator_model.calls)
        with patch.object(self.index, 'search', side_effect=OSError()):
            self.assertEqual(knowledge.answer(IDENTITY, '政策？').error_code, 'knowledge_unavailable')
        self.assertFalse(self.generator_model.calls)

    def test_permission_or_source_changes_during_generation_block_answer(self):
        for change in ('permission', 'source', 'expiry'):
            knowledge = self.knowledge(generated())
            def generate(system, payload):
                if change == 'permission':
                    with closing(self.db.connect()) as connection:
                        connection.execute("UPDATE memberships SET status='inactive'")
                elif change == 'source':
                    self.index.fail_cite = True
                else:
                    self.index.allowed = set()
                return generated()
            with closing(self.db.connect()) as connection:
                connection.execute("UPDATE memberships SET status='active'")
            with patch.object(self.generator_model, 'complete', side_effect=generate):
                result = knowledge.answer(IDENTITY, '政策？')
            self.assertNotEqual(result.status, 'answered')
            self.assertFalse(result.evidence)

    def test_revoked_order_context_is_not_sent_to_model(self):
        self.send(parsed('ticket_start', {'order_id': 'ORD-1001'}))
        calls = len(self.model.calls)
        with closing(self.db.connect()) as connection:
            connection.execute('DELETE FROM shop_access')
        result = self.send(parsed('ticket_edit', {'description': '人工核对'}))
        self.assertEqual(result.status, 'denied')
        self.assertEqual(len(self.model.calls), calls)

    @unittest.skipUnless(os.getenv('SAAS_RAG_INTEGRATION') == '1', '需要已准备的本地检索模型')
    def test_real_rag_with_fake_generator_and_fallback(self):
        from saas_support_agent.knowledge.models import LocalModels
        from saas_support_agent.knowledge.retrieval import PolicyIndex
        local = LocalModels(Path('runtime/rag/models'))
        index = PolicyIndex(Path('runtime/rag/index'), Path('data/merchant_policies/catalog.json'), local)
        self.addCleanup(index.close)
        fake = FakeModel('')
        def complete(system, payload):
            evidence = payload['evidence'][0]
            return json.dumps({'sentences': [{'text': '请参考当前政策并提交人工核对。',
                                             'source_id': evidence['source_id'], 'quote': evidence['text']}]})
        knowledge = PolicyKnowledge(index, self.agent.notifications.service, today=lambda: date(2026, 11, 6),
                                    generator=EvidenceGenerator(fake))
        with patch.object(fake, 'complete', side_effect=complete):
            answer = knowledge.answer(IDENTITY, '退货超期能不能受理？')
        self.assertEqual(answer.data['answer_mode'], 'model_with_evidence')
        self.assertEqual(answer.evidence[0]['file'], 'returns-v2.md')
        fake.output = ModelUnavailable()
        answer = knowledge.answer(IDENTITY, '退货超期能不能受理？')
        self.assertEqual(answer.data['answer_mode'], 'extractive_fallback')
        self.assertIn(answer.evidence[0]['text'], answer.message)

    def test_http_fake_model_cannot_confirm_or_execute(self):
        try:
            from fastapi.testclient import TestClient
            from saas_support_agent.api.app import create_app
        except ImportError:
            self.skipTest('需要 api extra')
        token = 'local-model-test-synthetic-token'
        headers = {'Authorization': 'Bearer ' + token}
        with TestClient(create_app(self.agent, {token: 'demo-merchant'})) as client:
            sid = client.post('/v1/sessions', headers=headers, json={'workspace_id': 'demo-workspace'}).json()['session_id']
            root = f'/v1/sessions/{sid}'
            self.model.output = parsed('ticket_start', {'order_id': 'ORD-1001', 'description': '超期人工核对'})
            draft = client.post(root + '/messages', headers=headers, json={'text': '帮我受理一下这笔订单'}).json()['draft']
            self.model.output = parsed('confirm')
            rejected = client.post(root + '/messages', headers=headers, json={'text': '确认'})
            self.assertEqual(rejected.json()['error_code'], 'invalid_model_output')
            self.assertEqual(self.count(), 0)
            self.model.output = parsed('policy_query')
            client.post(root + '/messages', headers=headers, json={'text': '这是什么政策？'})
            self.assertEqual(client.post(root + '/task/confirm', headers=headers, json=draft['confirmation']).status_code, 409)



@unittest.skipIf(httpx is None, '需要已授权的 HTTPX')
class HttpModelProtocolTests(unittest.TestCase):
    def client(self, handler):
        model = HttpJsonModel(ModelConfig('https://model.invalid/chat/completions', 'test-model', 'synthetic-key'),
                              transport=httpx.MockTransport(handler))
        self.addCleanup(model.close)
        return model

    def test_request_and_response_contract_no_real_network(self):
        requests = []
        def handler(request):
            requests.append(request)
            body = json.loads(request.content)
            self.assertNotIn('tools', body)
            self.assertEqual(body['model'], 'test-model')
            return httpx.Response(200, json={'choices': [{'finish_reason': 'stop', 'message': {'content': parsed('policy_query')}}]})
        model = self.client(handler)
        self.assertEqual(ModelUnderstanding(model).recognize('政策？').intent, 'policy_query')
        self.assertEqual(len(requests), 1)

    def test_transport_timeout_status_redirect_and_tool_calls(self):
        def timeout(request):
            raise httpx.ReadTimeout('synthetic timeout')
        for handler in (timeout, lambda r: httpx.Response(500),
                        lambda r: httpx.Response(307, headers={'Location': 'https://elsewhere.invalid'})):
            with self.assertRaises(ModelUnavailable):
                self.client(handler).complete('system', {})
        envelopes = [
            {'choices': [{'finish_reason': 'tool_calls', 'message': {'tool_calls': [{'name': 'confirm'}]}}]},
            {'choices': [{'finish_reason': 'stop', 'message': {'content': '{}', 'tool_calls': [{'name': 'refund'}]}}]},
            {'choices': [{'finish_reason': 'length', 'message': {'content': '{}'}}]},
            {'choices': [None]},
        ]
        for envelope in envelopes:
            with self.assertRaises(InvalidModelOutput):
                self.client(lambda r: httpx.Response(200, json=envelope)).complete('system', {})

    def test_disabled_or_incomplete_configuration_never_constructs_client(self):
        with patch.object(httpx, 'Client', side_effect=AssertionError('network client forbidden')):
            self.assertIsNone(configured_model(environ={}))
            self.assertIsNone(configured_model(environ={'SAAS_LLM_ENDPOINT': 'https://example.invalid'}))
            with self.assertRaises(ValueError):
                configured_model(environ={'SAAS_LLM_ENABLED': '1'})
        with self.assertRaises(ValueError):
            ModelConfig('http://example.invalid/api', 'm', 'key')

    def test_response_size_invalid_json_and_duplicate_keys(self):
        for response in (httpx.Response(200, content=b'x' * 128001), httpx.Response(200, content=b'not-json')):
            with self.assertRaises(InvalidModelOutput):
                self.client(lambda r: response).complete('system', {})
        with self.assertRaises(InvalidModelOutput):
            decode_object('{"x":1,"x":2}')
