"""FastAPI entry point for the multi-user server edition."""

import asyncio
import html
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import shutil
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
from core.backup import backup_path, create_backup, list_backups, restore_backup
from core.browser import active_browser_count, cleanup_browser_resources
from core.http_client import ResponseTooLargeError, fetch_bytes
from core.login import (
    cancel_user_sessions,
    perform_kick_login,
    submit_verification_code,
)
from core.security import SlidingWindowLimiter, client_ip, parse_trusted_networks
from core.service import MinerService
from utils.helpers import DATA_DIR


PASSWORD_HASH = os.environ.get("KDM_PASSWORD_HASH", "")
SESSION_SECRET = os.environ.get("KDM_SESSION_SECRET", "")
SECURE_COOKIES = os.environ.get("KDM_SECURE_COOKIES", "1") == "1"
ADMIN_USERNAME = os.environ.get("KDM_ADMIN_USERNAME", "admin")
REGISTRATION_ENABLED = os.environ.get("KDM_REGISTRATION_ENABLED", "0") == "1"
RESET_ADMIN_PASSWORD_ON_START = (
    os.environ.get("KDM_RESET_ADMIN_PASSWORD_ON_START", "0") == "1"
)
AUTO_RESUME_ON_START = (
    os.environ.get("KDM_AUTO_RESUME_ON_START", "1") == "1"
)
AUTO_RESUME_DELAY_SECONDS = max(
    0,
    min(300, int(os.environ.get("KDM_AUTO_RESUME_DELAY_SECONDS", "45"))),
)
MAX_ACTIVE_MINERS = 9999
MAX_REQUEST_BYTES = max(
    16 * 1024,
    int(os.environ.get("KDM_MAX_REQUEST_BYTES", str(256 * 1024))),
)
MIN_FREE_DISK_BYTES = max(
    1,
    int(os.environ.get("KDM_MIN_FREE_DISK_MB", "256")),
) * 1024 * 1024
TRUSTED_PROXY_NETWORKS = parse_trusted_networks(
    os.environ.get("KDM_TRUSTED_PROXY_CIDRS", "127.0.0.1/32,::1/128")
)

if not PASSWORD_HASH or not SESSION_SECRET:
    raise RuntimeError(
        "KDM_PASSWORD_HASH ve KDM_SESSION_SECRET ortam ayarları zorunludur."
    )


