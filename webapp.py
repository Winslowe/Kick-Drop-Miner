"""FastAPI entry point for the multi-user server edition."""

from collections import defaultdict, deque
import asyncio
import html
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import threading
import time
import urllib.error
import urllib.request


ROOT = Path(__file__).resolve().parent


def _load_env(path):
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


_load_env(ROOT / ".env")
os.environ.setdefault("KDM_DATA_DIR", str(ROOT / "data"))
os.environ.setdefault("KDM_SYSTEM_CHROMIUM", "1")

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.sessions import SessionMiddleware

from core.accounts import AccountStore
from core.browser import cleanup_browser_resources
from core.service import MinerService
from utils.helpers import DATA_DIR


PASSWORD_HASH = os.environ.get("KDM_PASSWORD_HASH", "")
SESSION_SECRET = os.environ.get("KDM_SESSION_SECRET", "")
SECURE_COOKIES = os.environ.get("KDM_SECURE_COOKIES", "0") == "1"
ADMIN_USERNAME = os.environ.get("KDM_ADMIN_USERNAME", "admin")
REGISTRATION_ENABLED = os.environ.get("KDM_REGISTRATION_ENABLED", "1") == "1"
MAX_ACTIVE_MINERS = max(1, int(os.environ.get("KDM_MAX_ACTIVE_MINERS", "3")))

if not PASSWORD_HASH or not SESSION_SECRET:
    raise RuntimeError(
        "KDM_PASSWORD_HASH ve KDM_SESSION_SECRET ortam ayarları zorunludur."
    )


def _client_ip(request):
    forwarded = request.headers.get("cf-connecting-ip") or request.headers.get(
        "x-forwarded-for"
    )
    if forwarded:
        return forwarded.split(",", 1)[0].strip()
    return request.client.host if request.client else "unknown"


class ServiceRegistry:
    def __init__(self):
        self._services = {}
        self._lock = threading.RLock()

    @staticmethod
    def _data_dir(user):
        if user["role"] == "admin":
            return DATA_DIR
        path = os.path.join(DATA_DIR, "users", user["id"])
        os.makedirs(path, exist_ok=True)
        return path

    def get(self, user):
        with self._lock:
            service = self._services.get(user["id"])
            if service is None:
                service = MinerService(
                    data_dir=self._data_dir(user),
                    user_id=user["id"],
                    username=user["username"],
                )
                self._services[user["id"]] = service
            return service

    def start(self, user, item_id=None):
        with self._lock:
            service = self.get(user)
            if not service.is_running():
                active_count = sum(
                    item.is_running() for item in self._services.values()
                )
                if active_count >= MAX_ACTIVE_MINERS:
                    raise ValueError(
                        "Sunucu eş zamanlı madenci sınırına ulaştı. "
                        "Aktif görevlerden biri bitince tekrar deneyin."
                    )
            service.start(item_id)

    def stop_user(self, user_id):
        with self._lock:
            service = self._services.get(user_id)
        if service:
            service.stop()

    def user_runtime(self, user):
        service = self.get(user)
        state = service.snapshot()
        active = next((item for item in state["items"] if item["active"]), None)
        return {
            "queue_count": state["stats"]["total"],
            "completed": state["stats"]["completed"],
            "browser_count": state["browser_count"],
            "queue_running": state["queue_running"],
            "cookie_ready": state["cookie"]["available"]
            and not state["cookie"]["expired"],
            "active_channel": active.get("url") if active else None,
            "active_status": active.get("status_label") if active else None,
        }

    def reward_name_for_image(self, filename):
        with self._lock:
            services = list(self._services.values())
        for service in services:
            inventory = service.snapshot().get("inventory") or {}
            for campaign in inventory.get("campaigns") or []:
                for reward in campaign.get("rewards") or []:
                    image_url = str(
                        reward.get("image_url")
                        or reward.get("image")
                        or reward.get("icon_url")
                        or ""
                    )
                    if image_url.rsplit("/", 1)[-1].casefold() == filename.casefold():
                        return str(reward.get("name") or campaign.get("name") or "Kick Drop")
        return "Kick Drop"

    def shutdown(self):
        with self._lock:
            services = list(self._services.values())
        for service in services:
            service.shutdown()


