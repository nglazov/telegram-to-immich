import asyncio
import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from contextlib import asynccontextmanager, suppress
from datetime import date, datetime
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from telethon import errors

from .calendar import day_map
from .config import ENV_FILE, Config, migrate_credentials
from .store import Store
from .worker import MOSCOW, Worker, safe_error

STATIC = Path(__file__).parent / "static"


class Login(BaseModel):
    password: str = Field(max_length=512)


class Phone(BaseModel):
    phone: str = Field(pattern=r"^\+[0-9]{7,15}$")


class Code(BaseModel):
    code: str = Field(pattern=r"^[0-9\s-]{3,12}$")


class Password(BaseModel):
    password: str = Field(min_length=1, max_length=512)


class ChatSelection(BaseModel):
    id: int
    selected: bool
    since: date
    until: date | None = None
    topic_ids: list[int] | None = Field(default=None, max_length=2000)


class Schedule(BaseModel):
    enabled: bool
    interval: int = Field(ge=5, le=1440)
    mode: Literal["preview", "download", "import"] = "preview"


class Run(BaseModel):
    mode: Literal["preview", "download", "import"] = "preview"


def make_app(state_dir=None, photos_dir=None, password=None, config=None, env_file=ENV_FILE):
    @asynccontextmanager
    async def lifespan(app):
        os.umask(0o077)
        state = Path(state_dir or os.environ.get("STATE_DIR", "state")).resolve()
        photos = Path(photos_dir or os.environ.get("PHOTOS_DIR", "photos")).resolve()
        state.mkdir(parents=True, exist_ok=True, mode=0o700)
        store = Store(state)
        migrate_credentials(store, env_file)
        app.state.config = config or Config.load(env_file)
        app_password = password or app.state.config.app_password
        if len(app_password) < 12:
            store.db.close()
            raise RuntimeError("Set APP_PASSWORD to at least 12 characters.")
        app.state.signing_key = hashlib.sha256(("telegram-photos/session/" + app_password).encode()).digest()
        app.state.password_digest = hashlib.sha256(app_password.encode()).digest()
        app.state.failures = []
        app.state.store = store
        app.state.worker = Worker(store, state, photos, app.state.config)
        # Old installations have no explicit schedule mode. Never inherit their
        # automatic download behaviour into the new preview-first version.
        if store.get("schedule_mode") is None:
            store.set("auto_sync", False)
            store.set("schedule_mode", "preview")
        scheduler = asyncio.create_task(app.state.worker.scheduler())
        try:
            yield
        finally:
            scheduler.cancel()
            with suppress(asyncio.CancelledError):
                await scheduler
            await app.state.worker.close()
            store.db.close()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    def session_valid(token):
        try:
            payload, signature = token.rsplit(".", 1)
            expected = hmac.new(app.state.signing_key, payload.encode(), hashlib.sha256).hexdigest()
            issued = int(payload.split(":")[0])
            return (
                hmac.compare_digest(signature, expected)
                and 0 <= time.time() - issued < 12 * 3600
            )
        except (ValueError, AttributeError):
            return False

    @app.middleware("http")
    async def gate(request: Request, call_next):
        public = request.url.path in {"/", "/health", "/api/login"} or request.url.path.startswith("/static/")
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            expected = app.state.config.app_origin or str(request.base_url).rstrip("/")
            if request.headers.get("x-requested-with") != "telegram-photos" or (origin and origin != expected):
                return JSONResponse({"detail": "Запрос отклонён. Откройте приложение заново."}, 403)
        if not public and not session_valid(request.cookies.get("photos_session", "")):
            return JSONResponse({"detail": "Войдите в приложение."}, 401)
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            async with app.state.worker.control_lock:
                response = await call_next(request)
        else:
            response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers.setdefault("Content-Security-Policy", (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; "
            "base-uri 'none'; form-action 'self'"
        ))
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        # Pydantic errors can echo submitted credentials. Never return the input.
        return JSONResponse({"detail": "Проверьте формат заполненных полей."}, 422)

    @app.exception_handler(ValueError)
    @app.exception_handler(OSError)
    @app.exception_handler(httpx.HTTPError)
    @app.exception_handler(errors.RPCError)
    async def operation_error(request, exc):
        return JSONResponse({"detail": safe_error(exc)}, 400)

    @app.exception_handler(Exception)
    async def unexpected_error(request, exc):
        return JSONResponse({"detail": safe_error(exc)}, 500)

    def idle():
        if app.state.worker.busy:
            raise HTTPException(409, "Остановите текущую синхронизацию и дождитесь её завершения.")

    async def check_telegram_identity(client):
        me = await client.get_me()
        known = app.state.store.get("telegram_user_id")
        if known and known != me.id:
            await client.log_out()
            app.state.worker.client = None
            raise ValueError("Эта установка привязана к другому Telegram-аккаунту. Нужна отдельная установка.")
        app.state.store.set("telegram_user_id", me.id)
        app.state.store.set("telegram_connected", True)
        app.state.worker.phone = app.state.worker.phone_code_hash = None

    @app.get("/")
    async def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.post("/api/login")
    async def login(body: Login, request: Request):
        now = time.time()
        app.state.failures = [t for t in app.state.failures if now - t < 900]
        if len(app.state.failures) >= 10:
            raise HTTPException(429, "Слишком много попыток входа. Подождите 15 минут.")
        if not hmac.compare_digest(
            hashlib.sha256(body.password.encode()).digest(), app.state.password_digest
        ):
            app.state.failures.append(now)
            raise HTTPException(401, "Неверный пароль.")
        app.state.failures.clear()
        payload = f"{int(now)}:{base64.urlsafe_b64encode(secrets.token_bytes(24)).decode()}"
        signature = hmac.new(app.state.signing_key, payload.encode(), hashlib.sha256).hexdigest()
        response = JSONResponse({"ok": True})
        response.set_cookie(
            "photos_session", f"{payload}.{signature}", httponly=True, samesite="strict",
            secure=urlsplit(app.state.config.app_origin or str(request.url)).scheme == "https",
            max_age=12 * 3600,
        )
        return response

    @app.post("/api/logout")
    async def logout():
        response = JSONResponse({"ok": True})
        response.delete_cookie("photos_session")
        return response

    @app.get("/api/status")
    async def status():
        store, worker = app.state.store, app.state.worker
        return {
            "busy": worker.busy, "phase": worker.phase, "error": worker.error,
            "telegram_retry_after": store.get("telegram_retry_after", 0),
            "telegram_wait": store.get("telegram_wait"),
            "telegram_download_delay": worker.config.telegram_download_delay,
            "deferred_count": store.one("SELECT COUNT(*) n FROM deferred")["n"],
            "takeout_retry_after": store.get("takeout_retry_after", 0),
            "takeout_wait": store.get("takeout_wait"),
            "takeout_error": store.get("takeout_error"),
            "telegram_premium": store.get("telegram_premium"),
            "current_transfer": worker.current,
            "takeout_active": worker.takeout_active,
            "telegram_takeout": worker.config.telegram_takeout,
            "last_finished": worker.last_finished,
            "telegram_configured": worker.config.telegram_configured,
            "telegram_connected": store.get("telegram_connected", False),
            "immich_configured": worker.config.immich_configured,
            "immich_url": worker.config.immich_url,
            "immich_public_url": worker.config.immich_public_url or worker.config.immich_url.removesuffix("/api"),
            "immich_account": store.get("immich_account") if worker.immich_verified else None,
            "default_mode": "preview" if worker.config.dry_run else "download",
            "mode": worker.mode, "report": worker.report,
            "schedule_mode": store.get("schedule_mode", "preview"),
            "photos_path": str(worker.photos),
            "auto_sync": store.get("auto_sync", False), "interval": store.get("interval", 30),
            "stats": store.one("""
                SELECT COUNT(*) AS downloaded,
                  COUNT(asset_id) AS uploaded,
                  SUM(CASE WHEN error IS NOT NULL THEN 1 ELSE 0 END) AS errors FROM media
            """),
            "pending_albums": store.one(
                "SELECT COUNT(*) AS count FROM sources WHERE album_added=0"
            )["count"],
        }

    @app.post("/api/config/reload")
    async def reload_config():
        idle()
        worker, store = app.state.worker, app.state.store
        updated = Config.load(env_file)
        if updated.app_password != app.state.config.app_password or updated.app_origin != app.state.config.app_origin:
            raise ValueError("После изменения APP_PASSWORD или APP_ORIGIN перезапустите приложение.")
        async with worker.auth_lock:
            if worker.client and (
                updated.telegram_api_id != worker.config.telegram_api_id
                or updated.telegram_api_hash != worker.config.telegram_api_hash
            ):
                await worker.client.disconnect()
                worker.client = None
                store.set("telegram_connected", False)
            app.state.config = worker.config = updated
            worker.immich_verified = False
            store.set("auto_sync", False)
            return {"ok": True}

    @app.post("/api/immich/check")
    async def immich_check():
        idle()
        return {"ok": True, "account": await app.state.worker.check_immich()}

    @app.post("/api/telegram/send-code")
    async def send_code(body: Phone):
        idle()
        worker = app.state.worker
        async with worker.auth_lock:
            client = await worker.telegram()
            if await client.is_user_authorized():
                await check_telegram_identity(client)
                return {"status": "connected"}
            result = await client.send_code_request(body.phone)
            worker.phone, worker.phone_code_hash = body.phone, result.phone_code_hash
            return {"status": "code"}

    @app.post("/api/telegram/code")
    async def sign_in(body: Code):
        idle()
        worker = app.state.worker
        async with worker.auth_lock:
            if not worker.phone or not worker.phone_code_hash:
                raise ValueError("Запросите новый код входа.")
            client = await worker.telegram()
            try:
                await client.sign_in(
                    phone=worker.phone, code=body.code.replace(" ", "").replace("-", ""),
                    phone_code_hash=worker.phone_code_hash,
                )
            except errors.SessionPasswordNeededError:
                return {"status": "password"}
            await check_telegram_identity(client)
            return {"status": "connected"}

    @app.post("/api/telegram/password")
    async def two_factor(body: Password):
        idle()
        worker = app.state.worker
        async with worker.auth_lock:
            client = await worker.telegram()
            await client.sign_in(password=body.password)
            await check_telegram_identity(client)
            return {"status": "connected"}

    @app.post("/api/telegram/disconnect")
    async def disconnect():
        idle()
        worker = app.state.worker
        async with worker.auth_lock:
            client = await worker.telegram()
            await client.log_out()
            worker.client = None
            worker.phone = worker.phone_code_hash = None
            app.state.store.set("telegram_connected", False)
            app.state.store.set("auto_sync", False)
            return {"ok": True}

    @app.post("/api/chats/refresh")
    async def refresh_chats():
        idle()
        worker, store = app.state.worker, app.state.store
        async with worker.auth_lock:
            client = await worker.authorized()
            await check_telegram_identity(client)
            async for dialog in client.iter_dialogs():
                if not dialog.is_group and not dialog.is_channel:
                    continue
                if getattr(dialog.entity, "noforwards", False):
                    continue
                store.execute("""
                    INSERT INTO chats(id,title,since,is_forum) VALUES(?,?,?,?)
                    ON CONFLICT(id) DO UPDATE SET title=excluded.title,is_forum=excluded.is_forum
                """, (
                    dialog.id, dialog.title or str(dialog.id),
                    datetime.now(MOSCOW).date().replace(day=1).isoformat(),
                    bool(getattr(dialog.entity, "forum", False)),
                ))
                if getattr(dialog.entity, "forum", False):
                    try:
                        topics = await worker.fetch_topics(client, dialog.entity)
                        store.execute(
                            "UPDATE chats SET topics=?,topics_error=NULL WHERE id=?",
                            (json.dumps(topics, ensure_ascii=False), dialog.id),
                        )
                    except errors.FloodWaitError:
                        raise
                    except Exception as exc:
                        store.execute(
                            "UPDATE chats SET topics_error=? WHERE id=?", (safe_error(exc), dialog.id)
                        )
        return {"ok": True}

    @app.get("/api/chats")
    async def chats():
        rows = app.state.store.rows("""
            SELECT c.*, (SELECT COUNT(*) FROM sources s WHERE s.chat_id=c.id) AS photos
            FROM chats c ORDER BY c.selected DESC, c.title
        """)
        for row in rows:
            row["topics"] = json.loads(row["topics"])
            row["topic_ids"] = json.loads(row["topic_ids"]) if row["topic_ids"] is not None else None
        return rows

    @app.put("/api/chats")
    async def select_chats(body: list[ChatSelection]):
        idle()
        if len(body) > 2000:
            raise ValueError("Слишком много чатов в одном запросе.")
        store = app.state.store
        for item in body:
            if item.until and item.until < item.since:
                raise ValueError("Конец периода должен быть не раньше начала.")
            chat = store.one("SELECT * FROM chats WHERE id=?", (item.id,))
            if not chat:
                raise ValueError("Обновите список чатов.")
            if item.topic_ids is not None:
                if item.selected and not item.topic_ids:
                    raise ValueError("Выберите хотя бы одну тему или всю группу.")
                available = {topic["id"] for topic in json.loads(chat["topics"])}
                # Allow an unavailable old topic to remain deselected without widening the scope.
                previous = set(json.loads(chat["topic_ids"] or "[]"))
                allowed = available if item.selected else available | previous
                if (item.selected and not chat["is_forum"]) or not set(item.topic_ids) <= allowed:
                    raise ValueError("Выбранная тема недоступна. Обновите список и проверьте выбор.")
        with store.db:
            for item in body:
                old = store.one("SELECT * FROM chats WHERE id=?", (item.id,))
                since, until = item.since.isoformat(), item.until.isoformat() if item.until else None
                topic_ids = json.dumps(sorted(set(item.topic_ids))) if item.topic_ids is not None else None
                changed = old["since"] != since or old["until_date"] != until or old["topic_ids"] != topic_ids
                cursor = 0 if changed else old["cursor"]
                store.db.execute(
                    "UPDATE chats SET selected=?,since=?,until_date=?,cursor=?,topic_ids=? WHERE id=?",
                    (item.selected, since, until, cursor, topic_ids, item.id),
                )
                if changed:
                    store.db.execute(
                        "UPDATE chats SET scan_start_day=NULL,scanned_before=NULL,checked_at=NULL WHERE id=?",
                        (item.id,),
                    )
        return {"ok": True}

    @app.post("/api/schedule")
    async def schedule(body: Schedule):
        if body.enabled:
            if not app.state.store.get("telegram_connected"):
                raise ValueError("Сначала подключите Telegram.")
            if body.mode == "import" and not app.state.worker.config.immich_configured:
                raise ValueError("Настройте Immich или выберите локальный режим.")
            if not app.state.store.one("SELECT 1 FROM chats WHERE selected=1"):
                raise ValueError("Выберите хотя бы один чат.")
        app.state.store.set("auto_sync", body.enabled)
        app.state.store.set("interval", body.interval)
        app.state.store.set("schedule_mode", body.mode)
        return {"ok": True}

    @app.post("/api/sync")
    async def sync(body: Run):
        store, worker = app.state.store, app.state.worker
        if worker.auth_lock.locked():
            raise HTTPException(409, "Дождитесь завершения операции Telegram.")
        if not store.get("telegram_connected"):
            raise ValueError("Сначала подключите Telegram.")
        if not store.one("SELECT 1 FROM chats WHERE selected=1"):
            raise ValueError("Выберите хотя бы один чат.")
        worker.start(body.mode)
        return {"ok": True, "mode": body.mode}

    @app.post("/api/stop")
    async def stop():
        app.state.worker.stop = True
        app.state.store.set("auto_sync", False)
        return {"ok": True}

    @app.get("/api/recent")
    async def recent():
        return app.state.store.rows("""
            SELECT s.message_id, s.part, s.kind, s.grouped_id, s.sent_at, c.title,
                   m.id AS media_id, m.asset_id, m.error
            FROM sources s JOIN chats c ON c.id=s.chat_id JOIN media m ON m.id=s.media_id
            ORDER BY s.sent_at DESC LIMIT 50
        """)

    @app.get("/api/calendar")
    async def calendar():
        return day_map(app.state.store, app.state.worker.current)

    @app.get("/api/files")
    async def files(
        status: Literal["all", "pending", "uploaded", "album_pending", "error"] = "all",
        q: str = Query(default="", max_length=200),
        offset: int = Query(default=0, ge=0),
        limit: int = Query(default=50, ge=1, le=100),
    ):
        return app.state.store.inventory(status=status, query=q.strip(), offset=offset, limit=limit)

    @app.get("/api/deferred")
    async def deferred():
        return app.state.store.deferred_files()

    @app.post("/api/deferred/{deferred_id}/retry")
    async def retry_deferred(deferred_id: int, body: Run):
        store, worker = app.state.store, app.state.worker
        if worker.busy or worker.auth_lock.locked():
            raise HTTPException(409, "Остановите текущую загрузку перед повтором отложенного файла.")
        if not store.get("telegram_connected"):
            raise ValueError("Сначала подключите Telegram.")
        if not store.one("SELECT 1 FROM deferred WHERE id=?", (deferred_id,)):
            raise HTTPException(404, "Отложенный файл не найден.")
        worker.start(body.mode, deferred_id=deferred_id)
        return {"ok": True, "mode": body.mode}

    @app.get("/api/media/{media_id}")
    async def local_media(media_id: str):
        media = app.state.store.one("SELECT path FROM media WHERE id=?", (media_id,))
        if not media:
            raise HTTPException(404, "Файл не найден.")
        path = (app.state.worker.photos / media["path"]).resolve()
        if not path.is_relative_to(app.state.worker.photos) or not path.is_file():
            raise HTTPException(404, "Локальная копия недоступна.")
        return FileResponse(path, headers={
            "Content-Security-Policy": "default-src 'none'; img-src 'self'; media-src 'self'; sandbox allow-same-origin"
        })

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app


app = make_app()