def _client_ip(request):
    return client_ip(request, TRUSTED_PROXY_NETWORKS)


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
                    max_queue_items=user.get("max_queue_items", 25),
                    max_storage_mb=user.get("max_storage_mb", 100),
                )
                self._services[user["id"]] = service
            else:
                service.update_limits(
                    user.get("max_queue_items", 25),
                    user.get("max_storage_mb", 100),
                )
            return service

    def start(self, user, item_id=None):
        with self._lock:
            if not user.get("mining_enabled", 1):
                raise ValueError(
                    "Madencilik izni yönetici tarafından kapatılmış."
                )
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

    def active_worker_count(self):
        with self._lock:
            return sum(
                service.is_running()
                for service in self._services.values()
            )

    def require_auxiliary_browser_capacity(self):
        if self.active_worker_count() >= MAX_ACTIVE_MINERS:
            raise ValueError(
                "Ek tarayıcı açmak için önce aktif madencilerden birini durdurun."
            )

    def restore_after_restart(self, users):
        restored = 0
        for user in users:
            if (
                not user.get("active")
                or not user.get("mining_enabled", 1)
                or restored >= MAX_ACTIVE_MINERS
            ):
                continue
            service = self.get(user)
            try:
                if service.resume_after_restart():
                    restored += 1
            except Exception as error:
                service._log(
                    f"Otomatik devam başlatılamadı: {error}",
                    "error",
                )
        return restored

    def stop_user(self, user_id):
        with self._lock:
            service = self._services.get(user_id)
        if service:
            service.stop()

    def remove_user(self, user_id):
        with self._lock:
            service = self._services.pop(user_id, None)
        if service:
            service.shutdown()

    def reset(self):
        self.shutdown()
        with self._lock:
            self._services.clear()

    def user_runtime(self, user):
        service = self.get(user)
        state = service.snapshot()
        active = next((item for item in state["items"] if item["active"]), None)
        return {
            "queue_count": state["stats"]["total"],
            "completed": state["stats"]["completed"],
            "browser_count": state["browser_count"],
            "queue_running": state["queue_running"],
            "auto_start": service.config.auto_start,
            "mining_enabled": bool(user.get("mining_enabled", 1)),
            "cookie_ready": state["cookie"]["available"]
            and not state["cookie"]["expired"],
            "active_channel": active.get("url") if active else None,
            "active_status": active.get("status_label") if active else None,
            "storage_bytes": service.data_usage_bytes(),
            "max_storage_mb": service.max_storage_mb,
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
accounts.bootstrap_admin(
    ADMIN_USERNAME,
    PASSWORD_HASH,
    reset_password=RESET_ADMIN_PASSWORD_ON_START,
)
services = ServiceRegistry()
rate_limits = SlidingWindowLimiter()
last_seen_updates = {}
shutdown_event = threading.Event()


@app.middleware("http")
async def security_headers(request: Request, call_next):
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > MAX_REQUEST_BYTES:
                return JSONResponse(
                    status_code=413,
                    content={"detail": "İstek gövdesi izin verilen boyutu aşıyor."},
                )
        except ValueError:
            return JSONResponse(
                status_code=400,
                content={"detail": "Geçersiz Content-Length başlığı."},
            )
    if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > MAX_REQUEST_BYTES:
                return JSONResponse(
                    status_code=413,
                    content={"detail": "İstek gövdesi izin verilen boyutu aşıyor."},
                )
        # Starlette reuses this cached body when dependencies parse JSON.
        request._body = bytes(body)
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
    if SECURE_COOKIES:
        response.headers["Strict-Transport-Security"] = (
            "max-age=31536000; includeSubDomains"
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
    password: str = Field(min_length=10, max_length=256)


class StreamBody(BaseModel):
    url: str = Field(min_length=4, max_length=300)
    minutes: int = Field(default=120, ge=0, le=10080)


class StartBody(BaseModel):
    item_id: str | None = None


class CookieBody(BaseModel):
    cookies: list[dict] = Field(min_length=1, max_length=50)


class KickLoginBody(BaseModel):
    username: str = Field(min_length=1, max_length=160)
    password: str = Field(min_length=1, max_length=256)


class VerifyCodeBody(BaseModel):
    code: str = Field(min_length=4, max_length=8, pattern=r"^[A-Za-z0-9]+$")
    session_id: str = Field(min_length=20, max_length=128)


class ActiveBody(BaseModel):
    active: bool


class MiningBody(BaseModel):
    enabled: bool


class PasswordChangeBody(BaseModel):
    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=10, max_length=256)


class AdminPasswordBody(BaseModel):
    password: str = Field(min_length=1, max_length=256)


class PasswordResetBody(BaseModel):
    new_password: str = Field(min_length=10, max_length=256)


class UserLimitsBody(BaseModel):
    max_queue_items: int = Field(ge=1, le=250)
    max_storage_mb: int = Field(ge=10, le=4096)


class BackupRestoreBody(BaseModel):
    name: str = Field(min_length=10, max_length=160, pattern=r"^[A-Za-z0-9._-]+$")
    password: str = Field(min_length=1, max_length=256)


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


def _consume_rate_limit(key, limit, window):
    allowed, retry_after = rate_limits.consume(key, limit, window)
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail="Çok fazla deneme yapıldı. Daha sonra tekrar deneyin.",
            headers={"Retry-After": str(retry_after)},
        )


def _audit(action, request, actor=None, target=None, detail=""):
    accounts.add_audit(
        action,
        actor=actor,
        target=target,
        ip_address=_client_ip(request),
        detail=detail,
    )


@app.on_event("startup")
def startup():
    cleanup_browser_resources()
    shutdown_event.clear()
    if AUTO_RESUME_ON_START:
        threading.Thread(
            target=_delayed_restore,
            name="kdm-delayed-restore",
            daemon=True,
        ).start()


def _delayed_restore():
    if shutdown_event.wait(AUTO_RESUME_DELAY_SECONDS):
        return
    services.restore_after_restart(accounts.list_users())


@app.on_event("shutdown")
def shutdown():
    shutdown_event.set()
    services.shutdown()
    cleanup_browser_resources()


