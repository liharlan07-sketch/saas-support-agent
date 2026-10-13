"""Repositories share their caller's transaction and never commit themselves."""

from dataclasses import asdict
from datetime import datetime
import json
import sqlite3
from uuid import uuid4

from saas_support_agent.core.models import (
    AuthorizationSnapshot, NotificationPreference, OperationReceipt,
    RuleViolation, TrustedIdentity,
)


class MembershipRepository:
    @staticmethod
    def require_active(connection: sqlite3.Connection, identity: TrustedIdentity) -> AuthorizationSnapshot:
        row = connection.execute(
            """SELECT w.version AS workspace_version, m.version AS membership_version
               FROM memberships m
               JOIN users u ON u.user_id = m.user_id
               JOIN workspaces w ON w.workspace_id = m.workspace_id
               WHERE m.workspace_id = ? AND m.user_id = ?
                 AND u.status = 'active' AND w.status = 'active' AND m.status = 'active'""",
            (identity.workspace_id, identity.user_id),
        ).fetchone()
        if row is None:
            raise RuleViolation("denied", "scope_or_permission_denied", "无权访问此工作空间或对象。")
        return AuthorizationSnapshot(row["workspace_version"], row["membership_version"])


class NotificationPreferenceRepository:
    @staticmethod
    def get(connection: sqlite3.Connection, identity: TrustedIdentity,
            notification_type: str, channel: str) -> NotificationPreference:
        row = connection.execute(
            """SELECT enabled, version FROM notification_preferences
               WHERE workspace_id = ? AND user_id = ? AND notification_type = ? AND channel = ?""",
            (identity.workspace_id, identity.user_id, notification_type, channel),
        ).fetchone()
        return NotificationPreference(
            identity.workspace_id, identity.user_id, notification_type, channel,
            bool(row["enabled"]) if row else False, row["version"] if row else 0,
        )

    @staticmethod
    def update(connection: sqlite3.Connection, current: NotificationPreference,
               enabled: bool) -> NotificationPreference:
        if current.enabled == enabled:
            return current  # A no-op still receives its own operation receipt.
        if current.version == 0:
            connection.execute(
                """INSERT INTO notification_preferences
                   (preference_id, workspace_id, user_id, notification_type, channel, enabled, version)
                   VALUES (?, ?, ?, ?, ?, ?, 1)""",
                (str(uuid4()), current.workspace_id, current.user_id,
                 current.notification_type, current.channel, int(enabled)),
            )
        else:
            cursor = connection.execute(
                """UPDATE notification_preferences SET enabled = ?, version = version + 1
                   WHERE workspace_id = ? AND user_id = ? AND notification_type = ?
                     AND channel = ? AND version = ?""",
                (int(enabled), current.workspace_id, current.user_id,
                 current.notification_type, current.channel, current.version),
            )
            if cursor.rowcount != 1:
                raise RuleViolation("needs_confirmation", "business_state_changed", "设置已变化，请重新确认。")
        return NotificationPreference(
            current.workspace_id, current.user_id, current.notification_type,
            current.channel, enabled, current.version + 1,
        )


class OperationReceiptRepository:
    @staticmethod
    def get(connection: sqlite3.Connection, identity: TrustedIdentity,
            operation_id: str) -> OperationReceipt | None:
        row = connection.execute(
            """SELECT * FROM operation_receipts
               WHERE workspace_id = ? AND user_id = ? AND operation_id = ?""",
            (identity.workspace_id, identity.user_id, operation_id),
        ).fetchone()
        if row is None:
            return None
        values = dict(row)
        values["result_snapshot"] = json.loads(values["result_snapshot"])
        values["created_at"] = datetime.fromisoformat(values["created_at"])
        return OperationReceipt(**values)

    @staticmethod
    def save(connection: sqlite3.Connection, receipt: OperationReceipt) -> None:
        values = asdict(receipt)
        values["result_snapshot"] = json.dumps(values["result_snapshot"], sort_keys=True)
        values["created_at"] = receipt.created_at.isoformat()
        connection.execute(
            """INSERT INTO operation_receipts
               (receipt_id, workspace_id, user_id, operation_id, pending_action_id,
                action_revision, action_type, payload_hash, status, result_snapshot, error_code, created_at)
               VALUES (:receipt_id, :workspace_id, :user_id, :operation_id, :pending_action_id,
                :action_revision, :action_type, :payload_hash, :status, :result_snapshot, :error_code, :created_at)""",
            values,
        )
