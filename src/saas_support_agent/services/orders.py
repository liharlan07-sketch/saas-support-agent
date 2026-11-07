"""只读业务入口：实时查询本地 SQLite，不从对话生成订单状态。"""

from dataclasses import replace
import sqlite3

from saas_support_agent.core.models import OperationResult, RuleViolation
from saas_support_agent.repositories.order_repository import require_order


def query_order(service, identity, order_id, shop_id=None):
    connection = None
    try:
        service.validate_identity(identity)
        connection = service.database.connect()
        connection.execute("BEGIN")
        service.memberships.require_active(connection, identity)
        order = require_order(connection, identity, order_id, shop_id)
        data = {key: order[key] for key in ("order_id", "shop_id", "status", "delivered_on")}
        return OperationResult(identity.request_id, "answered", "已查询订单实时状态。", "Query", {"order": data})
    except RuleViolation as error:
        return replace(error.result(identity), branch="Query")
    except sqlite3.Error:
        return OperationResult(identity.request_id, "failed", "暂时无法查询订单。", "Query",
                               error_code="database_unavailable")
    finally:
        if connection is not None:
            connection.close()
