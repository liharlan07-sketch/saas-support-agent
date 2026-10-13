"""Transaction/fault tests inspect persistent records, not success wording."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import sqlite3
from unittest.mock import patch

from saas_support_agent.core.models import Confirmation, pending_hash
from saas_support_agent.repositories.database import Database
from test_notification_workflow import DatabaseCase


class SQLiteTransactionTests(DatabaseCase):
    def bound(self, pending):
        confirmed = replace(pending, workflow_state="confirmed")
        confirmation = Confirmation(
            pending.pending_action_id, pending.revision, pending.operation_id, pending.canonical_payload_hash,
            pending.owner_user_id, pending.workspace_id, pending.session_id, self.clock(), pending.expires_at,
        )
        return confirmed, confirmation

    def test_receipt_failure_rolls_back_preference(self):
        pending = self.prepare()
        with patch.object(self.service.receipts, "save", side_effect=sqlite3.OperationalError("receipt failure")):
            result = self.confirm(pending)
        self.assertEqual(result.status, "failed")
        self.assertIsNone(result.receipt)
        self.assert_no_writes()

    def test_known_rollback_can_persist_failure_receipt(self):
        pending = self.prepare()
        save = self.service.receipts.save

        def fail_success(connection, receipt):
            if receipt.status == "succeeded":
                raise sqlite3.OperationalError("failure before receipt")
            save(connection, receipt)

        with patch.object(self.service.receipts, "save", side_effect=fail_success):
            result = self.confirm(pending)
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.receipt.status, "failed")
        self.assertEqual(self.counts(), (0, 1))
        self.assertEqual(self.confirm(pending).receipt.receipt_id, result.receipt.receipt_id)

    def test_failed_existing_row_update_restores_previous_value_and_version(self):
        self.confirm(self.prepare())
        pending = self.prepare(replace(self.request, enabled=False))
        with patch.object(self.service.receipts, "save", side_effect=sqlite3.OperationalError("receipt failure")):
            self.assertEqual(self.confirm(pending).status, "failed")
        row = self.sql("SELECT enabled, version FROM notification_preferences")[0]
        self.assertEqual((row["enabled"], row["version"]), (1, 1))
        self.assertEqual(self.counts(), (1, 1))

    def test_exception_before_commit_rolls_back_both_records(self):
        pending = self.prepare()
        with patch.object(self.database, "commit", side_effect=sqlite3.OperationalError("commit refused")):
            result = self.confirm(pending)
        self.assertEqual(result.status, "failed")
        self.assert_no_writes()

    def test_commit_then_lost_ack_is_reconciled_to_success(self):
        pending = self.prepare()

        def lose_ack(connection):
            Database.commit(connection)
            raise sqlite3.OperationalError("lost acknowledgement")

        with patch.object(self.database, "commit", side_effect=lose_ack) as commit:
            result = self.confirm(pending)
            self.assertEqual(commit.call_count, 1)
        self.assertEqual(result.status, "succeeded")
        self.assertEqual(self.counts(), (1, 1))
        self.assertEqual(self.confirm(pending).receipt.receipt_id, result.receipt.receipt_id)

    def test_unknown_result_blocks_cancel_revision_and_new_write_until_reconciled(self):
        pending = self.prepare()
        connection = self.database.connect()

        def lose_ack(conn):
            Database.commit(conn)
            raise sqlite3.OperationalError("lost acknowledgement")

        with patch.object(self.database, "connect", side_effect=[connection, sqlite3.OperationalError("offline")]), \
                patch.object(self.database, "commit", side_effect=lose_ack):
            result = self.confirm(pending)
        self.assertEqual(result.status, "pending_resolution")
        self.assertEqual(self.workflow.cancel(self.identity, pending.pending_action_id).error_code, "cannot_cancel_execution")
        self.assertEqual(self.workflow.revise(self.identity, self.request).error_code, "action_not_editable")
        self.assertEqual(self.workflow.prepare(self.identity, self.request).error_code, "pending_write_exists")
        recovered = self.workflow.resolve(self.identity)
        self.assertEqual(recovered.status, "succeeded")
        self.assertEqual(recovered.receipt.operation_id, pending.operation_id)
        self.assertEqual(self.counts(), (1, 1))

    def test_reconciliation_without_receipt_never_executes(self):
        pending = self.prepare()
        self.assertEqual(self.service.reconcile(self.identity, pending).status, "pending_resolution")
        self.assert_no_writes()

    def test_revoked_authorization_during_commit_recovery_does_not_claim_failure(self):
        pending = self.prepare()

        def lose_ack_and_access(connection):
            Database.commit(connection)
            self.sql("UPDATE memberships SET status = 'inactive', version = 2 WHERE membership_id = 'm1'")
            raise sqlite3.OperationalError("lost acknowledgement")

        with patch.object(self.database, "commit", side_effect=lose_ack_and_access):
            result = self.confirm(pending)
        self.assertEqual(result.status, "pending_resolution")
        self.assertEqual(result.error_code, "reconciliation_not_authorized")
        self.assertEqual(self.workflow.resolve(self.identity).status, "denied")
        self.assertEqual(self.workflow.prepare(self.identity, self.request).error_code, "pending_write_exists")
        self.sql("UPDATE memberships SET status = 'active', version = 3 WHERE membership_id = 'm1'")
        self.assertEqual(self.workflow.resolve(self.identity).status, "succeeded")
        self.assertEqual(self.counts(), (1, 1))

    def test_two_connections_same_operation_commit_once(self):
        pending, confirmation = self.bound(self.prepare())
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(self.service.execute, self.identity, pending, confirmation) for _ in range(2)]
            results = [future.result(timeout=5) for future in futures]
        self.assertEqual([r.status for r in results], ["succeeded", "succeeded"])
        self.assertEqual(results[0].receipt.receipt_id, results[1].receipt.receipt_id)
        self.assertEqual(self.counts(), (1, 1))
        self.assertEqual(self.sql("SELECT version FROM notification_preferences")[0][0], 1)

    def test_two_distinct_operations_cannot_overwrite_same_prepared_version(self):
        first, first_confirmation = self.bound(self.prepare())
        other_identity = replace(self.identity, session_id="s2")
        second, second_confirmation = self.bound(self.prepare(identity=other_identity))
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(self.service.execute, self.identity, first, first_confirmation),
                       executor.submit(self.service.execute, other_identity, second, second_confirmation)]
            results = [future.result(timeout=5) for future in futures]
        self.assertEqual(sorted(r.status for r in results), ["needs_confirmation", "succeeded"])
        self.assertEqual(self.counts(), (1, 1))

    def test_busy_database_does_not_report_success_or_retry_writes(self):
        self.database.timeout = 0.01
        pending = self.prepare()
        blocker = self.database.connect()
        try:
            blocker.execute("BEGIN IMMEDIATE")
            result = self.confirm(pending)
            self.assertEqual(result.status, "failed")
            self.assertEqual(result.error_code, "execution_not_started")
        finally:
            blocker.close()
        self.assert_no_writes()

    def test_tampered_draft_is_rejected_before_any_write(self):
        pending, confirmation = self.bound(self.prepare())
        tampered = replace(pending, payload=replace(pending.payload, channel="in_app"))
        result = self.service.execute(self.identity, tampered, confirmation)
        self.assertEqual(result.error_code, "invalid_draft")
        self.assert_no_writes()

    def test_same_operation_id_with_different_hash_is_rejected(self):
        pending, confirmation = self.bound(self.prepare())
        self.assertEqual(self.service.execute(self.identity, pending, confirmation).status, "succeeded")
        altered = replace(pending, payload=replace(pending.payload, enabled=False))
        altered = replace(altered, canonical_payload_hash=pending_hash(altered))
        confirmation = replace(confirmation, payload_hash=altered.canonical_payload_hash)
        result = self.service.execute(self.identity, altered, confirmation)
        self.assertEqual(result.error_code, "operation_identity_conflict")
        self.assertEqual(self.counts(), (1, 1))
        self.assertEqual(self.sql("SELECT enabled FROM notification_preferences")[0][0], 1)

    def test_sql_constraints_reject_duplicate_preference_and_orphan_scope(self):
        self.confirm(self.prepare())
        with self.assertRaises(sqlite3.IntegrityError):
            self.sql("""INSERT INTO notification_preferences
                        SELECT 'duplicate', workspace_id, user_id, notification_type, channel, enabled, version
                        FROM notification_preferences""")
        with self.assertRaises(sqlite3.IntegrityError):
            self.sql("INSERT INTO notification_preferences VALUES ('bad', 'w2', 'u2', 'task_assigned', 'email', 1, 1)")
        self.assertEqual(self.counts(), (1, 1))
