"""单进程商家客服入口。查询读数据库，写操作只推进已展示的版本。"""

from dataclasses import dataclass, replace
import re
from threading import RLock

from saas_support_agent.core.models import OperationResult, RuleViolation
from saas_support_agent.merchant_input import recognize
from saas_support_agent.services.notification_service import NotificationService
from saas_support_agent.services.orders import query_order
from saas_support_agent.services.support_ticket import MerchantTicketService
from saas_support_agent.workflows.notification_update import NotificationUpdateWorkflow
from saas_support_agent.workflows.support_ticket import SupportTicketTasks, SupportTicketWorkflow
from saas_support_agent.workflows.task_context import TaskContextManager


POLICIES = {
    "late_return": {
        "source_id": "demo-return-policy:1.0:late-return",
        "title": "示例商家售后政策", "version": "1.0", "section": "超期退货",
        "text": "超过示例退货期限的申请可创建售后工单，由人工核对订单及特殊情形；提交工单不代表批准退货或退款。",
    },
    "return_window": {
        "source_id": "demo-return-policy:1.0:return-window",
        "title": "示例商家售后政策", "version": "1.0", "section": "退货期限",
        "text": "在此合成演示中，普通退货申请期限为签收后 7 天。此条款不代表任何真实平台政策或法定规则。",
    },
}


@dataclass(frozen=True)
class DisplayedAction:
    tasks: TaskContextManager
    task_id: str
    pending_action_id: str
    revision: int
    payload_hash: str
    operation_id: str


