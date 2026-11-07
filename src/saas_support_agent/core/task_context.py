"""任务记录只保留收集中的参数或 PendingAction 引用，不保存执行事实。"""

from dataclasses import dataclass
from threading import RLock
from typing import Mapping

from saas_support_agent.core.models import NotificationUpdateRequest, SupportTicketRequest, TrustedIdentity


@dataclass(frozen=True)
class TaskRecord:
    task_id: str
    owner_user_id: str
    workspace_id: str
    session_id: str
    task_type: str
    collected: NotificationUpdateRequest | SupportTicketRequest | None = None
    pending_action_id: str | None = None
    terminal_status: str | None = None


@dataclass(frozen=True)
class TaskContext:
    """当前任务的只读投影；关联草稿后，状态和参数以 Workflow 为准。"""

    task_id: str
    owner_user_id: str
    workspace_id: str
    session_id: str
    task_type: str
    task_status: str
    collected_slots: Mapping[str, str | bool]
    missing_slots: tuple[str, ...] = ()
    pending_action_id: str | None = None

    @property
    def active(self) -> bool:
        return self.task_status in {"collecting", "pending_confirmation", "confirmed", "pending_resolution"}


class TaskContextStore:
    """V1 进程内存储；协调层共享此锁，串行完成读取、调用和更新。"""

    def __init__(self):
        self._records: dict[tuple[str, str, str], TaskRecord] = {}
        self.lock = RLock()

    @staticmethod
    def key(identity: TrustedIdentity):
        return identity.user_id, identity.workspace_id, identity.session_id

    def get(self, identity: TrustedIdentity) -> TaskRecord | None:
        with self.lock:
            return self._records.get(self.key(identity))

    def put(self, record: TaskRecord) -> None:
        with self.lock:
            self._records[(record.owner_user_id, record.workspace_id, record.session_id)] = record

    def for_session(self, identity: TrustedIdentity) -> tuple[TaskRecord, ...]:
        with self.lock:
            return tuple(r for r in self._records.values()
                         if (r.owner_user_id, r.session_id) == (identity.user_id, identity.session_id))

    def remove(self, identity: TrustedIdentity) -> None:
        with self.lock:
            self._records.pop(self.key(identity), None)