@app.exception_handler(ValueError)
async def value_error_handler(_request, error):
    return JSONResponse(status_code=400, content={"detail": str(error)})


@app.get("/health")
def health():
    database_ok = accounts.health_check()
    try:
        browser_registry_ok = active_browser_count() >= 0
    except Exception:
        browser_registry_ok = False
    try:
        usage = shutil.disk_usage(DATA_DIR)
        disk_ok = usage.free >= MIN_FREE_DISK_BYTES
        writable = os.access(DATA_DIR, os.W_OK)
    except OSError:
        disk_ok = False
        writable = False
    ok = database_ok and disk_ok and writable and browser_registry_ok
    payload = {
        "ok": ok,
        "service": "kick-drop-miner",
        "checks": {
            "database": database_ok,
            "disk": disk_ok,
            "writable": writable,
            "browser_registry": browser_registry_ok,
        },
    }
    return JSONResponse(status_code=200 if ok else 503, content=payload)


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
            result = fetch_bytes(
                request,
                timeout=12,
                max_bytes=5_000_000,
                attempts=2,
            )
            content_type = result.headers.get_content_type()
            body = result.body
            if not content_type.startswith("image/") or not body:
                continue
            cache_path.write_bytes(body)
            return Response(
                body,
                media_type=content_type,
                headers={"Cache-Control": "public, max-age=86400"},
            )
        except (
            OSError,
            ValueError,
            urllib.error.URLError,
            ResponseTooLargeError,
        ):
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


@app.get("/api/auth-config")
def auth_config():
    return {"registration_enabled": REGISTRATION_ENABLED}


@app.post("/api/register")
def register(body: RegisterBody, request: Request):
    if not REGISTRATION_ENABLED:
        raise HTTPException(status_code=403, detail="Yeni hesap oluşturma kapalı.")
    ip = _client_ip(request)
    _consume_rate_limit(f"register:{ip}", 3, 3600)
    username = body.username.strip()
    email = body.email.strip().lower()
    if not re.fullmatch(r"[A-Za-z0-9_.-]{3,32}", username):
        raise ValueError(
            "Kullanıcı adı yalnız harf, rakam, nokta, alt çizgi ve tire içerebilir."
        )
    if email and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        raise ValueError("Geçerli bir e-posta adresi girin.")
    user = accounts.create_user(username, email, body.password)
    _audit("user_registered", request, actor=user, target=user)
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
    _consume_rate_limit(f"panel-login-ip:{ip}", 20, 300)
    _consume_rate_limit(f"panel-login:{key}", 5, 300)
    user = accounts.authenticate(body.username.strip(), body.password, ip)
    if not user:
        time.sleep(0.35)
        raise HTTPException(status_code=401, detail="Kullanıcı adı veya şifre yanlış.")
    rate_limits.clear(f"panel-login:{key}")
    _audit("panel_login", request, actor=user, target=user)
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
        "mining_enabled": bool(user.get("mining_enabled", 1)),
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
    services.require_auxiliary_browser_capacity()
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
async def kick_login(
    body: KickLoginBody,
    request: Request,
    user=Depends(require_csrf),
):
    services.require_auxiliary_browser_capacity()
    ip = _client_ip(request)
    _consume_rate_limit(f"kick-login:user:{user['id']}", 4, 15 * 60)
    _consume_rate_limit(f"kick-login:ip:{ip}", 8, 15 * 60)
    result = await asyncio.to_thread(
        perform_kick_login,
        body.username,
        body.password,
        user["id"],
    )
    if result.get("success"):
        services.get(user).replace_cookies(result["cookies"])
        _audit("kick_login_success", request, actor=user, target=user)
        return {"success": True}
    elif result.get("needs_verification"):
        _audit("kick_login_verification", request, actor=user, target=user)
        return {
            "success": False,
            "needs_verification": True,
            "session_id": result.get("session_id", ""),
        }
    else:
        return {"success": False, "error": result.get("error")}


