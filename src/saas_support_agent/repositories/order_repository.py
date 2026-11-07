"""查询只在当前 Workspace 内的显式店铺授权中匹配订单。"""

from saas_support_agent.core.models import RuleViolation


def require_order(connection, identity, order_id, shop_id=None):
    rows = connection.execute(
        """SELECT o.order_id, o.shop_id, o.status, o.delivered_on,
                  o.version AS order_version, s.version AS shop_version, a.version AS access_version
           FROM orders o
           JOIN shops s USING (workspace_id, shop_id)
           JOIN shop_access a USING (workspace_id, shop_id)
           WHERE o.workspace_id = ? AND a.user_id = ? AND o.order_id = ?
             AND (? IS NULL OR o.shop_id = ?) AND s.status = 'active'
           ORDER BY o.shop_id LIMIT 2""",
        (identity.workspace_id, identity.user_id, order_id, shop_id, shop_id),
    ).fetchall()
    if not rows:
        raise RuleViolation("denied", "scope_or_permission_denied", "订单不存在或当前账号无权访问。")
    if len(rows) > 1:
        raise RuleViolation("needs_clarification", "ambiguous_order", "同一订单编号匹配多个可访问店铺，请提供店铺编号。",
                            ("shop_id",))
    return rows[0]
