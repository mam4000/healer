"""Authentication helpers for the public HEALER API.

Production requests arrive either through the IAP-protected load balancer or
from ChemQuery's Cloud Run service.  Local development deliberately remains
available without either integration when ``HEALER_AUTH_MODE=disabled``.
"""
from __future__ import annotations

import base64
import contextvars
import hashlib
import hmac
import json
import os
import time
from dataclasses import dataclass

from fastapi import HTTPException, Request


_principal: contextvars.ContextVar["Principal | None"] = contextvars.ContextVar("healer_principal", default=None)


@dataclass(frozen=True)
class Principal:
    subject: str
    email: str | None
    tenant_id: str | None
    auth_mode: str

    @property
    def owner_key(self) -> str:
        return ":".join((self.tenant_id or "standalone", self.subject))


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _internal_principal(request: Request) -> Principal | None:
    """Validate ChemQuery's short-lived, HMAC-signed caller context."""
    secret = os.environ.get("HEALER_CHEMQUERY_SHARED_SECRET", "")
    payload = request.headers.get("X-Healer-Caller")
    signature = request.headers.get("X-Healer-Caller-Signature", "")
    if not payload or not signature:
        return None
    if not secret:
        raise HTTPException(status_code=401, detail="Internal caller authentication is not configured")
    expected = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise HTTPException(status_code=401, detail="Invalid internal caller signature")
    try:
        claims = json.loads(_b64decode(payload))
        expires_at = int(claims["exp"])
        subject = str(claims["sub"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=401, detail="Invalid internal caller context") from exc
    if expires_at < time.time() or expires_at > time.time() + 600:
        raise HTTPException(status_code=401, detail="Expired internal caller context")
    return Principal(subject=subject, email=claims.get("email"), tenant_id=claims.get("tenant_id"), auth_mode="chemquery")


def _iap_principal(request: Request) -> Principal:
    audience = os.environ.get("HEALER_IAP_JWT_AUDIENCE", "")
    assertion = request.headers.get("X-Goog-IAP-JWT-Assertion", "")
    if not audience:
        raise HTTPException(status_code=500, detail="IAP JWT audience is not configured")
    if not assertion:
        raise HTTPException(status_code=401, detail="IAP authentication is required")
    try:
        from google.auth.transport.requests import Request as GoogleRequest
        from google.oauth2 import id_token

        claims = id_token.verify_token(
            assertion,
            GoogleRequest(),
            audience=audience,
            certs_url="https://www.gstatic.com/iap/verify/public_key",
        )
        subject = str(claims["sub"])
    except Exception as exc:
        raise HTTPException(status_code=401, detail="Invalid IAP authentication assertion") from exc
    return Principal(subject=subject, email=claims.get("email"), tenant_id=None, auth_mode="iap")


async def require_principal(request: Request):
    principal = _internal_principal(request)
    mode = os.environ.get("HEALER_AUTH_MODE", "disabled").strip().lower()
    if principal is None:
        if mode == "iap":
            principal = _iap_principal(request)
        elif mode == "disabled":
            principal = Principal(subject="local", email=None, tenant_id=None, auth_mode="local")
        else:
            raise HTTPException(status_code=500, detail="Invalid HEALER_AUTH_MODE")
    request.state.healer_principal = principal
    token = _principal.set(principal)
    try:
        yield principal
    finally:
        _principal.reset(token)


def current_principal() -> Principal:
    """Return the request principal, with a local fallback for direct unit calls."""
    return _principal.get() or Principal(subject="local", email=None, tenant_id=None, auth_mode="local")
