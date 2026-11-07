"""薄 HTTP 入口。业务调用统一委托 MerchantSupport。"""
from contextlib import ExitStack, asynccontextmanager, closing
import json
import logging
import os
from pathlib import Path
import time
from uuid import uuid4

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from saas_support_agent.api.auth import DemoIdentityStore
from saas_support_agent.api.schemas import (
    ConfirmationRequest, DraftResponse, MessageRequest, ReceiptResponse, ResultResponse,
    SessionRequest, SessionResponse, SlotsRequest, TaskRequest,
)
from saas_support_agent.core.models import OperationResult, RuleViolation
from saas_support_agent.merchant_support import MerchantSupport

logger = logging.getLogger(__name__)
security = HTTPBearer(auto_error=False)


def status_code(result):
    if result.status == "denied":
        return 403
    if result.error_code in {"knowledge_unavailable", "database_unavailable"}:
        return 503
    if result.error_code in {"stale_confirmation", "stale_task", "pending_write_exists",
                             "confirmation_expired", "business_state_changed", "cannot_cancel_execution",
                             "action_not_editable", "task_not_editable", "no_active_task"}:
        return 409
    if result.error_code == "invalid_parameters":
        return 422
    if result.status == "pending_resolution":
        return 202
    if result.status == "failed":
        return 503
    return 200


def present(result, task=None, binding=None):
    draft = None
    if result.pending_action is not None and binding is not None:
        pending = result.pending_action
        draft = DraftResponse(action_type=pending.action_type, summary=pending.displayed_summary,
                              expires_at=pending.expires_at, confirmation=ConfirmationRequest(**binding))
    receipt = None
    if result.receipt is not None:
        r = result.receipt
        receipt = ReceiptResponse(receipt_id=r.receipt_id, operation_id=r.operation_id,
                                  action_type=r.action_type, status=r.status,
                                  result=r.result_snapshot, created_at=r.created_at)
    payload = ResultResponse(request_id=result.request_id, status=result.status, branch=result.branch,
                             message=result.message, error_code=result.error_code, data=result.data,
                             evidence=list(result.evidence), missing_slots=list(result.missing_slots),
                             task=task, draft=draft, receipt=receipt)
    return JSONResponse(payload.model_dump(mode="json"), status_code=status_code(result))