app = FastAPI(
    title="Kick Drop Miner",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
app.add_middleware(
    SessionMiddleware,
    secret_key=SESSION_SECRET,
    session_cookie="kdm_session",
    max_age=60 * 60 * 24 * 14,
    same_site="strict",
    https_only=SECURE_COOKIES,
)
app.mount("/static", StaticFiles(directory=ROOT / "web" / "static"), name="static")
app.mount("/assets", StaticFiles(directory=ROOT / "assets"), name="assets")

accounts = AccountStore(os.path.join(DATA_DIR, "accounts.sqlite3"))
accounts.bootstrap_admin(ADMIN_USERNAME, PASSWORD_HASH)
services = ServiceRegistry()
login_attempts = defaultdict(lambda: deque(maxlen=10))
registration_attempts = defaultdict(lambda: deque(maxlen=5))
last_seen_updates = {}


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["Permissions-Policy"] = (
        "camera=(), microphone=(), geolocation=(), payment=()"
    )
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self' https://static.cloudflareinsights.com; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: https:; "
        "connect-src 'self' https://cloudflareinsights.com; "
        "frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
    )
    if request.url.path.startswith("/api/") or request.url.path in ("/", "/health"):
        response.headers["Cache-Control"] = "no-store"
    return response


class LoginBody(BaseModel):
    username: str = Field(min_length=3, max_length=32)
    password: str = Field(min_length=1, max_length=256)


class RegisterBody(BaseModel):
    username: str = Field(min_length=3, max_length=32)
    email: str = Field(default="", max_length=160)
    password: str = Field(min_length=8, max_length=256)


class StreamBody(BaseModel):
    url: str = Field(min_length=4, max_length=300)
    minutes: int = Field(default=120, ge=0, le=10080)


class StartBody(BaseModel):
    item_id: str | None = None


class CookieBody(BaseModel):
    cookies: list[dict]


class KickLoginBody(BaseModel):
    username: str
    password: str


class ActiveBody(BaseModel):
    active: bool


def _session_user(request):
    user_id = request.session.get("user_id")
    if not user_id:
        return None
    user = accounts.get(user_id)
    if (
        not user
        or not user["active"]
        or int(user["session_version"])
        != int(request.session.get("session_version") or 0)
    ):
        request.session.clear()
        return None
    now = time.time()
    if now - last_seen_updates.get(user_id, 0) > 60:
        last_seen_updates[user_id] = now
        accounts.touch(user_id)
    return user


def require_auth(request: Request):
    user = _session_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Oturum gerekli.")
    return user


def require_csrf(request: Request):
    user = require_auth(request)
    token = request.headers.get("x-csrf-token")
    if not token or not hmac.compare_digest(
        token, str(request.session.get("csrf") or "")
    ):
        raise HTTPException(status_code=403, detail="Güvenlik doğrulaması başarısız.")
    return user


def require_admin(request: Request):
    user = (
        require_auth(request)
        if request.method in {"GET", "HEAD", "OPTIONS"}
        else require_csrf(request)
    )
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Yönetici yetkisi gerekli.")
    return user


def _rate_limited(bucket, key, limit, window):
    now = time.time()
    attempts = bucket[key]
    while attempts and now - attempts[0] > window:
        attempts.popleft()
    if len(attempts) >= limit:
        return True
    attempts.append(now)
    return False


@app.on_event("startup")
def startup():
    cleanup_browser_resources()


@app.on_event("shutdown")
def shutdown():
    services.shutdown()
    cleanup_browser_resources()


@app.exception_handler(ValueError)
async def value_error_handler(_request, error):
    return JSONResponse(status_code=400, content={"detail": str(error)})


@app.get("/health")
def health():
    return {"ok": True, "service": "kick-drop-miner"}


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return FileResponse(ROOT / "assets" / "logo.png", media_type="image/png")


