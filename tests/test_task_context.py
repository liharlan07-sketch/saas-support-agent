"""真实临时 SQLite 验证任务生命周期；故障注入不冒充线上事故。"""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from datetime import timedelta
import sqlite3
from unittest.mock import patch

from saas_support_agent.core.models import ACTION_TYPE, OperationResult
from saas_support_agent.workflows.notification_update import NotificationUpdateWorkflow
from saas_support_agent.workflows.task_context import TaskContextManager
from test_notification_workflow import DatabaseCase


class TaskContextTests(DatabaseCase):
    def setUp(self):
        super().setUp()
        self.tasks = TaskContextManager(self.workflow)

    def start_task(self, slots=None, identity=None):
        identity = identity or self.identity
        result = self.tasks.start(identity, asdict(self.request) if slots is None else slots)
        self.assertIn(result.status, {"needs_confirmation", "needs_clarification"})
        return self.tasks.get_context(identity), result.pending_action

    def confirm_task(self, context, pending):
        return self.tasks.confirm(self.identity, context.task_id, pending.pending_action_id,
                                  pending.revision, pending.canonical_payload_hash)

    def test_create_and_collect_without_database_writes(self):
        context, pending = self.start_task({"channel": "email"})
        self.assertIsNone(pending)
        self.assertEqual(context.task_type, ACTION_TYPE)
        self.assertEqual(context.task_status, "collecting")
        self.assertEqual(dict(context.collected_slots), {"channel": "email"})
        self.assertEqual(context.missing_slots, ("notification_type", "enabled"))
        self.assertEqual((context.owner_user_id, context.workspace_id, context.session_id), ("u1", "w1", "s1"))
        self.assert_no_writes()

    def test_fill_slots_retains_task_and_transfers_payload_ownership(self):
        context, _ = self.start_task({"channel": "email"})
        result = self.tasks.update_slots(self.identity, context.task_id, {"notification_type": "task_assigned", "enabled": True})
        current = self.tasks.get_context(self.identity)
        self.assertEqual(current.task_id, context.task_id)
        self.assertEqual(current.pending_action_id, result.pending_action.pending_action_id)
        self.assertEqual(current.task_status, "pending_confirmation")
        self.assertEqual(current.missing_slots, ())
        self.assertIsNone(self.tasks._store.get(self.identity).collected)
        self.assert_no_writes()

    def test_revision_updates_projection_and_old_confirmation_cannot_execute(self):
        context, old = self.start_task()
        result = self.tasks.update_slots(self.identity, context.task_id, {"notification_type": "comment_mentioned"})
        new = result.pending_action
        self.assertEqual(new.revision, old.revision + 1)
        self.assertNotEqual(new.operation_id, old.operation_id)
        self.assertEqual(self.tasks.get_context(self.identity).collected_slots["notification_type"], "comment_mentioned")
        self.assertEqual(self.confirm_task(context, old).error_code, "stale_confirmation")
        self.assert_no_writes()
        self.assertEqual(self.confirm_task(context, new).status, "succeeded")
        self.assertEqual(self.sql('SELECT notification_type FROM notification_preferences')[0][0], 'comment_mentioned')

    def test_invalid_or_incomplete_revision_is_atomic_rejection(self):
        context, pending = self.start_task()
        for slots in ({"enabled": "false"}, {"channel": None}, {"user_id": "u2"}):
            result = self.tasks.update_slots(self.identity, context.task_id, slots)
            self.assertEqual(result.status, "needs_clarification")
            self.assertEqual(self.workflow.inspect_pending(self.identity), pending)
            self.assertEqual(dict(self.tasks.get_context(self.identity).collected_slots), asdict(self.request))
        self.assert_no_writes()

    def test_invalid_partial_input_is_not_saved(self):
        for slots in ({"enabled": 1}, {"channel": []}, {"workspace_id": "w2"}):
            self.assertEqual(self.tasks.start(self.identity, slots).error_code, "invalid_parameters")
            self.assertIsNone(self.tasks.get_context(self.identity))

    def test_cancel_collecting_and_stale_event_cannot_touch_new_task(self):
        context, _ = self.start_task({"notification_type": "task_assigned"})
        self.assertEqual(self.tasks.cancel(self.identity, context.task_id).status, "cancelled")
        closed = self.tasks.get_context(self.identity)
        self.assertFalse(closed.active)
        self.assertEqual(dict(closed.collected_slots), {})
        new, _ = self.start_task({"channel": "email", "enabled": True})
        self.assertNotEqual(new.task_id, context.task_id)
        self.assertEqual(new.missing_slots, ("notification_type",))
        self.assertEqual(self.tasks.update_slots(self.identity, context.task_id, {"notification_type": "task_assigned"}).error_code, "stale_task")
        self.assertEqual(self.tasks.cancel(self.identity, context.task_id).error_code, "stale_task")
        self.assertEqual(self.tasks.get_context(self.identity), new)

    def test_cancel_pending_closes_workflow_and_cannot_resume(self):
        context, pending = self.start_task()
        self.assertEqual(self.tasks.cancel(self.identity, context.task_id).status, "cancelled")
        self.assertFalse(self.tasks.get_context(self.identity).active)
        self.assertEqual(self.tasks.resume(self.identity, context.task_id).error_code, "task_closed")
        self.assertEqual(self.confirm_task(context, pending).status, "cancelled")
        self.assert_no_writes()

    def test_success_receipt_and_new_task_does_not_inherit_slots(self):
        context, pending = self.start_task()
        result = self.confirm_task(context, pending)
        self.assertEqual(result.status, "succeeded")
        self.assertFalse(self.tasks.get_context(self.identity).active)
        self.assertEqual(self.confirm_task(context, pending).receipt.receipt_id, result.receipt.receipt_id)
        self.assertEqual(self.counts(), (1, 1))
        new, _ = self.start_task({"channel": "email", "enabled": True})
        self.assertEqual(new.missing_slots, ("notification_type",))
        self.assertEqual(self.confirm_task(context, pending).error_code, "stale_task")
        self.assertEqual(self.counts(), (1, 1))

    def test_known_failure_is_terminal_without_failure_receipt(self):
        context, pending = self.start_task()
        with patch.object(self.service.receipts, 'save', side_effect=sqlite3.OperationalError('injected')):
            self.assertEqual(self.confirm_task(context, pending).status, "failed")
        self.assertEqual(self.tasks.get_context(self.identity).task_status, "failed")
        self.assertEqual(self.tasks.resolve(self.identity, context.task_id).status, "failed")
        self.assert_no_writes()
        new, _ = self.start_task({})
        self.assertNotEqual(new.task_id, context.task_id)

    def test_user_and_session_have_independent_tasks(self):
        original, _ = self.start_task()
        for identity in (replace(self.identity, user_id="u2"), replace(self.identity, session_id="s2")):
            self.assertIsNone(self.tasks.get_context(identity))
            self.assertEqual(self.tasks.resume(identity, original.task_id).error_code, "stale_task")
            own, _ = self.start_task(identity=identity)
            self.assertNotEqual(own.task_id, original.task_id)
        self.assertEqual(self.tasks.get_context(self.identity), original)

    def test_workspace_isolation_preserves_single_write_per_user_session(self):
        context, _ = self.start_task({"channel": "email"})
        other = replace(self.identity, workspace_id="w2")
        self.assertIsNone(self.tasks.get_context(other))
        self.assertEqual(self.tasks.resume(other, context.task_id).error_code, "stale_task")
        self.assertEqual(self.tasks.start(other, {}).error_code, "pending_write_exists")
        self.assertEqual(self.tasks.query_preference(other, "task_assigned", "email").status, "answered")
        self.assertEqual(self.tasks.get_context(self.identity), context)
        self.tasks.cancel(self.identity, context.task_id)
        second, _ = self.start_task({}, identity=other)
        self.assertEqual(second.workspace_id, "w2")
        self.assertEqual(dict(second.collected_slots), {})

    def test_invalid_identity_and_unauthorized_partial_task_rejected(self):
        self.assertEqual(self.tasks.start(replace(self.identity, user_id=""), {}).status, "denied")
        unauthorized = replace(self.identity, user_id="u2", workspace_id="w2")
        self.assertEqual(self.tasks.start(unauthorized, {}).status, "denied")
        self.assertIsNone(self.tasks.get_context(unauthorized))

    def test_query_keeps_task_and_resume_does_not_execute(self):
        context, pending = self.start_task()
        result = self.tasks.query_preference(self.identity, "comment_mentioned", "in_app")
        self.assertEqual(result.branch, "Query")
        self.assertEqual(self.tasks.get_context(self.identity), context)
        resumed = self.tasks.resume(self.identity, context.task_id)
        self.assertEqual(resumed.pending_action, pending)
        self.assert_no_writes()
        self.assertEqual(self.confirm_task(context, pending).status, "succeeded")

    def test_failed_query_preserves_pending_task(self):
        context, pending = self.start_task()
        with patch.object(self.database, "connect", side_effect=sqlite3.OperationalError('query failed')):
            self.assertEqual(self.tasks.query_preference(self.identity, "task_assigned", "email").status, "failed")
        self.assertEqual(self.tasks.get_context(self.identity), context)
        self.assertEqual(self.tasks.resume(self.identity, context.task_id).pending_action, pending)

    def test_query_resume_expiry_refreshes_confirmation(self):
        context, pending = self.start_task()
        self.tasks.query_preference(self.identity, "task_assigned", "email")
        self.clock.now += timedelta(minutes=11)
        result = self.tasks.resume(self.identity, context.task_id)
        self.assertEqual(result.error_code, "confirmation_expired")
        self.assertGreater(result.pending_action.revision, pending.revision)
        self.assertEqual(self.confirm_task(context, pending).error_code, "stale_confirmation")
        self.assert_no_writes()

    def test_query_resume_checks_business_and_authorization_versions(self):
        context, pending = self.start_task()
        self.sql("UPDATE memberships SET version=2 WHERE membership_id='m1'")
        result = self.tasks.resume(self.identity, context.task_id)
        self.assertEqual(result.error_code, "business_state_changed")
        self.assertEqual(self.confirm_task(context, pending).error_code, "stale_confirmation")
        self.assert_no_writes()

    def test_query_resume_checks_target_preference(self):
        context, pending = self.start_task()
        other = NotificationUpdateWorkflow(self.service)
        identity = replace(self.identity, session_id="other")
        draft = other.prepare(identity, self.request).pending_action
        other.confirm(identity, draft.pending_action_id, draft.revision, draft.canonical_payload_hash)
        result = self.tasks.resume(self.identity, context.task_id)
        self.assertEqual(result.error_code, "business_state_changed")
        self.assertEqual(result.pending_action.current.version, 1)
        self.assertEqual(self.confirm_task(context, pending).error_code, "stale_confirmation")
        self.assertEqual(self.counts(), (1, 1))

    def test_resume_revoked_permission_closes_unexecuted_task(self):
        context, _ = self.start_task()
        self.sql("UPDATE memberships SET status='inactive' WHERE membership_id='m1'")
        self.assertEqual(self.tasks.resume(self.identity, context.task_id).status, "denied")
        self.assertEqual(self.tasks.get_context(self.identity).task_status, "failed")
        self.assert_no_writes()

    def test_resume_unavailable_database_retains_draft_for_retry(self):
        context, pending = self.start_task()
        with patch.object(self.database, "connect", side_effect=sqlite3.OperationalError('unavailable')):
            self.assertEqual(self.tasks.resume(self.identity, context.task_id).status, "failed")
        self.assertEqual(self.workflow.inspect_pending(self.identity), pending)
        self.assertTrue(self.tasks.get_context(self.identity).active)

    def test_second_write_never_overwrites_collecting_or_pending_task(self):
        for slots in ({}, asdict(self.request)):
            context, pending = self.start_task(slots)
            self.assertEqual(self.tasks.start(self.identity, {}, task_type="create_support_ticket").error_code, "pending_write_exists")
            self.assertEqual(self.tasks.start(self.identity, {}).error_code, "pending_write_exists")
            self.assertEqual(self.tasks.get_context(self.identity), context)
            self.tasks.cancel(self.identity, context.task_id)
        self.assertEqual(self.tasks.start(self.identity, {}, task_type="create_support_ticket").status, "unsupported")

    def test_pending_resolution_retained_until_receipt_can_be_reconciled(self):
        context, pending = self.start_task()
        def lose_ack(connection):
            connection.execute('COMMIT')
            self.sql("UPDATE memberships SET status='inactive', version=2 WHERE membership_id='m1'")
            raise sqlite3.OperationalError('lost acknowledgement')
        with patch.object(self.database, "commit", side_effect=lose_ack):
            self.assertEqual(self.confirm_task(context, pending).status, "pending_resolution")
        self.assertEqual(self.tasks.get_context(self.identity).task_status, "pending_resolution")
        self.assertEqual(self.tasks.cleanup(self.identity, context.task_id).error_code, "active_task_retained")
        self.assertEqual(self.tasks.cancel(self.identity, context.task_id).error_code, "cannot_cancel_execution")
        self.assertEqual(self.tasks.update_slots(self.identity, context.task_id, {"enabled": False}).error_code, "task_not_editable")
        self.assertEqual(self.tasks.start(self.identity, {}).error_code, "pending_write_exists")
        self.assertEqual(self.tasks.resolve(self.identity, context.task_id).status, "denied")
        self.assertTrue(self.tasks.get_context(self.identity).active)
        self.sql("UPDATE memberships SET status='active', version=3 WHERE membership_id='m1'")
        result = self.tasks.resolve(self.identity, context.task_id)
        self.assertEqual(result.status, "succeeded")
        self.assertEqual(result.receipt.operation_id, pending.operation_id)
        self.assertEqual(self.counts(), (1, 1))
        self.assertFalse(self.tasks.get_context(self.identity).active)

    def test_cleanup_terminal_then_new_task_has_no_old_parameters(self):
        context, _ = self.start_task({"notification_type": "task_assigned"})
        self.assertEqual(self.tasks.cleanup(self.identity, context.task_id).error_code, "active_task_retained")
        self.tasks.cancel(self.identity, context.task_id)
        self.assertEqual(self.tasks.cleanup(self.identity, context.task_id).status, "answered")
        self.assertIsNone(self.tasks.get_context(self.identity))
        new, _ = self.start_task({"enabled": True})
        self.assertEqual(new.missing_slots, ("notification_type", "channel"))

    def test_restart_loses_task_and_pending_but_receipt_persists(self):
        context, pending = self.start_task()
        result = self.confirm_task(context, pending)
        restarted = TaskContextManager(NotificationUpdateWorkflow(self.service))
        self.assertIsNone(restarted.get_context(self.identity))
        self.assertEqual(restarted.resume(self.identity, context.task_id).error_code, "stale_task")
        self.assertEqual(self.service.query_receipt(self.identity, pending.operation_id).receipt.receipt_id, result.receipt.receipt_id)

    def test_restart_drops_unconfirmed_task_without_execution(self):
        context, pending = self.start_task()
        restarted = TaskContextManager(NotificationUpdateWorkflow(self.service))
        self.assertIsNone(restarted.get_context(self.identity))
        self.assertEqual(restarted.confirm(self.identity, context.task_id, pending.pending_action_id,
                                          pending.revision, pending.canonical_payload_hash).error_code, "stale_task")
        self.assertEqual(restarted.start(self.identity, {"channel": "email"}).missing_slots,
                         ("notification_type", "enabled"))
        self.assert_no_writes()

    def test_collecting_permission_loss_invalidates_and_can_be_cleaned(self):
        context, _ = self.start_task({"channel": "email"})
        self.sql("UPDATE memberships SET status='inactive' WHERE membership_id='m1'")
        self.assertEqual(self.tasks.resume(self.identity, context.task_id).status, "denied")
        closed = self.tasks.get_context(self.identity)
        self.assertEqual(closed.task_status, "invalidated")
        self.assertEqual(dict(closed.collected_slots), {})
        self.assertEqual(self.tasks.cleanup(replace(self.identity, session_id="other"), context.task_id).error_code, "stale_task")
        self.assertEqual(self.tasks.cleanup(self.identity, "wrong-task").error_code, "stale_task")
        self.assertEqual(self.tasks.cleanup(self.identity, context.task_id).status, "answered")
        self.assert_no_writes()

    def test_store_loss_does_not_overwrite_workflow_pending(self):
        self.start_task()
        fresh_manager = TaskContextManager(self.workflow)
        self.assertEqual(fresh_manager.start(self.identity, {}).error_code, "pending_write_exists")

    def test_lost_workflow_invalidates_task_without_reconstructing_action(self):
        context, _ = self.start_task()
        self.tasks.workflow = NotificationUpdateWorkflow(self.service)
        self.assertEqual(self.tasks.get_context(self.identity).task_status, "invalidated")
        self.assertEqual(self.tasks.resume(self.identity, context.task_id).error_code, "stale_task")
        self.assertEqual(self.tasks.cleanup(self.identity, context.task_id).status, "answered")
        self.assert_no_writes()

    def test_projection_cannot_mutate_authoritative_slots(self):
        context, pending = self.start_task()
        with self.assertRaises(TypeError):
            context.collected_slots["enabled"] = False
        self.assertEqual(self.workflow.inspect_pending(self.identity), pending)

    def test_concurrent_starts_create_only_one_task(self):
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: self.tasks.start(self.identity, {}), range(2)))
        self.assertEqual(sum(r.error_code == "missing_slots" for r in results), 1)
        self.assertEqual(sum(r.error_code == "pending_write_exists" for r in results), 1)
