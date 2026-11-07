"""One pending write per user/session. No LLM, HTTP adapter or full TaskContext.

The coordinator owns immutable drafts. Callers confirm a displayed reference,
not a caller-supplied draft. One instance serializes transitions; independent
instances/connections rely on SQLite transaction and business-version checks.
"""

from dataclasses import replace
from datetime import timedelta
import sqlite3
from threading import RLock
from uuid import uuid4

from saas_support_agent.core.models import (
    Confirmation, NotificationUpdateRequest, OperationResult,
    PendingAction, RuleViolation, TrustedIdentity, pending_hash,
)
from saas_support_agent.services.notification_service import NotificationService


class NotificationUpdateWorkflow:
    def __init__(self, service: NotificationService, *, confirmation_ttl: timedelta = timedelta(minutes=10)):
        if confirmation_ttl.total_seconds() <= 0:
            raise ValueError("confirmation_ttl must be positive")
        self.service = service
        self.confirmation_ttl = confirmation_ttl
        self._pending: dict[tuple[str, str], PendingAction] = {}
        self._lock = RLock()

    @staticmethod
    def _key(identity):
        return identity.user_id, identity.session_id

    def _get(self, identity):
        self.service.validate_identity(identity)
        pending = self._pending.get(self._key(identity))
        if pending is None:
            raise RuleViolation("needs_confirmation", "no_pending_action", "请重新准备并确认操作。")
        if pending.workspace_id != identity.workspace_id:
            raise RuleViolation("denied", "scope_or_permission_denied", "请返回原工作空间处理待确认操作。")
        return pending

    def _summary(self, identity, request, current):
        return (
            f"工作空间 {identity.workspace_id}，本人 {identity.user_id}："
            f"{request.notification_type}/{request.channel} "
            f"从{'开启' if current.enabled else '关闭'}改为{'开启' if request.enabled else '关闭'}。"
        )
    def _build(self, identity, request, *, previous=None):
        authorization, current = self.service.load_current(identity, request)
        summary = self._summary(identity, request, current)
        draft = PendingAction(
            previous.pending_action_id if previous else str(uuid4()),
            previous.revision + 1 if previous else 1, str(uuid4()),
            identity.user_id, identity.workspace_id, identity.session_id, self.service.action_type,
            request, current, authorization, summary, self.service.clock() + self.confirmation_ttl,
        )
        draft = replace(draft, canonical_payload_hash=pending_hash(draft))
        self._pending[self._key(identity)] = draft
        return OperationResult(identity.request_id, "needs_confirmation", summary, pending_action=draft)

    def prepare(self, identity: TrustedIdentity, request: NotificationUpdateRequest) -> OperationResult:
        with self._lock:
            try:
                self.service.validate_identity(identity)
                existing = self._pending.get(self._key(identity))
                if existing and existing.workflow_state in {"pending_confirmation", "confirmed", "pending_resolution"}:
                    # Do not leak another workspace's draft through this response.
                    return OperationResult(
                        identity.request_id, "needs_clarification", "已有待处理操作，请继续原操作，或明确取消后再发起新操作。",
                        error_code="pending_write_exists",
                    )
                return self._build(identity, request)
            except RuleViolation as error:
                return error.result(identity)
            except sqlite3.Error:
                return OperationResult(identity.request_id, "failed", "无法读取当前设置，尚未创建待执行操作。",
                                       error_code="database_unavailable")

    def revise(self, identity: TrustedIdentity, request: NotificationUpdateRequest) -> OperationResult:
        with self._lock:
            try:
                pending = self._get(identity)
                if pending.workflow_state != "pending_confirmation":
                    raise RuleViolation("failed", "action_not_editable", "当前操作不可修改，请先核对执行结果或发起新操作。")
                return self._build(identity, request, previous=pending)
            except RuleViolation as error:
                return error.result(identity)
            except sqlite3.Error:
                return OperationResult(identity.request_id, "failed", "无法刷新设置，原操作未被替换。",
                                       error_code="database_unavailable")

    def cancel(self, identity: TrustedIdentity, pending_action_id: str) -> OperationResult:
        with self._lock:
            try:
                pending = self._get(identity)
                if pending.pending_action_id != pending_action_id:
                    raise RuleViolation("failed", "stale_action", "取消请求不对应当前操作。")
                if pending.workflow_state == "cancelled":
                    return OperationResult(identity.request_id, "cancelled", "操作已取消。")
                if pending.workflow_state != "pending_confirmation":
                    raise RuleViolation("failed", "cannot_cancel_execution", "无法撤销已开始或已完成的操作，请核对回执。")
                self._pending[self._key(identity)] = replace(pending, workflow_state="cancelled")
                return OperationResult(identity.request_id, "cancelled", "操作已取消，未修改业务数据。")
            except RuleViolation as error:
                return error.result(identity)

    def confirm(self, identity: TrustedIdentity, pending_action_id: str,
                revision: int, payload_hash: str) -> OperationResult:
        """Explicit trusted-caller event; never interpreted from natural language."""
        with self._lock:
            try:
                pending = self._get(identity)
                if (type(revision) is not int or
                        (pending_action_id, revision, payload_hash) !=
                        (pending.pending_action_id, pending.revision, pending.canonical_payload_hash)):
                    raise RuleViolation("needs_confirmation", "stale_confirmation", "请确认当前展示的操作版本。")
                if pending.workflow_state == "cancelled":
                    return OperationResult(identity.request_id, "cancelled", "该操作已取消，不能执行。")
                if pending.workflow_state in {"succeeded", "failed", "pending_resolution"}:
                    # Finished/uncertain operations can only be read, never rerun.
                    result = self.service.reconcile(identity, pending)
                    return self._remember_result(identity, pending, result)
                confirmation = Confirmation(
                    pending.pending_action_id, pending.revision, pending.operation_id,
                    pending.canonical_payload_hash, identity.user_id, identity.workspace_id,
                    identity.session_id, self.service.clock(), pending.expires_at,
                )
                pending = replace(pending, workflow_state="confirmed")
                self._pending[self._key(identity)] = pending
                result = self.service.execute(identity, pending, confirmation)
                if result.error_code in {"confirmation_expired", "business_state_changed"}:
                    # Re-display from authoritative state; refreshing is never execution.
                    self._pending[self._key(identity)] = replace(pending, workflow_state="pending_confirmation")
                    refreshed = self.revise(identity, pending.payload)
                    return replace(refreshed, error_code=result.error_code) if refreshed.pending_action else refreshed
                return self._remember_result(identity, pending, result)
            except RuleViolation as error:
                return error.result(identity)

    def _remember_result(self, identity, pending, result):
        if result.status in {"succeeded", "failed"}:
            state = result.status
        elif pending.workflow_state in {"pending_resolution", "succeeded", "failed"}:
            # A failed read/authorization check does not change an execution fact.
            state = pending.workflow_state
        elif result.status == "denied":
            state = "failed"
        else:
            state = "pending_resolution"
        self._pending[self._key(identity)] = replace(pending, workflow_state=state)
        return result

    def resolve(self, identity: TrustedIdentity) -> OperationResult:
        with self._lock:
            try:
                pending = self._get(identity)
                if pending.workflow_state == "pending_confirmation":
                    return OperationResult(identity.request_id, "needs_confirmation", pending.displayed_summary,
                                           pending_action=pending)
                if pending.workflow_state == "cancelled":
                    return OperationResult(identity.request_id, "cancelled", "操作已取消，未执行。")
                result = self.service.reconcile(identity, pending)
                return self._remember_result(identity, pending, result)
            except RuleViolation as error:
                return error.result(identity)

    def inspect_pending(self, identity: TrustedIdentity) -> PendingAction | None:
        """仅向可信内部调用方提供同 scope 的不可变草稿，不代表确认仍有效。"""
        with self._lock:
            self.service.validate_identity(identity)
            pending = self._pending.get(self._key(identity))
            if pending is None or pending.workspace_id != identity.workspace_id:
                return None
            return pending

    def has_pending_write(self, identity: TrustedIdentity) -> bool:
        with self._lock:
            self.service.validate_identity(identity)
            pending = self._pending.get(self._key(identity))
            return pending is not None and pending.workflow_state in {
                "pending_confirmation", "confirmed", "pending_resolution",
            }

    def resume(self, identity: TrustedIdentity, pending_action_id: str) -> OperationResult:
        """Query 后恢复只做 revalidation；刷新草稿也不执行写入。

        pending_resolution 只能核对回执，不能因到期或失权丢弃操作关联。
        """
        with self._lock:
            try:
                pending = self._get(identity)
                if pending.pending_action_id != pending_action_id:
                    raise RuleViolation("needs_confirmation", "stale_action", "原任务已失效，请查看当前任务。")
                if pending.workflow_state != "pending_confirmation":
                    return self.resolve(identity)
                if pending.canonical_payload_hash != pending_hash(pending):
                    self._pending[self._key(identity)] = replace(pending, workflow_state="failed")
                    raise RuleViolation("failed", "invalid_draft", "待确认内容不一致，请重新发起。")
                try:
                    authorization, current = self.service.load_current(identity, pending.payload)
                except RuleViolation:
                    # 尚未执行且授权/参数失效，可以关闭；执行结果未知不能走这里。
                    self._pending[self._key(identity)] = replace(pending, workflow_state="failed")
                    raise
                expired = self.service.clock() >= pending.expires_at
                changed = authorization != pending.expected_versions or current != pending.current
                if expired or changed:
                    refreshed = self.revise(identity, pending.payload)
                    return replace(refreshed, error_code=("confirmation_expired" if expired else "business_state_changed"))
                return OperationResult(identity.request_id, "needs_confirmation", pending.displayed_summary,
                                       pending_action=pending)
            except RuleViolation as error:
                return error.result(identity)
            except sqlite3.Error:
                return OperationResult(identity.request_id, "failed", "暂时无法校验原任务，请稍后恢复。",
                                       error_code="database_unavailable")