def _reward_placeholder(filename):
    title = services.reward_name_for_image(filename).strip() or "Kick Drop"
    words = title.split()
    lines = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if current and len(candidate) > 18:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    lines = lines[:2] or ["Kick Drop"]
    first_y = 195 - (len(lines) - 1) * 18
    text = "".join(
        f'<text x="160" y="{first_y + index * 36}" text-anchor="middle">'
        f"{html.escape(line)}</text>"
        for index, line in enumerate(lines)
    )
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" width="320" height="320" '
        'viewBox="0 0 320 320">'
        "<defs>"
        '<linearGradient id="bg" x1="0" y1="0" x2="1" y2="1">'
        '<stop stop-color="#101820"/><stop offset="1" stop-color="#07110b"/>'
        "</linearGradient>"
        '<radialGradient id="glow"><stop stop-color="#53fc18" stop-opacity=".32"/>'
        '<stop offset="1" stop-color="#53fc18" stop-opacity="0"/></radialGradient>'
        "</defs>"
        '<rect width="320" height="320" rx="44" fill="url(#bg)"/>'
        '<circle cx="250" cy="54" r="150" fill="url(#glow)"/>'
        '<rect x="30" y="30" width="260" height="260" rx="34" fill="none" '
        'stroke="#53fc18" stroke-opacity=".34" stroke-width="2"/>'
        '<g fill="none" stroke="#53fc18" stroke-width="5" '
        'stroke-linecap="round" stroke-linejoin="round">'
        '<path d="M119 55h82v62l-41 23-41-23z"/>'
        '<path d="m119 78 41 23 41-23M160 101v39"/>'
        "</g>"
        '<g fill="#f4f7f5" font-family="Arial,sans-serif" font-size="20" '
        f'font-weight="700">{text}</g>'
        '<text x="160" y="270" text-anchor="middle" fill="#8f9b95" '
        'font-family="Arial,sans-serif" font-size="11" letter-spacing="2">'
        "GÖRSEL BEKLENİYOR</text></svg>"
    ).encode("utf-8")


@app.get("/drops/reward-image/{filename}", include_in_schema=False)
def reward_image(filename: str):
    if not re.fullmatch(r"[A-Za-z0-9._-]{10,180}", filename):
        raise HTTPException(status_code=404, detail="Görsel bulunamadı.")
    cache_dir = Path(DATA_DIR) / "reward_images"
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / filename
    if cache_path.is_file() and cache_path.stat().st_size > 0:
        return FileResponse(
            cache_path,
            media_type="image/png",
            headers={"Cache-Control": "public, max-age=86400"},
        )

    relative_path = f"drops/reward-image/{filename}"
    candidates = (
        f"https://files.kick.com/{relative_path}",
        f"https://files.kick.com/images/{relative_path}",
    )
    headers = {
        "Accept": "image/avif,image/webp,image/png,image/*,*/*;q=0.8",
        "Referer": "https://kick.com/",
        "User-Agent": os.environ.get("KDM_USER_AGENT")
        or "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "Chrome/149.0.0.0 Safari/537.36",
    }
    for url in candidates:
        try:
            request = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(request, timeout=12) as upstream:
                content_type = upstream.headers.get_content_type()
                content_length = int(upstream.headers.get("Content-Length") or 0)
                if not content_type.startswith("image/") or content_length > 5_000_000:
                    continue
                body = upstream.read(5_000_001)
                if not body or len(body) > 5_000_000:
                    continue
            cache_path.write_bytes(body)
            return Response(
                body,
                media_type=content_type,
                headers={"Cache-Control": "public, max-age=86400"},
            )
        except (OSError, ValueError, urllib.error.URLError):
            continue

    return Response(
        _reward_placeholder(filename),
        media_type="image/svg+xml",
        headers={"Cache-Control": "public, max-age=300"},
    )


@app.get("/")
def home(request: Request):
    if not _session_user(request):
        return FileResponse(ROOT / "web" / "login.html")
    return FileResponse(ROOT / "web" / "index.html")


@app.post("/api/register")
def register(body: RegisterBody, request: Request):
    if not REGISTRATION_ENABLED:
        raise HTTPException(status_code=403, detail="Yeni hesap oluşturma kapalı.")
    ip = _client_ip(request)
    if _rate_limited(registration_attempts, ip, 3, 3600):
        raise HTTPException(
            status_code=429,
            detail="Çok fazla kayıt denemesi. Daha sonra tekrar deneyin.",
        )
    username = body.username.strip()
    email = body.email.strip().lower()
    if not re.fullmatch(r"[A-Za-z0-9_.-]{3,32}", username):
        raise ValueError(
            "Kullanıcı adı yalnız harf, rakam, nokta, alt çizgi ve tire içerebilir."
        )
    if email and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        raise ValueError("Geçerli bir e-posta adresi girin.")
    user = accounts.create_user(username, email, body.password)
    csrf = secrets.token_urlsafe(32)
    request.session.clear()
    request.session.update(
        {
            "user_id": user["id"],
            "session_version": user["session_version"],
            "csrf": csrf,
        }
    )
    return {"ok": True}