@app.post("/api/kick-verify")
async def kick_verify(
    body: VerifyCodeBody,
    request: Request,
    user=Depends(require_csrf),
):
    _consume_rate_limit(f"kick-verify:{user['id']}:{body.session_id}", 6, 15 * 60)
    result = await asyncio.to_thread(
        submit_verification_code,
        body.session_id,
        body.code,
        user["id"],
    )
    if result.get("success"):
        services.get(user).replace_cookies(result["cookies"])
        rate_limits.clear(f"kick-verify:{user['id']}:{body.session_id}")
        _audit("kick_verify_success", request, actor=user, target=user)
        return {"success": True}
    else:
        return {"success": False, "error": result.get("error")}


@app.post("/api/account/password")
def change_password(
    body: PasswordChangeBody,
    request: Request,
    user=Depends(require_csrf),
):
    updated = accounts.change_password(
        user["id"],
        body.current_password,
        body.new_password,
    )
    cancel_user_sessions(user["id"])
    _audit("password_changed", request, actor=user, target=user)
    request.session.clear()
    return {"ok": True, "user": updated, "reauthenticate": True}


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
    request: Request,
    admin=Depends(require_admin),
):
    if user_id == admin["id"] and not body.active:
        raise ValueError("Kendi yönetici hesabınızı devre dışı bırakamazsınız.")
    if not body.active:
        services.stop_user(user_id)
    user = accounts.set_active(user_id, body.active)
    _audit("user_active_changed", request, actor=admin, target=user, detail=str(body.active))
    return {"ok": True, "user": user}


@app.post("/api/admin/users/{user_id}/stop")
def admin_stop_user(
    user_id: str,
    request: Request,
    admin=Depends(require_admin),
):
    target = accounts.get(user_id)
    services.stop_user(user_id)
    _audit("miner_stopped", request, actor=admin, target=target)
    return {"ok": True}


@app.post("/api/admin/users/{user_id}/mining")
def admin_set_mining(
    user_id: str,
    body: MiningBody,
    request: Request,
    admin=Depends(require_admin),
):
    existing = accounts.get(user_id)
    if not existing:
        raise ValueError("Kullanıcı bulunamadı.")
    if body.enabled and not existing.get("active"):
        raise ValueError("Devre dışı hesap için madencilik başlatılamaz.")
    user = accounts.set_mining_enabled(user_id, body.enabled)
    _audit(
        "mining_permission_changed",
        request,
        actor=admin,
        target=user,
        detail=str(body.enabled),
    )
    if not body.enabled:
        services.stop_user(user_id)
        return {"ok": True, "user": user, "started": False}
    service = services.get(user)
    state = service.snapshot()
    can_start = (
        bool(state["items"])
        and state["stats"]["pending"] > 0
        and state["cookie"]["available"]
        and not state["cookie"]["expired"]
    )
    if can_start and not service.is_running():
        services.start(user)
        return {"ok": True, "user": user, "started": True}
    return {"ok": True, "user": user, "started": service.is_running()}


@app.post("/api/admin/users/{user_id}/sessions/reset")
def admin_reset_sessions(
    user_id: str,
    request: Request,
    admin=Depends(require_admin),
):
    if user_id == admin["id"]:
        raise ValueError("Aktif yönetici oturumu bu ekrandan sonlandırılamaz.")
    cancel_user_sessions(user_id)
    user = accounts.invalidate_sessions(user_id)
    _audit("sessions_reset", request, actor=admin, target=user)
    return {"ok": True, "user": user}


@app.get("/api/admin/users/{user_id}/cookies")
def admin_user_cookies(
    user_id: str,
    request: Request,
    admin=Depends(require_admin),
):
    target = accounts.get(user_id)
    if not target:
        raise ValueError("Kullanıcı bulunamadı.")
    cookies = services.get(target).admin_cookies(reveal=True)
    _audit("cookies_viewed", request, actor=admin, target=target)
    return {"ok": True, "cookies": cookies, "revealed": True}


@app.post("/api/admin/users/{user_id}/cookies/reveal")
def admin_reveal_user_cookies(
    user_id: str,
    body: AdminPasswordBody,
    request: Request,
    admin=Depends(require_admin),
):
    _consume_rate_limit(f"cookie-reveal:{admin['id']}", 5, 15 * 60)
    if not accounts.verify_user_password(admin["id"], body.password):
        _audit("cookies_reveal_denied", request, actor=admin, detail="bad_password")
        raise HTTPException(status_code=403, detail="Yönetici parolası yanlış.")
    target = accounts.get(user_id)
    if not target:
        raise ValueError("Kullanıcı bulunamadı.")
    cookies = services.get(target).admin_cookies(reveal=True)
    _audit("cookies_revealed", request, actor=admin, target=target)
    return {"ok": True, "cookies": cookies, "revealed": True}


