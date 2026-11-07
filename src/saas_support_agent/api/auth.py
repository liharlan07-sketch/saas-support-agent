"""仅用于本机演示的令牌映射和内存 Session，不提供生产登录能力。"""
from secrets import compare_digest
from threading import RLock
from uuid import uuid4

from fastapi import HTTPException

from saas_support_agent.core.models import TrustedIdentity


class DemoIdentityStore:
    def __init__(self, tokens):
        if not tokens or any(not isinstance(k, str) or len(k) < 24 or not isinstance(v, str) or not v
                             for k, v in tokens.items()):
            raise ValueError("配置至少一个长度不小于 24 的本地演示令牌及用户映射")
        self._tokens = dict(tokens)
        self._sessions = {}
        self._lock = RLock()

    def authenticate(self, credentials):
        if credentials is not None and credentials.scheme.lower() == "bearer":
            for token, user in self._tokens.items():
                if compare_digest(token.encode(), credentials.credentials.encode()):
                    return user
        raise HTTPException(401, "authentication_required", headers={"WWW-Authenticate": "Bearer"})

    def create(self, user, workspace, request_id, service):
        session_id = str(uuid4())
        identity = TrustedIdentity(user, workspace, session_id, request_id)
        service.authorize(identity)
        with self._lock:
            self._sessions[session_id] = (user, workspace)
        return identity

    def identity(self, user, session_id, request_id):
        with self._lock:
            owner = self._sessions.get(session_id)
        # 不泄露其他用户是否拥有这个 Session。
        if owner is None or owner[0] != user:
            raise HTTPException(404, "session_not_found")
        return TrustedIdentity(user, owner[1], session_id, request_id)