@app.post("/api/login")
def login(body: LoginBody, request: Request):
    ip = _client_ip(request)
    key = f"{ip}:{body.username.casefold()}"
    if _rate_limited(login_attempts, key, 5, 300):
        raise HTTPException(
            status_code=429,
            detail="Çok fazla hatalı deneme. Beş dakika sonra tekrar deneyin.",
        )
    user = accounts.authenticate(body.username.strip(), body.password, ip)
    if not user:
        time.sleep(0.35)
        raise HTTPException(status_code=401, detail="Kullanıcı adı veya şifre yanlış.")
    login_attempts[key].clear()
    csrf = secrets.token_urlsafe(32)
    request.session.clear()
    request.session.update(
        {
            "user_id": user["id"],
            "session_version": user["session_version"],
            "csrf": csrf,
        }
    )
    return {"ok": True}


@app.post("/api/logout")
def logout(request: Request, _user=Depends(require_csrf)):
    request.session.clear()
    return {"ok": True}


@app.get("/api/state")
def state(request: Request, user=Depends(require_auth)):
    result = services.get(user).snapshot()
    result["csrf"] = request.session.get("csrf")
    result["user"] = {
        "id": user["id"],
        "username": user["username"],
        "email": user["email"],
        "role": user["role"],
    }
    result["limits"] = {"max_active_miners": MAX_ACTIVE_MINERS}
    return result


@app.post("/api/queue")
def add_stream(body: StreamBody, user=Depends(require_csrf)):
    return {
        "ok": True,
        "item": services.get(user).add_stream(body.url, body.minutes),
    }


@app.delete("/api/queue/{item_id}")
def remove_stream(item_id: str, user=Depends(require_csrf)):
    services.get(user).remove_stream(item_id)
    return {"ok": True}


@app.post("/api/queue/clear")
def clear_queue(user=Depends(require_csrf)):
    services.get(user).clear_queue()
    return {"ok": True}


@app.post("/api/miner/start")
def start_miner(body: StartBody, user=Depends(require_csrf)):
    services.start(user, body.item_id)
    return {"ok": True}


@app.post("/api/miner/stop")
def stop_miner(user=Depends(require_csrf)):
    services.get(user).stop()
    return {"ok": True}


@app.post("/api/inventory/refresh")
async def refresh_inventory(user=Depends(require_csrf)):
    inventory = await asyncio.to_thread(services.get(user).refresh_inventory)
    return {"ok": True, "inventory": inventory}


@app.post("/api/inventory/{campaign_id}/add")
async def add_campaign(campaign_id: str, user=Depends(require_csrf)):
    result = await asyncio.to_thread(
        services.get(user).add_campaign,
        campaign_id,
    )
    return {"ok": True, **result}


@app.post("/api/cookies")
def replace_cookies(body: CookieBody, user=Depends(require_csrf)):
    return {
        "ok": True,
        "cookie": services.get(user).replace_cookies(body.cookies),
    }

@app.post("/api/kick-login")
async def kick_login(body: KickLoginBody, user=Depends(require_csrf)):
    from core.login import perform_kick_login
    import asyncio
    
    result = await asyncio.to_thread(perform_kick_login, body.username, body.password)
    if result.get("success"):
        services.get(user).replace_cookies(result["cookies"])
        return {"success": True}
    else:
        return {"success": False, "error": result.get("error")}


@app.post("/api/logs/clear")
def clear_logs(user=Depends(require_csrf)):
    services.get(user).clear_logs()
    return {"ok": True}


@app.get("/api/admin/users")
def admin_users(_admin=Depends(require_admin)):
    users = []
    for user in accounts.list_users():
        users.append({**user, "runtime": services.user_runtime(user)})
    return {
        "ok": True,
        "users": users,
        "max_active_miners": MAX_ACTIVE_MINERS,
    }


@app.post("/api/admin/users/{user_id}/active")
def admin_set_active(
    user_id: str,
    body: ActiveBody,
    admin=Depends(require_admin),
):
    if user_id == admin["id"] and not body.active:
        raise ValueError("Kendi yönetici hesabınızı devre dışı bırakamazsınız.")
    if not body.active:
        services.stop_user(user_id)
    return {"ok": True, "user": accounts.set_active(user_id, body.active)}


@app.post("/api/admin/users/{user_id}/stop")
def admin_stop_user(user_id: str, _admin=Depends(require_admin)):
    services.stop_user(user_id)
    return {"ok": True}


@app.post("/api/admin/users/{user_id}/sessions/reset")
def admin_reset_sessions(
    user_id: str,
    admin=Depends(require_admin),
):
    if user_id == admin["id"]:
        raise ValueError("Aktif yönetici oturumu bu ekrandan sonlandırılamaz.")
    return {"ok": True, "user": accounts.invalidate_sessions(user_id)}