def create_app(support: MerchantSupport, tokens: dict[str, str], *, lifespan=None):
    app = FastAPI(title="商家售后客服", version="0.1.0", lifespan=lifespan)
    identities = DemoIdentityStore(tokens)

    @app.middleware("http")
    async def trace(request, call_next):
        request.state.request_id = str(uuid4())
        start = time.monotonic()
        try:
            response = await call_next(request)
        except Exception:
            # 不记录异常文本、请求正文或令牌，避免将用户数据写进日志。
            response = present(OperationResult(request.state.request_id, "failed", "服务暂不可用。",
                                               error_code="internal_error"))
        response.headers["X-Request-ID"] = request.state.request_id
        route = request.scope.get("route")
        logger.info("request_id=%s route=%s status=%s elapsed_ms=%.2f", request.state.request_id,
                    getattr(route, "path", "unmatched"), response.status_code,
                    (time.monotonic() - start) * 1000)
        return response

    @app.exception_handler(HTTPException)
    async def http_error(request, error):
        body = ResultResponse(request_id=request.state.request_id, status="denied", branch="API",
                              message="请求未获授权或资源不存在。", error_code=str(error.detail))
        return JSONResponse(body.model_dump(mode="json"), error.status_code, headers=error.headers)

    @app.exception_handler(RequestValidationError)
    async def invalid(request, error):
        # Pydantic 原始错误可能包含输入原文，不能直接返回。
        return present(OperationResult(request.state.request_id, "needs_clarification", "请求字段或类型不合法。",
                                       "API", error_code="invalid_parameters"))

    @app.exception_handler(RuleViolation)
    async def rule_error(request, error):
        return present(OperationResult(request.state.request_id, error.status, error.message,
                                       "API", error_code=error.code, missing_slots=error.missing_slots))

    def user(credentials: HTTPAuthorizationCredentials | None = Depends(security)):
        return identities.authenticate(credentials)

    def identity(session_id: str, request: Request, owner=Depends(user)):
        result = identities.identity(owner, session_id, request.state.request_id)
        support.notifications.service.authorize(result)
        return result

    @app.get("/health")
    def health(owner=Depends(user)):
        return {"status": "ok", "mode": "local-demo", "session_storage": "memory"}

    @app.post("/v1/sessions", response_model=SessionResponse, status_code=201)
    def session(body: SessionRequest, request: Request, owner=Depends(user)):
        value = identities.create(owner, body.workspace_id, request.state.request_id, support.notifications.service)
        return SessionResponse(session_id=value.session_id, workspace_id=value.workspace_id)

    @app.post("/v1/sessions/{session_id}/messages", response_model=ResultResponse)
    def message(body: MessageRequest, who=Depends(identity)):
        return present(*support.http_request(who, "message", text=body.text))

    @app.get("/v1/sessions/{session_id}/task", response_model=ResultResponse)
    def task(who=Depends(identity)):
        return present(*support.http_request(who, "inspect"))

    @app.patch("/v1/sessions/{session_id}/task/slots", response_model=ResultResponse)
    def slots(body: SlotsRequest, who=Depends(identity)):
        return present(*support.http_request(who, "slots", task_id=body.task_id, slots=body.slots))

    @app.post("/v1/sessions/{session_id}/task/confirm", response_model=ResultResponse)
    def confirm(body: ConfirmationRequest, who=Depends(identity)):
        return present(*support.http_request(who, "confirm", confirmation=body.model_dump()))

    def control(command):
        def endpoint(body: TaskRequest, who=Depends(identity)):
            return present(*support.http_request(who, command, task_id=body.task_id))
        return endpoint

    for command in ("cancel", "resume", "reconcile"):
        app.add_api_route(f"/v1/sessions/{{session_id}}/task/{command}", control(command),
                          methods=["POST"], response_model=ResultResponse, name=command)
    return app


def local_app():
    """Uvicorn factory；不自动创建/清空业务库，不自动下载模型或重建索引。"""
    from saas_support_agent.repositories.database import Database

    path = Path(os.environ.get("SAAS_DATABASE", "runtime/http-demo.sqlite3"))
    if not path.is_file():
        raise ValueError("请先显式准备演示数据库或配置 SAAS_DATABASE")
    tokens = json.loads(os.environ.get("SAAS_DEMO_TOKENS", "{}"))
    support = MerchantSupport(Database(path))

    @asynccontextmanager
    async def lifespan(app):
        with ExitStack() as stack:
            from saas_support_agent.conversation.model import configured_model
            from saas_support_agent.conversation.understanding import ModelUnderstanding
            from saas_support_agent.knowledge.generation import EvidenceGenerator

            model = configured_model()
            if model is not None:
                stack.callback(model.close)
                support.understanding = ModelUnderstanding(model)
            if os.environ.get("SAAS_RAG_ENABLED") == "1":
                from saas_support_agent.knowledge.models import LocalModels
                from saas_support_agent.knowledge.retrieval import PolicyIndex, PolicyKnowledge

                root = Path(os.environ.get("SAAS_RAG_ROOT", "runtime/rag"))
                catalog = Path(os.environ.get("SAAS_POLICY_CATALOG", "data/merchant_policies/catalog.json"))
                models = LocalModels(root / "models")
                index = stack.enter_context(closing(PolicyIndex(root / "index", catalog, models)))
                support.knowledge = PolicyKnowledge(index, support.notifications.service,
                                                  generator=EvidenceGenerator(model) if model else None)
            yield
    return create_app(support, tokens, lifespan=lifespan)
