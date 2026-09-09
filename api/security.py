import hashlib
import hmac
import os
import secrets
import threading
import time
from collections import OrderedDict, deque
from urllib.parse import urlsplit

from fastapi.responses import JSONResponse, RedirectResponse


ADMIN_PASSWORD = os.getenv("ODIN_ADMIN_PASSWORD", "")
VIEWER_PASSWORD = os.getenv("ODIN_VIEWER_PASSWORD", "")
SESSION_SECRET = os.getenv("ODIN_SESSION_SECRET") or secrets.token_hex(32)
COOKIE_NAME = "odin_admin"
COOKIE_TTL = 8 * 3600
PUBLIC_READ = os.getenv("ODIN_PUBLIC_READ", "false").lower() == "true"
_attempts = OrderedDict()
_attempts_lock = threading.Lock()


def make_token(role="admin", now=None):
    timestamp = int(time.time() if now is None else now)
    payload = f"{role}.{timestamp}"
    signature = hmac.new(SESSION_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{signature}"


def token_role(token, now=None):
    try:
        role, timestamp, signature = token.split(".")
        if role not in {"admin", "viewer"}:
            return None
        expected = make_token(role, int(timestamp)).rsplit(".", 1)[1]
        age = (time.time() if now is None else now) - int(timestamp)
        if 0 <= age < COOKIE_TTL and hmac.compare_digest(signature, expected):
            return role
    except (ValueError, TypeError):
        pass
    return None


def password_role(password):
    candidate = hashlib.sha256(password.encode()).digest()
    for role, configured in (("admin", ADMIN_PASSWORD), ("viewer", VIEWER_PASSWORD)):
        if configured and hmac.compare_digest(candidate, hashlib.sha256(configured.encode()).digest()):
            return role
    return None


def is_admin(request):
    return token_role(request.cookies.get(COOKIE_NAME, "")) == "admin"


def allow_request(key, limit=10, period=60):
    now = time.monotonic()
    with _attempts_lock:
        attempts = _attempts.setdefault(key, deque())
        _attempts.move_to_end(key)
        while attempts and attempts[0] <= now - period:
            attempts.popleft()
        if len(attempts) >= limit:
            return False
        attempts.append(now)
        if len(_attempts) > 4096:
            _attempts.popitem(last=False)
        return True


def install_security(app):
    @app.middleware("http")
    async def access_control(request, call_next):
        path = request.url.path
        public = path.startswith("/static/") or path == "/settings/login"
        role = token_role(request.cookies.get(COOKIE_NAME, ""))
        writes = request.method not in {"GET", "HEAD", "OPTIONS"}
        runs_analysis = path.startswith("/api/analyze/") and path.endswith("/stream")
        response = None
        if writes:
            origin = request.headers.get("origin")
            if request.headers.get("sec-fetch-site") == "cross-site" or (
                origin and urlsplit(origin).netloc != request.headers.get("host")
            ):
                response = JSONResponse({"detail": "Origine non autorisee"}, status_code=403)
        if response is None and not public:
            if (writes or runs_analysis) and role != "admin":
                response = JSONResponse({"detail": "Droits administrateur requis"}, status_code=403)
            elif not role and not PUBLIC_READ:
                response = (JSONResponse({"detail": "Authentification requise"}, status_code=401)
                            if path.startswith("/api/") else RedirectResponse("/settings/login", status_code=303))
        if response is None and (writes or runs_analysis):
            client = request.client.host if request.client else "unknown"
            login = path == "/settings/login"
            if not allow_request((client, "login" if login else "write"), 10 if login else 120):
                response = JSONResponse({"detail": "Trop de demandes, reessayez dans une minute"}, status_code=429,
                                        headers={"Retry-After": "60"})
        if response is None:
            response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
        )
        if not path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-store"
        return response