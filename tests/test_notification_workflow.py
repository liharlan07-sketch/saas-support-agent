"""Behavior tests use real file-backed SQLite; no model calls or external services."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from saas_support_agent.core.models import NotificationUpdateRequest, TrustedIdentity
from saas_support_agent.repositories.database import Database
from saas_support_agent.services.notification_service import NotificationService
from saas_support_agent.workflows.notification_update import NotificationUpdateWorkflow


class MutableClock:
    def __init__(self):
        self.now = datetime(2026, 10, 13, tzinfo=timezone.utc)

    def __call__(self):
        return self.now


class DatabaseCase(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.database = Database(Path(self.directory.name) / "business.sqlite3")
        self.database.initialize()
        self.clock = MutableClock()
        self.service = NotificationService(self.database, clock=self.clock)
        self.workflow = NotificationUpdateWorkflow(self.service)
        self.identity = TrustedIdentity("u1", "w1", "s1", "req1")
        self.request = NotificationUpdateRequest("task_assigned", "email", True)
        connection = self.database.connect()
        try:
            connection.executemany("INSERT INTO users VALUES (?, ?, ?)",
                                   [("u1", "用户一", "active"), ("u2", "用户二", "active")])
            connection.executemany("INSERT INTO workspaces VALUES (?, ?, ?, ?, ?)",
                                   [("w1", "研发", "active", "basic", 1), ("w2", "运营", "active", "basic", 1)])
            connection.executemany("INSERT INTO memberships VALUES (?, ?, ?, ?, ?, ?)",
                                   [("m1", "w1", "u1", "member", "active", 1),
                                    ("m2", "w2", "u1", "member", "active", 1),
                                    ("m3", "w1", "u2", "admin", "active", 1)])
        finally:
            connection.close()

    def sql(self, statement, parameters=()):
        connection = self.database.connect()
        try:
            return connection.execute(statement, parameters).fetchall()
        finally:
            connection.close()

    def prepare(self, request=None, identity=None):
        result = self.workflow.prepare(identity or self.identity, request or self.request)
        self.assertEqual(result.status, "needs_confirmation", result)
        self.assertIsNotNone(result.pending_action)
        return result.pending_action

    def confirm(self, pending, identity=None):
        return self.workflow.confirm(identity or self.identity, pending.pending_action_id,
                                     pending.revision, pending.canonical_payload_hash)

    def counts(self):
        return (
            self.sql("SELECT count(*) FROM notification_preferences")[0][0],
            self.sql("SELECT count(*) FROM operation_receipts")[0][0],
        )

    def assert_no_writes(self):
        self.assertEqual(self.counts(), (0, 0))


class NotificationWorkflowTests(DatabaseCase):
    def test_success_is_persisted_and_readable_from_new_connection(self):
        pending = self.prepare()
        self.assert_no_writes()
        self.assertIn("关闭改为开启", pending.displayed_summary)
        result = self.confirm(pending)
        self.assertEqual(result.status, "succeeded")
        self.assertTrue(result.data["changed"])
        self.assertEqual(result.receipt.operation_id, pending.operation_id)
        reader = NotificationService(Database(self.database.path), clock=self.clock)
        query = reader.query_preference(self.identity, "task_assigned", "email")
        self.assertEqual(query.data["enabled"], True)
        self.assertEqual(query.data["version"], 1)
        receipt = reader.query_receipt(self.identity, pending.operation_id)
        self.assertEqual(receipt.receipt.receipt_id, result.receipt.receipt_id)
        self.assertEqual(receipt.data, result.data)
        self.assertEqual(self.counts(), (1, 1))

    def test_missing_membership_is_denied(self):
        result = self.workflow.prepare(replace(self.identity, user_id="u2", workspace_id="w2"), self.request)
        self.assertEqual(result.status, "denied")
        self.assert_no_writes()

    def test_user_and_workspace_must_be_active(self):
        for table, column, value in [("users", "user_id", "disabled"), ("workspaces", "workspace_id", "suspended")]:
            with self.subTest(table=table):
                identifier = "u1" if table == "users" else "w1"
                self.sql(f"UPDATE {table} SET status = ? WHERE {column} = ?", (value, identifier))
                self.assertEqual(self.workflow.prepare(self.identity, self.request).status, "denied")
                self.sql(f"UPDATE {table} SET status = 'active' WHERE {column} = ?", (identifier,))
        self.assert_no_writes()

    def test_missing_slots_are_reported_without_preparing_action(self):
        result = self.workflow.prepare(self.identity, NotificationUpdateRequest())
        self.assertEqual(result.status, "needs_clarification")
        self.assertEqual(set(result.missing_slots), {"notification_type", "channel", "enabled"})
        self.assertIsNone(result.pending_action)
        self.assert_no_writes()

    def test_invalid_parameters_are_not_coerced(self):
        for request in [replace(self.request, enabled="false"), replace(self.request, enabled=1),
                        replace(self.request, channel="sms"), replace(self.request, notification_type="all"),
                        replace(self.request, notification_type=[]), replace(self.request, channel="EMAIL")]:
            with self.subTest(request=request):
                result = self.workflow.prepare(self.identity, request)
                self.assertEqual(result.error_code, "invalid_parameters")
        self.assert_no_writes()

    def test_request_cannot_specify_another_user_or_project(self):
        with self.assertRaises(TypeError):
            NotificationUpdateRequest("task_assigned", "email", True, user_id="u2")
        with self.assertRaises(TypeError):
            NotificationUpdateRequest("task_assigned", "email", True, project_id="p1")

    def test_cancel_invalidates_confirmation_without_writes(self):
        pending = self.prepare()
        self.assertEqual(self.workflow.cancel(self.identity, pending.pending_action_id).status, "cancelled")
        self.assertEqual(self.confirm(pending).status, "cancelled")
        self.assert_no_writes()

    def test_revise_invalidates_old_confirmation(self):
        old = self.prepare()
        result = self.workflow.revise(self.identity, replace(self.request, channel="in_app"))
        new = result.pending_action
        self.assertEqual(new.pending_action_id, old.pending_action_id)
        self.assertEqual(new.revision, old.revision + 1)
        self.assertNotEqual(new.operation_id, old.operation_id)
        self.assertNotEqual(new.canonical_payload_hash, old.canonical_payload_hash)
        self.assertEqual(self.confirm(old).error_code, "stale_confirmation")
        self.assert_no_writes()
        self.assertEqual(self.confirm(new).status, "succeeded")
        self.assertEqual(self.sql("SELECT channel FROM notification_preferences")[0][0], "in_app")

    def test_invalid_revision_does_not_replace_valid_draft(self):
        pending = self.prepare()
        self.assertEqual(self.workflow.revise(self.identity, replace(self.request, enabled="false")).error_code,
                         "invalid_parameters")
        self.assertEqual(self.confirm(pending).status, "succeeded")

    def test_expiry_rebuilds_but_does_not_execute(self):
        pending = self.prepare()
        self.clock.now += timedelta(minutes=10)
        result = self.confirm(pending)
        self.assertEqual(result.status, "needs_confirmation")
        self.assertEqual(result.error_code, "confirmation_expired")
        self.assertNotEqual(result.pending_action.operation_id, pending.operation_id)
        self.assert_no_writes()
        self.assertEqual(self.confirm(result.pending_action).status, "succeeded")

    def test_duplicate_confirmation_returns_same_receipt_after_expiry(self):
        pending = self.prepare()
        first = self.confirm(pending)
        self.clock.now += timedelta(days=1)
        second = self.confirm(pending, replace(self.identity, request_id="retry"))
        self.assertEqual(second.status, "succeeded")
        self.assertEqual(first.receipt, second.receipt)
        self.assertEqual(second.request_id, "retry")
        self.assertEqual(self.counts(), (1, 1))
        self.assertEqual(self.sql("SELECT version FROM notification_preferences")[0][0], 1)

    def test_other_session_change_requires_new_confirmation_even_if_target_matches(self):
        original = self.prepare()
        other = replace(self.identity, session_id="s2")
        changed = self.prepare(identity=other)
        self.assertEqual(self.confirm(changed, other).status, "succeeded")
        result = self.confirm(original)
        self.assertEqual(result.error_code, "business_state_changed")
        self.assertEqual(result.pending_action.current.version, 1)
        self.assertEqual(self.counts(), (1, 1))
        done = self.confirm(result.pending_action)
        self.assertEqual(done.status, "succeeded")
        self.assertFalse(done.data["changed"])
        self.assertEqual(self.sql("SELECT version FROM notification_preferences")[0][0], 1)

    def test_membership_revoked_after_prepare_prevents_execution(self):
        pending = self.prepare()
        self.sql("UPDATE memberships SET status = 'inactive', version = 2 WHERE membership_id = 'm1'")
        self.assertEqual(self.confirm(pending).status, "denied")
        self.assert_no_writes()

    def test_authorization_version_change_requires_confirmation(self):
        pending = self.prepare()
        self.sql("UPDATE memberships SET version = 2 WHERE membership_id = 'm1'")
        result = self.confirm(pending)
        self.assertEqual(result.error_code, "business_state_changed")
        self.assert_no_writes()

    def test_user_and_workspace_isolation(self):
        pending = self.prepare()
        other_user = replace(self.identity, user_id="u2")
        other_workspace = replace(self.identity, workspace_id="w2")
        self.assertNotEqual(self.confirm(pending, other_user).status, "succeeded")
        self.assertEqual(self.confirm(pending, other_workspace).status, "denied")
        result = self.confirm(pending)
        for identity in [other_user, other_workspace]:
            self.assertEqual(self.service.query_preference(identity, "task_assigned", "email").data["enabled"], False)
            self.assertEqual(self.service.query_receipt(identity, pending.operation_id).status, "denied")
        self.assertEqual(result.status, "succeeded")
        self.assertEqual(self.counts(), (1, 1))

    def test_same_session_name_different_users_have_independent_drafts(self):
        one = self.prepare()
        other = replace(self.identity, user_id="u2")
        two = self.prepare(identity=other)
        self.assertNotEqual(one.operation_id, two.operation_id)
        self.assertEqual(self.confirm(one).status, "succeeded")
        self.assertEqual(self.confirm(two, other).status, "succeeded")
        self.assertEqual(self.counts(), (2, 2))

    def test_second_write_requires_explicit_cancel_before_switch(self):
        first = self.prepare()
        second_request = replace(self.request, channel="in_app")
        self.assertEqual(self.workflow.prepare(self.identity, second_request).error_code, "pending_write_exists")
        self.workflow.cancel(self.identity, first.pending_action_id)
        second = self.prepare(second_request)
        self.assertNotEqual(first.pending_action_id, second.pending_action_id)
        self.assertNotEqual(self.confirm(first).status, "succeeded")
        self.assertEqual(self.confirm(second).status, "succeeded")

    def test_other_workspace_request_does_not_overwrite_pending(self):
        pending = self.prepare()
        other = replace(self.identity, workspace_id="w2")
        blocked = self.workflow.prepare(other, self.request)
        self.assertEqual(blocked.error_code, "pending_write_exists")
        self.assertIsNone(blocked.pending_action)
        self.assertEqual(self.confirm(pending).status, "succeeded")

    def test_new_semantically_identical_operation_gets_new_noop_receipt(self):
        first = self.prepare()
        self.confirm(first)
        second = self.prepare()
        self.assertNotEqual(first.operation_id, second.operation_id)
        result = self.confirm(second)
        self.assertEqual(result.status, "succeeded")
        self.assertFalse(result.data["changed"])
        self.assertEqual(result.data["version"], 1)
        self.assertEqual(self.counts(), (1, 2))

    def test_default_false_noop_does_not_materialize_preference(self):
        result = self.confirm(self.prepare(replace(self.request, enabled=False)))
        self.assertEqual(result.status, "succeeded")
        self.assertFalse(result.data["changed"])
        self.assertEqual(result.data["version"], 0)
        self.assertEqual(self.counts(), (0, 1))

    def test_existing_preference_update_keeps_old_receipt_as_historical_fact(self):
        first = self.prepare()
        self.confirm(first)
        second = self.prepare(replace(self.request, enabled=False))
        result = self.confirm(second)
        self.assertEqual(result.status, "succeeded")
        self.assertTrue(result.data["changed"])
        self.assertEqual((result.data["previous_version"], result.data["version"]), (1, 2))
        self.assertEqual(self.counts(), (1, 2))
        current = self.service.query_preference(self.identity, "task_assigned", "email")
        historical = self.service.query_receipt(self.identity, first.operation_id)
        self.assertFalse(current.data["enabled"])
        self.assertTrue(historical.data["enabled"])

    def test_unrelated_preference_change_does_not_invalidate_confirmation(self):
        pending = self.prepare()
        other = replace(self.identity, session_id="s2")
        other_draft = self.prepare(replace(self.request, channel="in_app"), other)
        self.assertEqual(self.confirm(other_draft, other).status, "succeeded")
        self.assertEqual(self.confirm(pending).status, "succeeded")
        self.assertEqual(self.counts(), (2, 2))

    def test_restart_loses_draft_but_receipt_survives(self):
        pending = self.prepare()
        result = self.confirm(pending)
        restarted = NotificationUpdateWorkflow(NotificationService(Database(self.database.path)))
        self.assertEqual(restarted.confirm(self.identity, pending.pending_action_id, pending.revision,
                                           pending.canonical_payload_hash).error_code, "no_pending_action")
        self.assertEqual(restarted.service.query_receipt(self.identity, pending.operation_id).receipt, result.receipt)

    def test_cancel_after_success_does_not_claim_rollback(self):
        pending = self.prepare()
        self.confirm(pending)
        self.assertEqual(self.workflow.cancel(self.identity, pending.pending_action_id).error_code, "cannot_cancel_execution")
        self.assertEqual(self.sql("SELECT enabled FROM notification_preferences")[0][0], 1)

    def test_query_does_not_implicitly_confirm(self):
        pending = self.prepare()
        self.assertEqual(self.service.query_preference(self.identity, "task_assigned", "email").status, "answered")
        self.assert_no_writes()
        self.assertEqual(self.confirm(pending).status, "succeeded")

    def test_resolving_unexecuted_draft_does_not_trap_it_in_unknown_state(self):
        pending = self.prepare()
        result = self.workflow.resolve(self.identity)
        self.assertEqual(result.status, "needs_confirmation")
        self.assertEqual(self.confirm(pending).status, "succeeded")

    def test_known_failure_without_receipt_stays_failed_on_repeat_confirm(self):
        pending = self.prepare()
        with patch.object(self.database, "connect", side_effect=sqlite3.OperationalError("unavailable")):
            self.assertEqual(self.confirm(pending).status, "failed")
        self.assertEqual(self.confirm(pending).status, "failed")
        self.assert_no_writes()
        self.assertEqual(self.workflow.prepare(self.identity, self.request).status, "needs_confirmation")

    def test_nonexistent_and_out_of_scope_receipts_have_same_external_error(self):
        pending = self.prepare()
        self.confirm(pending)
        invisible = self.service.query_receipt(replace(self.identity, user_id="u2"), pending.operation_id)
        absent = self.service.query_receipt(self.identity, "does-not-exist")
        self.assertEqual((invisible.status, invisible.error_code, invisible.message),
                         (absent.status, absent.error_code, absent.message))


if __name__ == "__main__":
    unittest.main()