class MerchantSupport:
    """复用一组 Workflow 实例；可信身份由本地入口提供，文本不赋予权限。"""

    def __init__(self, database, *, knowledge=None, understanding=None):
        self.knowledge = knowledge
        self.understanding = understanding
        self.notifications = TaskContextManager(NotificationUpdateWorkflow(NotificationService(database)))
        self.tickets = SupportTicketTasks(SupportTicketWorkflow(MerchantTicketService(database)))
        self._tasks = (self.notifications, self.tickets)
        self._displayed = {}
        self._lock = RLock()

    @staticmethod
    def _key(identity):
        return identity.user_id, identity.workspace_id, identity.session_id

    def _active(self, identity):
        for tasks in self._tasks:
            context = tasks.get_context(identity)
            if context and context.active:
                return tasks, context
        return None, None

    def _show(self, identity, tasks, result):
        pending = result.pending_action
        if pending is not None:
            context = tasks.get_context(identity)
            self._displayed[self._key(identity)] = DisplayedAction(
                tasks, context.task_id, pending.pending_action_id, pending.revision,
                pending.canonical_payload_hash, pending.operation_id,
            )
        return result

    def _control(self, identity, text):
        key = self._key(identity)
        match = re.fullmatch(r"/确认\s+([a-f0-9-]{36})", text)
        if match:
            shown = self._displayed.get(key)
            if not shown or match[1] != shown.operation_id:
                return OperationResult(identity.request_id, "needs_confirmation",
                                       "确认编号与当前展示不一致。请 /恢复 后核对编号。",
                                       error_code="stale_confirmation")
            result = shown.tasks.confirm(identity, shown.task_id, shown.pending_action_id,
                                         shown.revision, shown.payload_hash)
            return self._show(identity, shown.tasks, result)
        if text not in {"/恢复", "/取消", "/核对"}:
            return None
        tasks, context = self._active(identity)
        if not context:
            return OperationResult(identity.request_id, "needs_clarification", "当前没有待处理任务。",
                                   error_code="no_active_task")
        if text == "/取消":
            result = tasks.cancel(identity, context.task_id)
            if result.status == "cancelled":
                self._displayed.pop(key, None)
            return result
        if text == "/核对":
            if context.task_status != "pending_resolution":
                return OperationResult(identity.request_id, "needs_clarification", "只有结果待核对的操作可使用 /核对。")
            return tasks.resolve(identity, context.task_id)
        self._displayed.pop(key, None)
        return self._show(identity, tasks, tasks.resume(identity, context.task_id))

    def _ticket(self, identity, slots, *, edit):
        tasks, context = self._active(identity)
        if edit:
            if tasks is not self.tickets or context.task_status not in {"collecting", "pending_confirmation"}:
                return OperationResult(identity.request_id, "needs_clarification", "当前没有可补充的售后工单。",
                                       error_code="no_editable_ticket")
        elif any(manager.has_active_write(identity) for manager in self._tasks):
            return self._blocked(identity)
        previous = context.collected_slots if edit else {}
        order_id = slots.get("order_id", previous.get("order_id"))
        # 换订单时不继承旧店铺；只在明确保持同一订单时复用原作用域。
        shop_id = slots.get("shop_id", previous.get("shop_id") if order_id == previous.get("order_id") else None)
        if order_id and (not edit or {"shop_id", "order_id"} & slots.keys()):
            lookup = query_order(self.notifications.service, identity, order_id, shop_id)
            if lookup.status != "answered":
                return lookup
            slots = {**slots, "order_id": order_id, "shop_id": lookup.data["order"]["shop_id"]}
        if edit:
            result = self.tickets.update_slots(identity, context.task_id, slots)
        else:
            result = self.tickets.start(identity, {"issue_type": "other", **slots}, task_type=self.tickets.action_type)
            if result.pending_action or result.error_code == "missing_slots":
                self._displayed.pop(self._key(identity), None)
        return self._show(identity, self.tickets, result)

    @staticmethod
    def _blocked(identity):
        return OperationResult(identity.request_id, "needs_clarification", "已有待处理操作，请先继续或取消。",
                               error_code="pending_write_exists")

    def handle(self, identity, text, *, allow_control=True, use_model=True):
        with self._lock:
            try:
                self.notifications.service.validate_identity(identity)
            except RuleViolation as error:
                return error.result(identity)
            if not isinstance(text, str) or not text.strip() or len(text) > 3000 or any(not c.isprintable() for c in text):
                return OperationResult(identity.request_id, "needs_clarification", "请输入单行请求（最多 3000 字符）。")
            text = text.strip()
            control = self._control(identity, text) if allow_control else None
            if control is not None:
                return control
            if self.understanding is None or not use_model:
                parsed = recognize(text)
            else:
                from saas_support_agent.conversation.model import InvalidModelOutput, ModelUnavailable
                try:
                    self.notifications.service.authorize(identity)
                    tasks, context = self._active(identity)
                    if tasks is self.tickets and context.collected_slots.get("order_id"):
                        access = query_order(self.notifications.service, identity,
                                             context.collected_slots["order_id"], context.collected_slots.get("shop_id"))
                        if access.status != "answered":
                            return access
                    parsed = self.understanding.recognize(text, context)
                except RuleViolation as error:
                    return error.result(identity)
                except ModelUnavailable:
                    # 模型故障只回退到原有确定性句式；不猜测新业务动作。
                    parsed = recognize(text)
                    if parsed.intent == 'unsupported':
                        return OperationResult(identity.request_id, "needs_clarification", "模型暂不可用，请使用离线支持的句式。",
                                               error_code="model_unavailable")
                    result = self.handle(identity, text, allow_control=False, use_model=False)
                    return replace(result, data={**result.data, "understanding_mode": "offline_fallback"})
                except InvalidModelOutput:
                    return OperationResult(identity.request_id, "needs_clarification", "无法安全理解请求，请明确要咨询、查询或办理的事项。",
                                           error_code="invalid_model_output")
            intent, slots = parsed.intent, parsed.slots
            if intent in {"order_query", "ticket_query", "notification_query", "policy_query"}:
                # 读请求不会修改任务，也不把旧展示自动当作下次写入授权。
                if self._active(identity)[1]:
                    self._displayed.pop(self._key(identity), None)
            if intent == "order_query":
                if not slots.get("order_id"):
                    return OperationResult(identity.request_id, "needs_clarification", "请提供订单编号。",
                                           "Query", missing_slots=("order_id",))
                return query_order(self.notifications.service, identity, **slots)
            if intent == "ticket_query":
                if not slots:
                    return OperationResult(identity.request_id, "needs_clarification", "请提供工单编号。",
                                           "Query", missing_slots=("ticket_id",))
                return self.tickets.service.query_ticket(identity, **slots)
            if intent == "notification_query":
                missing = tuple(k for k in ("notification_type", "channel") if k not in slots)
                if missing:
                    return OperationResult(identity.request_id, "needs_clarification", "请提供通知类型和渠道。",
                                           "Query", missing_slots=missing)
                return self.notifications.query_preference(identity, **slots)
            if intent == "policy_query":
                if self.knowledge is not None:
                    return self.knowledge.answer(identity, text)
                evidence = POLICIES.get(slots.get("topic"))
                if evidence is None:
                    return OperationResult(identity.request_id, "no_evidence", "示例知识库没有足够证据，无法回答。",
                                           "Knowledge", error_code="no_evidence")
                return OperationResult(identity.request_id, "answered", evidence["text"], "Knowledge",
                                       evidence=(dict(evidence),))
            if intent in {"ticket_start", "ticket_edit"}:
                return self._ticket(identity, slots, edit=intent == "ticket_edit")
            if intent == "notification_edit":
                tasks, context = self._active(identity)
                if tasks is not self.notifications:
                    return OperationResult(identity.request_id, "needs_clarification", "当前没有可补充的通知任务。",
                                           error_code="no_editable_notification")
                return self._show(identity, tasks, tasks.update_slots(identity, context.task_id, slots))
            if intent == "notification_update":
                if any(manager.has_active_write(identity) for manager in self._tasks):
                    return self._blocked(identity)
                result = self.notifications.start(identity, slots)
                if result.pending_action:
                    self._displayed.pop(self._key(identity), None)
                return self._show(identity, self.notifications, result)
            if self.understanding is not None and use_model:
                return OperationResult(identity.request_id, "needs_clarification", "无法确定请求意图，请明确咨询、查询或办理的事项。",
                                       error_code="uncertain_intent")
            return OperationResult(identity.request_id, "unsupported",
                                   "未识别此离线句式。可输入咨询退货超期政策、查询订单或创建售后工单；确认请使用展示的命令。",
                                   error_code="unsupported_request")

    def http_request(self, identity, command, *, text="", task_id=None, slots=None, confirmation=None):
        """HTTP 控制事件与聊天分离；校验展示绑定后才委托原 Workflow。

        同一锁覆盖业务结果和展示快照，避免响应包含另一请求的确认版本。
        """
        with self._lock:
            self.notifications.service.authorize(identity)
            tasks, context = self._active(identity)
            if command == "message":
                result = self.handle(identity, text, allow_control=False)
            elif command == "inspect":
                result = OperationResult(identity.request_id, "answered", "当前任务状态。", "Query")
            elif command == "confirm":
                shown = self._displayed.get(self._key(identity))
                expected = ({"task_id": shown.task_id, "pending_action_id": shown.pending_action_id,
                             "revision": shown.revision, "payload_hash": shown.payload_hash,
                             "operation_id": shown.operation_id} if shown else None)
                if expected is None or confirmation != expected:
                    result = OperationResult(identity.request_id, "needs_confirmation", "确认已失效，请恢复并核对当前草稿。",
                                             error_code="stale_confirmation")
                else:
                    result = self._show(identity, shown.tasks, shown.tasks.confirm(
                        identity, shown.task_id, shown.pending_action_id, shown.revision, shown.payload_hash))
            elif context is None or context.task_id != task_id:
                result = OperationResult(identity.request_id, "needs_clarification", "任务不存在或已经变化。",
                                         error_code="stale_task")
            elif command == "slots":
                if tasks is self.tickets:
                    tasks._validate_patch(slots)
                    result = self._ticket(identity, slots, edit=True)
                else:
                    result = self._show(identity, tasks, tasks.update_slots(identity, task_id, slots))
            else:
                result = self._control(identity, {"cancel": "/取消", "resume": "/恢复", "reconcile": "/核对"}[command])
            _, context = self._active(identity)
            task = None if context is None else {
                "task_id": context.task_id, "task_type": context.task_type, "status": context.task_status,
                "slots": dict(context.collected_slots), "missing_slots": list(context.missing_slots),
            }
            shown = self._displayed.get(self._key(identity))
            binding = None
            if result.pending_action is not None and shown is not None:
                binding = {"task_id": shown.task_id, "pending_action_id": shown.pending_action_id,
                           "revision": shown.revision, "payload_hash": shown.payload_hash,
                           "operation_id": shown.operation_id}
            return result, task, binding
