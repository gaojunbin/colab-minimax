import hashlib
import hmac
from typing import Optional
from fastapi import Request, HTTPException, status
from app.config import config

SESSION_COOKIE_NAME = "colab_minimax_session"


def create_session_token(username: str) -> str:
    sig = hmac.new(
        config.secret_key.encode("utf-8"),
        f"session:{username}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"{username}.{sig}"


def verify_session_token(token: Optional[str]) -> bool:
    if not token or "." not in token:
        return False
    username, sig = token.split(".", 1)
    if username != config.web_username:
        return False
    expected = hmac.new(
        config.secret_key.encode("utf-8"),
        f"session:{username}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(sig, expected)


def is_authenticated(request: Request) -> bool:
    # 1. Check Session Cookie
    cookie_token = request.cookies.get(SESSION_COOKIE_NAME)
    if verify_session_token(cookie_token):
        return True

    # 2. Check X-API-Key Header
    api_key = request.headers.get("X-API-Key") or request.headers.get("x-api-key")
    if api_key and hmac.compare_digest(api_key, config.api_key):
        return True

    # 3. Check Authorization: Bearer <API_KEY>
    auth_header = request.headers.get("Authorization") or request.headers.get("authorization")
    if auth_header and auth_header.lower().startswith("bearer "):
        bearer_token = auth_header.split(" ", 1)[1].strip()
        if hmac.compare_digest(bearer_token, config.api_key):
            return True

    # 4. Check query param api_key
    q_key = request.query_params.get("api_key")
    if q_key and hmac.compare_digest(q_key, config.api_key):
        return True

    return False


def require_auth(request: Request) -> None:
    if not is_authenticated(request):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized: Please log in or provide a valid X-API-Key / Bearer token.",
        )
