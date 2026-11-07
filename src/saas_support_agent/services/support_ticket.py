"""工单复用受控写入事务，但有独立的参数、项目授权与持久化规则。"""

from dataclasses import asdict, replace
import json
import sqlite3
from uuid import uuid4

from saas_support_agent.core.models import (
    OperationResult, RuleViolation, SupportTicketRequest, TICKET_ACTION_TYPE,
)
from saas_support_agent.repositories.order_repository import require_order
from saas_support_agent.services.notification_service import NotificationService


ISSUE_TYPES = {"access", "notification", "usage", "other"}
TICKET_FIELDS = set(SupportTicketRequest.__dataclass_fields__)


def validate_ticket_patch(slots):
    if type(slots) is not dict or set(slots) - TICKET_FIELDS:
        raise RuleViolation("needs_clarification", "invalid_parameters", "工单含不支持的字段。")
    for key, value in slots.items():
        if value is None:
            continue
        if (type(value) is not str or not value.strip() or len(value) > 2000 or
                any(not c.isprintable() for c in value)):
            raise RuleViolation("needs_clarification", "invalid_parameters", "请提供非空、单行的工单信息（最多 2000 字符）。")
        if key == "issue_type" and value not in ISSUE_TYPES:
            raise RuleViolation("needs_clarification", "invalid_parameters", "请选择 access、notification、usage 或 other。")


def require_project(connection, identity, project_id):
    row = connection.execute(
        """SELECT p.project_id, p.name, p.version AS project_version,
                  a.access_level, a.version AS access_version
           FROM projects p JOIN project_access a USING (workspace_id, project_id)
           WHERE p.workspace_id = ? AND p.project_id = ? AND a.user_id = ? AND p.status = 'active'""",
        (identity.workspace_id, project_id, identity.user_id),
    ).fetchone()
    if row is None:
        raise RuleViolation("denied", "scope_or_permission_denied", "无权访问此工作空间或对象。")
    return row


class SupportTicketService(NotificationService):
    action_type = TICKET_ACTION_TYPE
    required_slots = ("issue_type", "description")

    @classmethod
    def validate_request(cls, request: SupportTicketRequest):
        slots = asdict(request)
        validate_ticket_patch(slots)
        missing = tuple(key for key in cls.required_slots if slots[key] is None)
        if "order_id" in cls.required_slots or request.shop_id is not None or request.order_id is not None:
            missing += tuple(key for key in ("shop_id", "order_id") if slots[key] is None and key not in missing)
            if request.project_id is not None:
                raise RuleViolation("needs_clarification", "invalid_parameters", "售后工单不能同时关联项目。")
        if missing:
            raise RuleViolation("needs_clarification", "missing_slots", "请补齐工单所需信息。", missing)

    def _current(self, connection, identity, request):
        if request.order_id is not None:
            order = require_order(connection, identity, request.order_id, request.shop_id)
            return order["shop_version"], order["access_version"], order["order_version"]
        if request.project_id is None:
            return None
        row = require_project(connection, identity, request.project_id)
        return row["project_version"], row["access_version"]

    def _apply(self, connection, identity, pending, current):
        request = pending.payload
        ticket = {
            "ticket_id": f"T-{uuid4().hex[:16]}", "workspace_id": identity.workspace_id,
            "requester_user_id": identity.user_id, "issue_type": request.issue_type,
            "summary": request.description, "project_id": request.project_id,
            "context_snapshot": {k: v for k, v in asdict(request).items()
                                 if k in {"steps", "observed_error"} and v is not None},
            "status": "waiting_for_agent", "created_at": self.clock().isoformat(), "version": 1,
        }
        connection.execute(
            """INSERT INTO support_tickets
            (ticket_id, workspace_id, requester_user_id, issue_type, summary, context_snapshot,
             project_id, status, created_at, version)
            VALUES (:ticket_id, :workspace_id, :requester_user_id, :issue_type, :summary, :context_snapshot,
                    :project_id, :status, :created_at, :version)""",
            {**ticket, "context_snapshot": json.dumps(ticket["context_snapshot"], ensure_ascii=False)},
        )
        if request.order_id is not None:
            connection.execute(
                "INSERT INTO ticket_orders (ticket_id, workspace_id, shop_id, order_id) VALUES (?, ?, ?, ?)",
                (ticket["ticket_id"], identity.workspace_id, request.shop_id, request.order_id),
            )
            ticket.update(kind="after_sales", shop_id=request.shop_id, order_id=request.order_id)
        return {"ticket": ticket}

    def _authorize_receipt(self, connection, identity, receipt):
        super()._authorize_receipt(connection, identity, receipt)
        ticket = receipt.result_snapshot.get("ticket", {})
        if ticket.get("kind") == "after_sales":
            require_order(connection, identity, ticket["order_id"], ticket["shop_id"])

    def query_ticket(self, identity, ticket_id):
        connection = None
        try:
            self.validate_identity(identity)
            connection = self.database.connect()
            connection.execute("BEGIN")
            self.memberships.require_active(connection, identity)
            row = connection.execute(
                """SELECT * FROM support_tickets
                   WHERE workspace_id = ? AND requester_user_id = ? AND ticket_id = ?""",
                (identity.workspace_id, identity.user_id, ticket_id),
            ).fetchone()
            if row is None:
                raise RuleViolation("denied", "scope_or_permission_denied", "无权访问此工作空间或对象。")
            ticket = dict(row)
            target = connection.execute(
                "SELECT shop_id, order_id FROM ticket_orders WHERE workspace_id = ? AND ticket_id = ?",
                (identity.workspace_id, ticket_id),
            ).fetchone()
            if target:
                require_order(connection, identity, target["order_id"], target["shop_id"])
                ticket.update(kind="after_sales", shop_id=target["shop_id"], order_id=target["order_id"])
            ticket["context_snapshot"] = json.loads(ticket["context_snapshot"])
            return OperationResult(identity.request_id, "answered", "已查询本人工单。", "Query", {"ticket": ticket})
        except RuleViolation as error:
            return replace(error.result(identity), branch="Query")
        except sqlite3.Error:
            return OperationResult(identity.request_id, "failed", "暂时无法查询工单。", "Query",
                                   error_code="database_unavailable")
        finally:
            if connection is not None:
                connection.close()


class MerchantTicketService(SupportTicketService):
    """售后入口强制绑定订单；原有通用/项目工单仍可单独使用。"""

    required_slots = (*SupportTicketService.required_slots, "shop_id", "order_id")
