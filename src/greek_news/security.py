"""Fail-closed role tokens for a small single-operator deployment."""

from dataclasses import dataclass
from secrets import compare_digest

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from greek_news.config import Settings, get_settings


bearer = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class Actor:
    role: str


def current_actor(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    settings: Settings = Depends(get_settings),
) -> Actor:
    if not settings.admin_token:
        raise HTTPException(status_code=503, detail="Admin authentication is not configured")
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(status_code=401, detail="Bearer token required",
                            headers={"WWW-Authenticate": "Bearer"})
    if compare_digest(credentials.credentials, settings.admin_token):
        return Actor("admin")
    if settings.editor_token and compare_digest(credentials.credentials, settings.editor_token):
        return Actor("editor")
    raise HTTPException(status_code=401, detail="Invalid token",
                        headers={"WWW-Authenticate": "Bearer"})


def require_admin(actor: Actor = Depends(current_actor)) -> Actor:
    if actor.role != "admin":
        raise HTTPException(status_code=403, detail="Admin role required")
    return actor


def require_editor(actor: Actor = Depends(current_actor)) -> Actor:
    return actor