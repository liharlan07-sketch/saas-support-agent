"""HTTP 输入拒绝未知字段；响应不直接暴露内部授权快照。"""
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class SessionRequest(Contract):
    workspace_id: str = Field(min_length=1, max_length=128)


class SessionResponse(Contract):
    session_id: str
    workspace_id: str


class MessageRequest(Contract):
    text: str = Field(min_length=1, max_length=3000)


class TaskRequest(Contract):
    task_id: str = Field(min_length=1, max_length=128)


class SlotsRequest(TaskRequest):
    slots: dict[str, str | bool | None]


class ConfirmationRequest(TaskRequest):
    pending_action_id: str = Field(min_length=1, max_length=128)
    operation_id: str = Field(min_length=1, max_length=128)
    revision: int = Field(ge=1)
    payload_hash: str = Field(pattern=r"^[a-f0-9]{64}$")


class DraftResponse(Contract):
    action_type: str
    summary: str
    expires_at: datetime
    confirmation: ConfirmationRequest


class TaskResponse(Contract):
    task_id: str
    task_type: str
    status: str
    slots: dict[str, Any]
    missing_slots: list[str]


class ReceiptResponse(Contract):
    receipt_id: str
    operation_id: str
    action_type: str
    status: str
    result: dict[str, Any]
    created_at: datetime


class ResultResponse(Contract):
    request_id: str
    status: str
    branch: str
    message: str
    error_code: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    missing_slots: list[str] = Field(default_factory=list)
    task: TaskResponse | None = None
    draft: DraftResponse | None = None
    receipt: ReceiptResponse | None = None
