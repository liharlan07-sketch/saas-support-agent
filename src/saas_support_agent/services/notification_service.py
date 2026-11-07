"""Notification authorization, transactional execution and receipt reconciliation."""

from dataclasses import asdict, replace
from datetime import datetime, timezone
import logging
import sqlite3
from uuid import uuid4

from saas_support_agent.core.models import (
    ACTION_TYPE, CHANNELS, NOTIFICATION_TYPES, AuthorizationSnapshot, Confirmation, NotificationUpdateRequest,
    OperationReceipt, OperationResult, PendingAction, RuleViolation, TrustedIdentity,
    pending_hash,
)
from saas_support_agent.repositories.database import Database
from saas_support_agent.repositories.notification_repositories import (
    MembershipRepository, NotificationPreferenceRepository, OperationReceiptRepository,
)


logger = logging.getLogger(__name__)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class NotificationService:
    action_type = ACTION_TYPE

    def __init__(self, database: Database, *, clock=utc_now):
        self.database = database
        self.clock = clock
        self.memberships = MembershipRepository()
        self.preferences = NotificationPreferenceRepository()
        self.receipts = OperationReceiptRepository()

    @staticmethod
    def validate_identity(identity: TrustedIdentity) -> None:
        # This validates shape only. Authentication belongs to the trusted caller.
        if any(type(value) is not str or not value or value != value.strip()
               for value in asdict(identity).values()):
            raise RuleViolation("denied", "invalid_identity", "身份上下文无效。")

    @staticmethod
    def validate_request(request: NotificationUpdateRequest) -> None:
        missing = tuple(name for name, value in asdict(request).items() if value is None)
        if missing:
            raise RuleViolation("needs_clarification", "missing_slots", "请补齐通知类型、渠道和目标开关。", missing)
        if (type(request.notification_type) is not str or request.notification_type not in NOTIFICATION_TYPES
                or type(request.channel) is not str or request.channel not in CHANNELS
                or type(request.enabled) is not bool):
            raise RuleViolation("needs_clarification", "invalid_parameters", "通知类型、渠道或布尔值不合法。")

    def authorize(self, identity: TrustedIdentity) -> AuthorizationSnapshot:
        """参数尚未收齐时也检查成员资格，Memory 不能替代 authorization。"""
        self.validate_identity(identity)
        connection = self.database.connect()
        try:
            return self.memberships.require_active(connection, identity)
        finally:
            connection.close()

    def load_current(self, identity: TrustedIdentity, request: NotificationUpdateRequest):
        self.validate_identity(identity)
        self.validate_request(request)
        connection = self.database.connect()
        try:
            connection.execute("BEGIN")
            authorization = self.memberships.require_active(connection, identity)
            current = self._current(connection, identity, request)
            return authorization, current
        finally:
            connection.close()  # Ends this read-only snapshot.

    def _current(self, connection, identity, request):
        return self.preferences.get(connection, identity, request.notification_type, request.channel)

    def _apply(self, connection, identity, pending, current):
        updated = self.preferences.update(connection, current, pending.payload.enabled)
        return {
            **asdict(updated), "old_enabled": current.enabled,
            "previous_version": current.version, "changed": current != updated,
        }

    def query_preference(self, identity: TrustedIdentity, notification_type: str, channel: str) -> OperationResult:
        try:
            _, current = self.load_current(identity, NotificationUpdateRequest(notification_type, channel, False))
            return OperationResult(identity.request_id, "answered", "已查询当前通知设置。",
                                   branch="Query", data=asdict(current))
        except RuleViolation as error:
            return replace(error.result(identity), branch="Query")
        except sqlite3.Error:
            return OperationResult(identity.request_id, "failed", "暂时无法查询设置。",
                                   branch="Query", error_code="database_unavailable")

    def _authorize_receipt(self, connection, identity, receipt):
        if receipt is None or receipt.action_type != self.action_type:
            raise RuleViolation("denied", "scope_or_permission_denied", "无权访问此工作空间或对象。")

    def query_receipt(self, identity: TrustedIdentity, operation_id: str) -> OperationResult:
        connection = None
        try:
            self.validate_identity(identity)
            connection = self.database.connect()
            connection.execute("BEGIN")
            self.memberships.require_active(connection, identity)
            receipt = self.receipts.get(connection, identity, operation_id)
            self._authorize_receipt(connection, identity, receipt)
            return OperationResult(identity.request_id, "answered", "已查询操作回执。", branch="Query",
                                   data=dict(receipt.result_snapshot), receipt=receipt)
        except RuleViolation as error:
            return replace(error.result(identity), branch="Query")
        except sqlite3.Error:
            return OperationResult(identity.request_id, "failed", "暂时无法查询回执。",
                                   branch="Query", error_code="database_unavailable")
        finally:
            if connection is not None:
                connection.close()

    def _check_binding(self, identity: TrustedIdentity, pending: PendingAction, confirmation: Confirmation) -> None:
        if (pending.owner_user_id, pending.workspace_id, pending.session_id) != (
                identity.user_id, identity.workspace_id, identity.session_id):
            raise RuleViolation("denied", "scope_or_permission_denied", "无权执行此操作。")
        if (pending.action_type != self.action_type or pending.canonical_payload_hash != pending_hash(pending)
                or pending.workflow_state != "confirmed"):
            raise RuleViolation("needs_confirmation", "invalid_draft", "待确认内容不一致。")
        if (confirmation.pending_action_id, confirmation.revision, confirmation.operation_id,
                confirmation.payload_hash, confirmation.owner_user_id, confirmation.workspace_id,
                confirmation.session_id, confirmation.expires_at) != (
                pending.pending_action_id, pending.revision, pending.operation_id,
                pending.canonical_payload_hash, identity.user_id, identity.workspace_id,
                identity.session_id, pending.expires_at):
            raise RuleViolation("needs_confirmation", "stale_confirmation", "确认与当前操作版本不一致。")

    @staticmethod
    def _receipt_result(identity: TrustedIdentity, pending: PendingAction, receipt: OperationReceipt) -> OperationResult:
        if (receipt.payload_hash, receipt.pending_action_id, receipt.action_revision, receipt.action_type) != (
                pending.canonical_payload_hash, pending.pending_action_id, pending.revision, pending.action_type):
            raise RuleViolation("failed", "operation_identity_conflict", "操作编号与内容冲突，未执行写入。")
        return OperationResult(
            identity.request_id, receipt.status,
            "操作已完成，可核对回执。" if receipt.status == "succeeded" else "该次操作已失败，请重新发起并确认。",
            data=dict(receipt.result_snapshot), receipt=receipt, error_code=receipt.error_code,
        )

    def _make_receipt(self, identity, pending, status, snapshot, error_code=None):
        return OperationReceipt(
            str(uuid4()), identity.workspace_id, identity.user_id, pending.operation_id,
            pending.pending_action_id, pending.revision, pending.action_type,
            pending.canonical_payload_hash, status, snapshot, error_code, self.clock(),
        )

    def execute(self, identity: TrustedIdentity, pending: PendingAction, confirmation: Confirmation) -> OperationResult:
        """Internal entry: workflow supplies its stored draft and bound confirmation.

        No external API accepts arbitrary PendingAction/Confirmation objects.
        Receipt lookup precedes expiry/version checks, so a completed operation
        can be replayed even after its original version or confirmation expired.
        """
        connection = None
        commit_attempted = False
        execution_started = False
        try:
            self.validate_identity(identity)
            self.validate_request(pending.payload)
            self._check_binding(identity, pending, confirmation)
            connection = self.database.connect()
            connection.execute("BEGIN IMMEDIATE")
            authorization = self.memberships.require_active(connection, identity)
            receipt = self.receipts.get(connection, identity, pending.operation_id)
            if receipt:
                result = self._receipt_result(identity, pending, receipt)
                self._authorize_receipt(connection, identity, receipt)
                return result
            now = self.clock()
            if now >= pending.expires_at or confirmation.confirmed_at > now:
                raise RuleViolation("needs_confirmation", "confirmation_expired", "确认已过期，请重新查看并确认。")
            current = self._current(connection, identity, pending.payload)
            if authorization != pending.expected_versions or current != pending.current:
                raise RuleViolation("needs_confirmation", "business_state_changed", "当前状态已变化，请重新查看并确认。")

            execution_started = True
            snapshot = self._apply(connection, identity, pending, current)
            receipt = self._make_receipt(identity, pending, "succeeded", snapshot)
            self.receipts.save(connection, receipt)
            commit_attempted = True
            self.database.commit(connection)
            logger.info("action_execution request_id=%s operation_id=%s status=succeeded",
                        identity.request_id, pending.operation_id)
            return self._receipt_result(identity, pending, receipt)
        except RuleViolation as error:
            return error.result(identity)
        except Exception:
            # Failure after COMMIT may only mean that acknowledgement was lost.
            rollback_known = False
            if connection is not None and connection.in_transaction:
                try:
                    connection.execute("ROLLBACK")
                    rollback_known = True
                except sqlite3.Error:
                    pass
            if connection is not None:
                connection.close()
                connection = None
            logger.warning("action_execution request_id=%s operation_id=%s stage=exception",
                           identity.request_id, pending.operation_id)
            if (commit_attempted and not rollback_known) or (execution_started and not rollback_known):
                resolved = self.reconcile(identity, pending)
                if resolved.status == "denied":
                    # Authorization lost during recovery says nothing about the commit.
                    return OperationResult(
                        identity.request_id, "pending_resolution", "暂时无法授权核对执行结果，请保留原操作编号。",
                        data={"operation_id": pending.operation_id}, error_code="reconciliation_not_authorized",
                    )
                return resolved
            # Known not executed/rolled back: never report success or blindly retry.
            failed = OperationResult(identity.request_id, "failed", "操作未提交，请重新发起并确认。",
                                     error_code="execution_rolled_back" if rollback_known else "execution_not_started")
            if execution_started and rollback_known:
                return self._record_known_failure(identity, pending, failed)
            return failed
        finally:
            if connection is not None:
                connection.close()  # Rolls back all paths that did not commit.

    def _record_known_failure(self, identity, pending, failed):
        """Best effort only after a confirmed rollback. Success was never committed."""
        connection = None
        try:
            connection = self.database.connect()
            connection.execute("BEGIN IMMEDIATE")
            self.memberships.require_active(connection, identity)
            existing = self.receipts.get(connection, identity, pending.operation_id)
            if existing:
                result = self._receipt_result(identity, pending, existing)
                self._authorize_receipt(connection, identity, existing)
                return result
            receipt = self._make_receipt(identity, pending, "failed", {}, failed.error_code)
            self.receipts.save(connection, receipt)
            self.database.commit(connection)
            return self._receipt_result(identity, pending, receipt)
        except Exception:
            return failed  # Failure receipt is optional when persistence is unavailable.
        finally:
            if connection is not None:
                connection.close()

    def reconcile(self, identity: TrustedIdentity, pending: PendingAction) -> OperationResult:
        """Read only. An absent receipt never starts another execution."""
        connection = None
        try:
            self.validate_identity(identity)
            if (identity.user_id, identity.workspace_id, identity.session_id) != (
                    pending.owner_user_id, pending.workspace_id, pending.session_id):
                raise RuleViolation("denied", "scope_or_permission_denied", "无权核对此操作。")
            connection = self.database.connect()
            connection.execute("BEGIN")
            self.memberships.require_active(connection, identity)
            receipt = self.receipts.get(connection, identity, pending.operation_id)
            if receipt:
                result = self._receipt_result(identity, pending, receipt)
                self._authorize_receipt(connection, identity, receipt)
                return result
        except RuleViolation as error:
            return error.result(identity)
        except Exception:
            pass
        finally:
            if connection is not None:
                connection.close()
        if pending.workflow_state == "failed":
            # The coordinator already observed a known non-execution/rollback.
            # Lack of a failure receipt must not convert that fact into uncertainty.
            return OperationResult(identity.request_id, "failed", "该次操作已失败，请重新发起并确认。",
                                   error_code="known_failure_without_receipt")
        return OperationResult(
            identity.request_id, "pending_resolution", "执行结果尚待核对，请保留操作编号，勿重复办理。",
            data={"operation_id": pending.operation_id}, error_code="execution_outcome_unknown",
        )
