"""Immutable action drafts; these are not database tables or authentication."""

from dataclasses import asdict, dataclass, field
from datetime import datetime
import hashlib
import json
from typing import Any


ACTION_TYPE = "update_notification_preferences"
TICKET_ACTION_TYPE = "create_support_ticket"
NOTIFICATION_TYPES = frozenset({"task_assigned", "comment_mentioned"})
CHANNELS = frozenset({"email", "in_app"})


@dataclass(frozen=True)
class TrustedIdentity:
    """Supplied by a trusted caller. Never build this from unauthenticated input."""

    user_id: str
    workspace_id: str
    session_id: str
    request_id: str


@dataclass(frozen=True)
class NotificationUpdateRequest:
    # Missing values remain explicit. Natural-language normalization is deferred.
    notification_type: str | None = None
    channel: str | None = None
    enabled: bool | None = None


@dataclass(frozen=True)
class SupportTicketRequest:
    issue_type: str | None = None
    description: str | None = None
    project_id: str | None = None
    steps: str | None = None
    observed_error: str | None = None
    shop_id: str | None = None
    order_id: str | None = None


@dataclass(frozen=True)
class AuthorizationSnapshot:
    workspace_version: int
    membership_version: int


@dataclass(frozen=True)
class NotificationPreference:
    workspace_id: str
    user_id: str
    notification_type: str
    channel: str
    enabled: bool
    version: int


@dataclass(frozen=True)
class PendingAction:
    pending_action_id: str
    revision: int
    operation_id: str
    owner_user_id: str
    workspace_id: str
    session_id: str
    action_type: str
    payload: NotificationUpdateRequest | SupportTicketRequest
    current: NotificationPreference | tuple[int, ...] | None
    expected_versions: AuthorizationSnapshot
    displayed_summary: str
    expires_at: datetime
    canonical_payload_hash: str = ""
    workflow_state: str = "pending_confirmation"


def pending_hash(pending: PendingAction) -> str:
    """Bind the displayed operation, including scope and expected business state.

    A digest detects content mismatch; it is not an authentication credential.
    Workflow state is excluded so confirming does not change the operation.
    """
    document = asdict(pending)
    document.pop("canonical_payload_hash")
    document.pop("workflow_state")
    document["expires_at"] = pending.expires_at.isoformat()
    encoded = json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Confirmation:
    """Bound by the workflow to a stored draft after an explicit confirm call."""

    pending_action_id: str
    revision: int
    operation_id: str
    payload_hash: str
    owner_user_id: str
    workspace_id: str
    session_id: str
    confirmed_at: datetime
    expires_at: datetime


@dataclass(frozen=True)
class OperationReceipt:
    receipt_id: str
    workspace_id: str
    user_id: str
    operation_id: str
    pending_action_id: str
    action_revision: int
    action_type: str
    payload_hash: str
    status: str
    result_snapshot: dict[str, Any]
    error_code: str | None
    created_at: datetime


@dataclass(frozen=True)
class OperationResult:
    request_id: str
    status: str
    message: str
    branch: str = "Action"
    data: dict[str, Any] = field(default_factory=dict)
    evidence: tuple = ()
    missing_slots: tuple[str, ...] = ()
    pending_action: PendingAction | None = None
    receipt: OperationReceipt | None = None
    error_code: str | None = None


class RuleViolation(Exception):
    """An expected business rejection, safe to expose without database details."""

    def __init__(self, status: str, code: str, message: str, missing_slots: tuple = ()):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.missing_slots = missing_slots

    def result(self, identity: TrustedIdentity) -> OperationResult:
        return OperationResult(
            identity.request_id, self.status, self.message,
            error_code=self.code, missing_slots=self.missing_slots,
        )
