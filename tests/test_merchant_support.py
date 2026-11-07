"""商家售后闭环及安全边界；全部使用临时 SQLite 和合成数据。"""

from dataclasses import replace
import sqlite3
from unittest.mock import patch

from saas_support_agent.core.models import SupportTicketRequest
from saas_support_agent.merchant_support import MerchantSupport
from saas_support_agent.repositories.database import SCHEMA
from saas_support_agent.services.support_ticket import SupportTicketService
from saas_support_agent.workflows.support_ticket import SupportTicketWorkflow
from test_notification_workflow import DatabaseCase


class MerchantSupportTests(DatabaseCase):
    def setUp(self):
        super().setUp()
        self.sql("INSERT INTO shops VALUES ('w1','SHOP-1','可见店铺','active',1),"
                 "('w1','SHOP-2','不可见店铺','active',1),('w2','SHOP-1','另一空间','active',1)")
        self.sql("INSERT INTO shop_access VALUES ('w1','SHOP-1','u1',1),"
                 "('w1','SHOP-2','u2',1),('w2','SHOP-1','u1',1)")
        self.sql("INSERT INTO orders VALUES ('w1','SHOP-1','ORD-1001','delivered','2026-10-20',1),"
                 "('w1','SHOP-2','ORD-2001','shipped',NULL,1),"
                 "('w2','SHOP-1','ORD-1001','closed',NULL,1)")
        self.agent = MerchantSupport(self.database)
        self.agent.tickets.service.clock = self.clock
        self.agent.notifications.service.clock = self.clock

    def send(self, text, identity=None):
        return self.agent.handle(identity or self.identity, text)

    def draft(self):
        result = self.send("创建售后工单 ORD-1001，原因：超期，需要人工核对")
        self.assertEqual(result.status, "needs_confirmation", result)
        return result.pending_action

    def commit_draft(self, draft, identity=None):
        return self.send(f"/确认 {draft.operation_id}", identity)

    def assert_no_ticket(self):
        self.assertEqual(self.sql("SELECT COUNT(*) FROM support_tickets")[0][0], 0)
        self.assertEqual(self.sql("SELECT COUNT(*) FROM ticket_orders")[0][0], 0)

    def test_policy_order_ticket_and_readback(self):
        policy = self.send("退货超期能不能受理？")
        self.assertEqual(policy.evidence[0]["source_id"], "demo-return-policy:1.0:late-return")
        self.assertEqual(self.send("咨询海外税费？").status, "no_evidence")
        self.assertEqual(self.send("查询订单 ORD-1001").data["order"]["status"], "delivered")
        self.sql("UPDATE orders SET status='closed', version=2 WHERE workspace_id='w1' AND order_id='ORD-1001'")
        self.assertEqual(self.send("查询订单 ORD-1001").data["order"]["status"], "closed")
        draft = self.draft()
        self.assertIn("SHOP-1；订单：ORD-1001", draft.displayed_summary)
        self.assert_no_ticket()
        self.assertEqual(self.send("确认").status, "unsupported")
        self.assert_no_ticket()
        written = self.commit_draft(draft)
        self.assertEqual(written.status, "succeeded")
        ticket = written.data["ticket"]
        self.assertEqual(ticket["status"], "waiting_for_agent")
        readback = self.send(f"查询工单 {ticket['ticket_id']} 状态")
        self.assertEqual(readback.data["ticket"], ticket)
        self.assertEqual(self.commit_draft(draft).receipt.receipt_id, written.receipt.receipt_id)
        self.assertEqual(tuple(self.sql("SELECT shop_id, order_id FROM ticket_orders")[0]), ("SHOP-1", "ORD-1001"))
        self.assertEqual(self.sql("SELECT COUNT(*) FROM operation_receipts")[0][0], 1)

    def test_isolation_and_ambiguous_order(self):
        hidden = self.send("查询订单 ORD-2001")
        missing = self.send("查询订单 ORD-9999")
        self.assertEqual((hidden.status, hidden.error_code, hidden.message),
                         (missing.status, missing.error_code, missing.message))
        self.assertEqual(self.send("查询订单 ORD-1001", replace(self.identity, workspace_id="w2")).data["order"]["status"],
                         "closed")
        self.assertEqual(self.send("查询订单 ORD-1001", replace(self.identity, user_id="u2")).status, "denied")
        self.assertEqual(self.send("创建售后工单 ORD-2001，原因：越权").status, "denied")
        self.sql("INSERT INTO shop_access VALUES ('w1','SHOP-2','u1',1)")
        self.sql("INSERT INTO orders VALUES ('w1','SHOP-2','ORD-1001','shipped',NULL,1)")
        self.assertEqual(self.send("查询订单 ORD-1001").error_code, "ambiguous_order")
        self.assertEqual(self.send("查询订单 SHOP-2/ORD-1001").data["order"]["shop_id"], "SHOP-2")
        self.assert_no_ticket()

    def test_query_interrupt_revision_and_single_write(self):
        missing = self.send("创建售后工单")
        self.assertEqual(missing.missing_slots, ("description", "shop_id", "order_id"))
        self.assertEqual(self.send("开启任务指派邮件通知").error_code, "pending_write_exists")
        result = self.send("/补充 ORD-1001，原因：超期待核对")
        old = result.pending_action
        self.assertIsNotNone(old, result)
        self.send("查询订单 ORD-1001")
        self.assertEqual(self.commit_draft(old).error_code, "stale_confirmation")
        self.assert_no_ticket()
        self.assertEqual(self.send("/恢复").pending_action, old)
        updated = self.send("/补充 原因：已补充证据，请人工核对").pending_action
        self.assertEqual(updated.revision, old.revision + 1)
        self.assertEqual(self.commit_draft(old).error_code, "stale_confirmation")
        self.assertEqual(self.send("/取消").status, "cancelled")
        self.assertEqual(self.commit_draft(updated).error_code, "stale_confirmation")
        self.assert_no_ticket()
        notification = self.send("开启任务指派邮件通知").pending_action
        self.assertEqual(self.send("创建售后工单 ORD-1001，原因：需要人工").error_code, "pending_write_exists")
        self.assertEqual(self.commit_draft(notification).status, "succeeded")
        self.assertTrue(self.send("查询任务指派邮件通知").data["enabled"])

    def test_revocation_blocks_confirm_and_later_reads(self):
        draft = self.draft()
        self.assertEqual(self.commit_draft(draft, replace(self.identity, session_id="other")).error_code,
                         "stale_confirmation")
        self.sql("DELETE FROM shop_access WHERE workspace_id='w1' AND shop_id='SHOP-1'")
        self.assertEqual(self.commit_draft(draft).status, "denied")
        self.assert_no_ticket()
        self.sql("INSERT INTO shop_access VALUES ('w1','SHOP-1','u1',2)")
        new = self.draft()
        ticket = self.commit_draft(new).data["ticket"]
        self.assertEqual(self.send(f"查询工单 {ticket['ticket_id']}", replace(self.identity, user_id="u2")).status, "denied")
        self.sql("DELETE FROM shop_access WHERE workspace_id='w1' AND shop_id='SHOP-1'")
        self.assertEqual(self.send(f"查询工单 {ticket['ticket_id']}").status, "denied")
        self.assertEqual(self.commit_draft(new).status, "denied")
        self.assertEqual(self.agent.tickets.service.query_receipt(self.identity, new.operation_id).status, "denied")

    def test_order_change_requires_fresh_confirmation(self):
        draft = self.draft()
        self.sql("UPDATE orders SET status='closed', version=2 WHERE workspace_id='w1' AND order_id='ORD-1001'")
        refresh = self.commit_draft(draft)
        self.assertEqual(refresh.error_code, "business_state_changed")
        self.assert_no_ticket()
        self.assertEqual(self.commit_draft(draft).error_code, "stale_confirmation")
        self.assertEqual(self.commit_draft(refresh.pending_action).status, "succeeded")

    def test_ticket_and_relation_roll_back_when_receipt_fails(self):
        draft = self.draft()
        with patch.object(self.agent.tickets.service.receipts, "save", side_effect=sqlite3.OperationalError("injected")):
            failed = self.commit_draft(draft)
        self.assertEqual(failed.status, "failed")
        self.assert_no_ticket()
        self.assertEqual(self.sql("SELECT COUNT(*) FROM operation_receipts")[0][0], 0)

    def test_lost_commit_ack_recovers_ticket_without_repeating_write(self):
        draft = self.draft()
        original_commit = self.database.commit

        def lost_ack(connection):
            original_commit(connection)
            raise sqlite3.OperationalError("injected")

        with patch.object(self.database, "commit", side_effect=lost_ack):
            result = self.commit_draft(draft)
        self.assertEqual(result.status, "succeeded")
        self.assertEqual(self.commit_draft(draft).receipt.receipt_id, result.receipt.receipt_id)
        self.assertEqual(self.sql("SELECT COUNT(*) FROM ticket_orders")[0][0], 1)

    def test_existing_project_ticket_remains_supported(self):
        self.sql("INSERT INTO projects VALUES ('p1','w1','原项目','active',1)")
        self.sql("INSERT INTO project_access VALUES ('w1','p1','u1','viewer',1)")
        workflow = SupportTicketWorkflow(SupportTicketService(self.database, clock=self.clock))
        draft = workflow.prepare(self.identity, SupportTicketRequest("access", "原项目访问问题", "p1")).pending_action
        self.assertIsNotNone(draft)
        result = workflow.confirm(self.identity, draft.pending_action_id, draft.revision, draft.canonical_payload_hash)
        self.assertEqual(result.status, "succeeded")
        self.assertEqual(result.data["ticket"]["project_id"], "p1")
        self.assertEqual(self.sql("SELECT COUNT(*) FROM ticket_orders")[0][0], 0)

    def test_legacy_notification_receipt_preserved_by_migration(self):
        prior = self.confirm(self.prepare()).receipt
        receipt_schema = SCHEMA[SCHEMA.index("CREATE TABLE IF NOT EXISTS operation_receipts"):].replace(
            "action_type IN ('update_notification_preferences', 'create_support_ticket')",
            "action_type = 'update_notification_preferences'",
        )
        self.sql("ALTER TABLE operation_receipts RENAME TO fixture_receipts")
        self.sql(receipt_schema)
        self.sql("INSERT INTO operation_receipts SELECT * FROM fixture_receipts")
        self.sql("DROP TABLE fixture_receipts")
        self.database.initialize()
        self.database.initialize()
        self.assertEqual(self.service.query_receipt(self.identity, prior.operation_id).receipt, prior)
        self.assertEqual(self.commit_draft(self.draft()).status, "succeeded")