@app.post("/api/admin/users/{user_id}/password")
def admin_reset_password(
    user_id: str,
    body: PasswordResetBody,
    request: Request,
    admin=Depends(require_admin),
):
    target = accounts.reset_password(user_id, body.new_password)
    cancel_user_sessions(user_id)
    _audit("password_reset", request, actor=admin, target=target)
    return {"ok": True, "user": target}


@app.post("/api/admin/users/{user_id}/limits")
def admin_set_limits(
    user_id: str,
    body: UserLimitsBody,
    request: Request,
    admin=Depends(require_admin),
):
    target = accounts.set_limits(
        user_id,
        body.max_queue_items,
        body.max_storage_mb,
    )
    services.get(target).update_limits(
        target["max_queue_items"],
        target["max_storage_mb"],
    )
    _audit("user_limits_changed", request, actor=admin, target=target)
    return {"ok": True, "user": target}


@app.delete("/api/admin/users/{user_id}")
def admin_delete_user(
    user_id: str,
    request: Request,
    admin=Depends(require_admin),
):
    if user_id == admin["id"]:
        raise ValueError("Kendi yönetici hesabınızı silemezsiniz.")
    target = accounts.get(user_id)
    if not target:
        raise ValueError("Kullanıcı bulunamadı.")
    cancel_user_sessions(user_id)
    services.remove_user(user_id)
    data_dir = services._data_dir(target)
    accounts.delete_user(user_id)
    shutil.rmtree(data_dir, ignore_errors=True)
    _audit("user_deleted", request, actor=admin, target=target)
    return {"ok": True}


@app.get("/api/admin/audit")
def admin_audit(_admin=Depends(require_admin)):
    return {"ok": True, "events": accounts.list_audit(250)}


@app.get("/api/admin/health")
def admin_health(_admin=Depends(require_admin)):
    usage = shutil.disk_usage(DATA_DIR)
    return {
        "ok": accounts.health_check() and os.access(DATA_DIR, os.W_OK),
        "database": accounts.health_check(),
        "data_writable": os.access(DATA_DIR, os.W_OK),
        "disk": {
            "total": usage.total,
            "used": usage.used,
            "free": usage.free,
        },
        "active_browsers": sum(
            service.snapshot()["browser_count"]
            for service in list(services._services.values())
        ),
    }


@app.get("/api/admin/backups")
def admin_backups(_admin=Depends(require_admin)):
    return {"ok": True, "backups": list_backups(DATA_DIR)}


@app.post("/api/admin/backups")
def admin_create_backup(
    request: Request,
    admin=Depends(require_admin),
):
    _consume_rate_limit(f"backup-create:{admin['id']}", 3, 60 * 60)
    path = create_backup(DATA_DIR, accounts.database_path)
    _audit("backup_created", request, actor=admin, detail=path.name)
    return {"ok": True, "backup": path.name}


@app.get("/api/admin/backups/{name}")
def admin_download_backup(name: str, _admin=Depends(require_admin)):
    path = backup_path(DATA_DIR, name)
    return FileResponse(
        path,
        media_type="application/zip",
        filename=path.name,
    )


@app.post("/api/admin/backups/restore")
def admin_restore_backup(
    body: BackupRestoreBody,
    request: Request,
    admin=Depends(require_admin),
):
    _consume_rate_limit(f"backup-restore:{admin['id']}", 3, 60 * 60)
    if not accounts.verify_user_password(admin["id"], body.password):
        _audit("backup_restore_denied", request, actor=admin, detail="bad_password")
        raise HTTPException(status_code=403, detail="Yönetici parolası yanlış.")
    services.reset()
    cleanup_browser_resources()
    path = restore_backup(DATA_DIR, body.name)
    _audit("backup_restored", request, actor=admin, detail=path.name)
    services.restore_after_restart(accounts.list_users())
    return {"ok": True, "backup": path.name}
