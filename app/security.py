import base64
import hashlib
import hmac
import secrets
from typing import Any, Dict, Optional

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from .config import settings

SESSION_COOKIE = "nexoflow_session"
ANONYMOUS_CSRF_COOKIE = "nexoflow_anon_csrf"
serializer = URLSafeTimedSerializer(settings.app_secret, salt="nexoflow-session-v2")
anonymous_serializer = URLSafeTimedSerializer(settings.app_secret, salt="nexoflow-anonymous-csrf-v2")


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    iterations = 600_000
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations, dklen=32)
    return f"pbkdf2_sha256${iterations}$" + base64.urlsafe_b64encode(salt).decode() + "$" + base64.urlsafe_b64encode(digest).decode()


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, rounds, salt_b64, digest_b64 = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        salt = base64.urlsafe_b64decode(salt_b64.encode())
        expected = base64.urlsafe_b64decode(digest_b64.encode())
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(rounds), dklen=32)
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def new_session(user_id: str, workspace_id: str) -> str:
    return serializer.dumps({"user_id": user_id, "workspace_id": workspace_id, "csrf": secrets.token_urlsafe(24)})


def read_session(value: Optional[str]) -> Optional[Dict[str, Any]]:
    if not value:
        return None
    try:
        return serializer.loads(value, max_age=settings.session_max_age)
    except (BadSignature, SignatureExpired):
        return None


def verify_csrf(session: Dict[str, Any], token: str) -> bool:
    return bool(token and hmac.compare_digest(str(session.get("csrf", "")), token))


def new_anonymous_csrf():
    token = secrets.token_urlsafe(24)
    return token, anonymous_serializer.dumps(token)


def verify_anonymous_csrf(cookie: Optional[str], token: str) -> bool:
    if not cookie or not token:
        return False
    try:
        expected = anonymous_serializer.loads(cookie, max_age=60 * 60)
        return hmac.compare_digest(str(expected), token)
    except (BadSignature, SignatureExpired):
        return False
