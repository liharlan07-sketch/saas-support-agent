"""结构化任务入口：收集参数、关联草稿、显式恢复；不解析自然语言。"""

from dataclasses import asdict, replace
import sqlite3
from types import MappingProxyType
from uuid import uuid4

from saas_support_agent.core.models import (
    ACTION_TYPE, CHANNELS, NOTIFICATION_TYPES, NotificationUpdateRequest,
    OperationResult, RuleViolation, TrustedIdentity,
)
from saas_support_agent.core.task_context import TaskContext, TaskContextStore, TaskRecord
from saas_support_agent.workflows.notification_update import NotificationUpdateWorkflow


class TaskContextManager:
    """一个 Manager 配对一个 Workflow，所有任务入口复用同一实例。

    未准备时参数归 TaskRecord；准备后归 PendingAction。TaskContext 只是投影，
    不能用其中的状态跳过确认、授权或事务内 revalidation。
    """

    action_type = ACTION_TYPE
    request_type = NotificationUpdateRequest
    required_slots = frozenset({"notification_type", "channel", "enabled"})

    def __init__(self, workflow: NotificationUpdateWorkflow):
        self.workflow = workflow
        self.service = workflow.service
        self._store = TaskContextStore()

    def _project(self, identity, record):
        status = record.terminal_status or "collecting"
        request = record.collected
        if record.pending_action_id and not record.terminal_status:
            pending = self.workflow.inspect_pending(identity)
            if pending is None or pending.pending_action_id != record.pending_action_id:
                status = "invalidated"
            else:
                status = pending.workflow_state
                request = pending.payload
            if status in {"succeeded", "failed", "cancelled", "invalidated"}:
                record = replace(record, terminal_status=status, collected=None)
                self._store.put(record)
        active = status in {"collecting", "pending_confirmation", "confirmed", "pending_resolution"}
        values = asdict(request) if request is not None and active else {}
        missing = tuple(k for k, v in values.items() if v is None and k in self.required_slots)
        return TaskContext(
            record.task_id, record.owner_user_id, record.workspace_id, record.session_id,
            record.task_type, status, MappingProxyType({k: v for k, v in values.items() if v is not None}),
            missing, record.pending_action_id,
        )

    def get_context(self, identity: TrustedIdentity) -> TaskContext | None:
        """只供可信内部调用方读取；快照不是可执行参数或授权凭据。"""
        with self._store.lock:
            self.service.validate_identity(identity)
            record = self._store.get(identity)
            return self._project(identity, record) if record else None

    def _require(self, identity, task_id, *, allow_invalidated=False):
        context = self.get_context(identity)
        if context is None or context.task_id != task_id:
            raise RuleViolation("needs_clarification", "stale_task", "任务已变化，请重新查看当前任务。")
        if context.task_status == "invalidated" and not allow_invalidated:
            raise RuleViolation("needs_clarification", "stale_task", "原任务关联已失效，请重新发起。")
        return context

    @staticmethod
    def _validate_patch(slots):
        if type(slots) is not dict or set(slots) - {"notification_type", "channel", "enabled"}:
            raise RuleViolation("needs_clarification", "invalid_parameters", "只接受通知类型、渠道和目标开关。")
        for key, value in slots.items():
            if value is None:
                continue
            valid = (type(value) is bool if key == "enabled" else
                     type(value) is str and value in (NOTIFICATION_TYPES if key == "notification_type" else CHANNELS))
            if not valid:
                raise RuleViolation("needs_clarification", "invalid_parameters", "通知参数类型或取值不合法。")

    def _guard(self, identity, operation):
        # 所有复合操作在同一锁内完成，避免两个 start 同时占用当前会话。
        with self._store.lock:
            try:
                self.service.validate_identity(identity)
                return operation()
            except RuleViolation as error:
                return error.result(identity)
            except sqlite3.Error:
                return OperationResult(identity.request_id, "failed", "暂时无法读取业务状态，请稍后重试。",
                                       error_code="database_unavailable")

    def _save_preparation(self, identity, record, request, result):
        if result.pending_action:
            # 转交权威参数给 Workflow，Store 不保留另一份 payload 或 revision。
            self._store.put(replace(record, collected=None, pending_action_id=result.pending_action.pending_action_id))
        elif result.error_code == "missing_slots":
            self._store.put(replace(record, collected=request))
        elif record.pending_action_id is None:
            self._store.put(replace(record, collected=None, terminal_status="failed"))
        return result

    def start(self, identity: TrustedIdentity, slots: dict | None = None, *, task_type: str = ACTION_TYPE) -> OperationResult:
        """新任务不继承旧 slots。第二个写请求必须先显式取消原任务。"""
        def operation():
            if self.has_active_write(identity):
                return self._blocked(identity)
            if task_type != self.action_type:
                return OperationResult(identity.request_id, "unsupported", "当前协调器不支持该任务。",
                                       error_code="unsupported_task")
            patch = {} if slots is None else slots
            self._validate_patch(patch)
            self.service.authorize(identity)
            request = self.request_type(**patch)
            record = TaskRecord(str(uuid4()), identity.user_id, identity.workspace_id,
                                identity.session_id, task_type, collected=request)
            return self._save_preparation(identity, record, request, self.workflow.prepare(identity, request))
        return self._guard(identity, operation)

    def has_active_write(self, identity: TrustedIdentity) -> bool:
        with self._store.lock:
            return self.workflow.has_pending_write(identity) or any(
                self._project(replace(identity, workspace_id=record.workspace_id), record).active
                for record in self._store.for_session(identity)
            )

    @staticmethod
    def _blocked(identity):
        return OperationResult(identity.request_id, "needs_clarification",
                               "已有待处理任务，请继续原任务，或先明确取消再切换。", error_code="pending_write_exists")

    def update_slots(self, identity: TrustedIdentity, task_id: str, slots: dict) -> OperationResult:
        """补参保留 task_id；已展示草稿的有效修改必须经过 revise。

        非法或不完整的草稿修改整体拒绝，不出现 Memory 已改、旧确认仍有效的分裂。
        """
        def operation():
            context = self._require(identity, task_id)
            if context.task_status not in {"collecting", "pending_confirmation"}:
                raise RuleViolation("failed", "task_not_editable", "当前任务不可修改，请核对结果或新建任务。")
            self._validate_patch(slots)
            self.service.authorize(identity)
            request = self.request_type(**{**context.collected_slots, **slots})
            record = self._store.get(identity)
            if record.pending_action_id:
                self.service.validate_request(request)
                result = self.workflow.revise(identity, request)
            else:
                result = self.workflow.prepare(identity, request)
            return self._save_preparation(identity, record, request, result)
        return self._guard(identity, operation)

    def confirm(self, identity: TrustedIdentity, task_id: str, pending_action_id: str,
                revision: int, payload_hash: str) -> OperationResult:
        """确认原样交给 Workflow，不能从当前上下文自动补成有效确认。"""
        def operation():
            context = self._require(identity, task_id)
            if not context.pending_action_id or context.pending_action_id != pending_action_id:
                raise RuleViolation("needs_confirmation", "stale_confirmation", "请确认当前展示的操作。")
            result = self.workflow.confirm(identity, pending_action_id, revision, payload_hash)
            self.get_context(identity)
            return result
        return self._guard(identity, operation)

    def cancel(self, identity: TrustedIdentity, task_id: str) -> OperationResult:
        def operation():
            context = self._require(identity, task_id)
            if context.pending_action_id:
                result = self.workflow.cancel(identity, context.pending_action_id)
                self.get_context(identity)
                return result
            if context.task_status != "collecting":
                raise RuleViolation("failed", "task_not_active", "任务已结束。")
            record = self._store.get(identity)
            self._store.put(replace(record, collected=None, terminal_status="cancelled"))
            return OperationResult(identity.request_id, "cancelled", "任务已取消，未执行业务写入。")
        return self._guard(identity, operation)

    def resume(self, identity: TrustedIdentity, task_id: str) -> OperationResult:
        """显式恢复先重验；查询本身不恢复或确认任务。"""
        def operation():
            context = self._require(identity, task_id)
            if not context.active:
                raise RuleViolation("needs_clarification", "task_closed", "任务已结束，请重新发起。")
            if context.pending_action_id:
                result = self.workflow.resume(identity, context.pending_action_id)
                self.get_context(identity)
                return result
            try:
                self.service.authorize(identity)
            except RuleViolation:
                record = self._store.get(identity)
                self._store.put(replace(record, collected=None, terminal_status="invalidated"))
                raise
            return OperationResult(identity.request_id, "needs_clarification", "请继续补齐当前任务参数。",
                                   missing_slots=context.missing_slots)
        return self._guard(identity, operation)

    def resolve(self, identity: TrustedIdentity, task_id: str) -> OperationResult:
        def operation():
            context = self._require(identity, task_id)
            if not context.pending_action_id:
                raise RuleViolation("needs_clarification", "no_pending_action", "尚未生成待确认操作。")
            result = self.workflow.resolve(identity)
            self.get_context(identity)
            return result
        return self._guard(identity, operation)

    def query_preference(self, identity: TrustedIdentity, notification_type: str, channel: str) -> OperationResult:
        """有限 Query 入口只调用已有读服务；无任意 callback，无任务栈。"""
        with self._store.lock:
            return self.service.query_preference(identity, notification_type, channel)

    def cleanup(self, identity: TrustedIdentity, task_id: str) -> OperationResult:
        """只清除终态；pending_resolution 仍是活跃任务，必须保留核对关联。"""
        def operation():
            # 失效任务禁止恢复，但仍可清理；清理仍须匹配当前 scope 和 task_id。
            context = self._require(identity, task_id, allow_invalidated=True)
            if context.active:
                raise RuleViolation("needs_clarification", "active_task_retained", "任务尚未结束，不能清除。")
            self._store.remove(identity)
            return OperationResult(identity.request_id, "answered", "已清除结束的任务上下文。")
        return self._guard(identity, operation)
