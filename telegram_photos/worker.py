import asyncio
import hashlib
import json
import os
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from telethon import TelegramClient, errors, functions

from .config import Config
from .immich import Immich
from .media import Attachment, attachments

MOSCOW = ZoneInfo("Europe/Moscow")


def utc_now():
    return datetime.now(UTC).isoformat()


def boundary(value):
    return datetime.combine(date.fromisoformat(value), time.min, MOSCOW).astimezone(UTC)


def safe_error(exc):
    # Do not send provider response bodies, phone numbers, keys or headers to the UI/log.
    if isinstance(exc, errors.FloodWaitError):
        return f"Telegram просит подождать {exc.seconds} секунд. Повторите позже."
    if isinstance(exc, errors.RPCError):
        return f"Telegram: {type(exc).__name__}. Проверьте данные и повторите."
    if isinstance(exc, httpx.HTTPStatusError):
        return f"Immich вернул HTTP {exc.response.status_code}. Проверьте адрес и права API-ключа."
    if isinstance(exc, (httpx.HTTPError, ConnectionError, OSError)):
        return "Ошибка сети или файлового хранилища. Проверьте доступ и свободное место."
    if isinstance(exc, ValueError):
        return str(exc)
    return f"Операция не завершена ({type(exc).__name__}). Повторите."


def message_topic_id(msg):
    reply = getattr(msg, "reply_to", None)
    # General (1) has ordinary messages/replies without the forum_topic flag.
    if not getattr(reply, "forum_topic", False):
        return 1
    return getattr(reply, "reply_to_top_id", None) or getattr(reply, "reply_to_msg_id", None)


def selected_topics(chat):
    return None if chat.get("topic_ids") is None else set(json.loads(chat["topic_ids"]))


class Worker:
    def __init__(self, store, state: Path, photos: Path, config=None):
        self.store, self.state, self.photos = store, state, photos
        self.config = config or Config()
        self.photos.mkdir(parents=True, exist_ok=True)
        self.client = None
        self.phone = None
        self.phone_code_hash = None
        self.auth_lock = asyncio.Lock()
        self.control_lock = asyncio.Lock()
        self.task = None
        self.phase = "Ожидание"
        self.error = None
        self.last_finished = None
        self.stop = False
        self.next_auto = 0.0
        self.mode = "preview"
        self.report = self.store.get("last_report")
        self.immich_verified = False

    @property
    def busy(self):
        return self.task is not None and not self.task.done()

    def immich(self):
        if not self.config.immich_configured:
            raise ValueError("Для отправки в Immich задайте IMMICH_URL и IMMICH_API_KEY в .env.")
        return Immich(self.config.immich_url, self.config.immich_api_key)

    async def check_immich(self):
        self.immich_verified = False
        client = self.immich()
        try:
            me = await client.request("GET", "users/me")
            await client.request("GET", "albums", params={"isOwned": "true"})
        finally:
            await client.close()
        identity = {"url": self.config.immich_url, "user_id": me["id"]}
        previous = self.store.get("immich_identity")
        if previous and previous != identity and self.store.one("SELECT 1 FROM media WHERE asset_id IS NOT NULL"):
            raise ValueError("В этой установке уже есть загрузки в другой аккаунт или сервер Immich.")
        self.store.set("immich_identity", identity)
        self.store.set("immich_account", me.get("name") or me.get("email") or me["id"])
        self.immich_verified = True
        return self.store.get("immich_account")

    async def telegram(self):
        if self.client is None:
            if not self.config.telegram_configured:
                raise ValueError("Задайте TELEGRAM_API_ID и TELEGRAM_API_HASH в .env и перечитайте настройки.")
            self.client = TelegramClient(
                str(self.state / "telegram"), self.config.telegram_api_id, self.config.telegram_api_hash,
                device_model="Telegram Photos", system_version="Personal server",
                app_version="0.2.0", flood_sleep_threshold=0,
                receive_updates=False, request_retries=2, connection_retries=2,
            )
        if not self.client.is_connected():
            await self.client.connect()
        return self.client

    async def authorized(self):
        client = await self.telegram()
        if not await client.is_user_authorized():
            self.store.set("telegram_connected", False)
            self.store.set("auto_sync", False)
            raise ValueError("Войдите в Telegram по номеру и коду.")
        return client

    async def fetch_topics(self, client, entity):
        topics, offsets = {}, set()
        offset_date, offset_id, offset_topic = None, 0, 0
        while True:
            result = await client(functions.messages.GetForumTopicsRequest(
                peer=entity, offset_date=offset_date, offset_id=offset_id,
                offset_topic=offset_topic, limit=100,
            ))
            page = [topic for topic in result.topics if getattr(topic, "title", None) is not None]
            if not result.topics:
                break
            if not page:
                raise ValueError("Не удалось прочитать список тем. Обновите список ещё раз.")
            for topic in page:
                topics[topic.id] = {
                    "id": topic.id, "title": topic.title,
                    "closed": bool(topic.closed), "hidden": bool(topic.hidden),
                }
            if len(topics) >= result.count:
                break
            last = page[-1]
            dates = {msg.id: msg.date for msg in result.messages}
            offset_date = (
                last.date if result.order_by_create_date
                else dates.get(last.top_message, last.date)
            )
            offset_id, offset_topic = last.top_message, last.id
            marker = (offset_date, offset_id, offset_topic)
            if marker in offsets:
                raise ValueError("Telegram повторил страницу тем. Обновите список ещё раз.")
            offsets.add(marker)
        return sorted(topics.values(), key=lambda item: (item["id"] != 1, item["title"].casefold()))

    def start(self, mode="preview"):
        if self.busy:
            raise ValueError("Задание уже выполняется.")
        if mode not in {"preview", "download", "import"}:
            raise ValueError("Неизвестный режим запуска.")
        if mode == "import" and not self.config.immich_configured:
            raise ValueError("Задайте IMMICH_URL и IMMICH_API_KEY или выберите локальный режим.")
        retry_after = self.store.get("telegram_retry_after", 0)
        if retry_after > datetime.now(UTC).timestamp():
            raise ValueError("Telegram ограничил частоту запросов. Дождитесь следующего запуска по расписанию.")
        self.stop, self.error = False, None
        self.mode = mode
        self.task = asyncio.create_task(self.run(mode))

    async def run(self, mode="preview"):
        self.mode = mode
        try:
            if mode == "preview":
                await self.preview()
                return
            if mode == "import":
                # Verify the env-provided account before downloading or uploading.
                await self.check_immich()
            # Downloads and uploads are independent durable stages. A network
            # failure in Telegram must not block photos already in the inbox.
            try:
                await self.download(upload_to_immich=mode == "import")
            except Exception as exc:
                self.error = safe_error(exc)
            if not self.stop and mode == "import":
                await self.upload()
        except Exception as exc:
            self.error = safe_error(exc)
        finally:
            self.phase = "Остановлено" if self.stop else "Ожидание"
            self.last_finished = utc_now()
            self.next_auto = asyncio.get_running_loop().time() + self.store.get("interval", 30) * 60

    async def scheduler(self):
        # Startup does not automatically run an unconfigured Telegram account.
        self.next_auto = asyncio.get_running_loop().time() + 60
        while True:
            await asyncio.sleep(10)
            if (
                self.store.get("auto_sync", False) and not self.busy and not self.control_lock.locked()
                and asyncio.get_running_loop().time() >= self.next_auto
            ):
                if self.store.get("telegram_retry_after", 0) <= datetime.now(UTC).timestamp():
                    try:
                        self.start(self.store.get("schedule_mode", "preview"))
                    except ValueError as exc:
                        self.error = safe_error(exc)
                        self.store.set("auto_sync", False)

    async def preview(self):
        client = await self.authorized()
        self.report = {"chats": [], "complete": False, "started_at": utc_now()}
        try:
            for chat in self.store.rows("SELECT * FROM chats WHERE selected=1 ORDER BY id"):
                if self.stop:
                    break
                self.phase = f"Проверка без скачивания: {chat['title']}"
                result = {"title": chat["title"], "scanned": 0, "new": 0, "known": 0, "bytes": 0,
                          "unknown_sizes": 0, "error": None, "complete": False, "types": {},
                          "skipped": {}, "albums": 0, "album_items": 0}
                albums = set()
                self.report["chats"].append(result)
                try:
                    entity = await client.get_entity(chat["id"])
                    if getattr(entity, "noforwards", False):
                        raise ValueError("В этом чате запрещено сохранение содержимого.")
                    since = boundary(chat["since"])
                    until = boundary(chat["until_date"]) + timedelta(days=1) if chat["until_date"] else None
                    topic_ids = selected_topics(chat)
                    if topic_ids is not None:
                        names = [topic["title"] for topic in json.loads(chat["topics"]) if topic["id"] in topic_ids]
                        result["title"] += " → " + ", ".join(names or map(str, sorted(topic_ids)))
                    # Independent cursor: dry run must never consume the download queue.
                    async for msg in client.iter_messages(
                        entity, reverse=True, limit=None, offset_date=since - timedelta(seconds=1), wait_time=1
                    ):
                        if self.stop or (until and msg.date >= until):
                            break
                        if msg.date < since:
                            continue
                        if topic_ids is not None and message_topic_id(msg) not in topic_ids:
                            continue
                        result["scanned"] += 1
                        items, skipped = attachments(msg)
                        if skipped:
                            result["skipped"][skipped] = result["skipped"].get(skipped, 0) + 1
                        group = getattr(msg, "grouped_id", None)
                        if items and group is not None:
                            albums.add(group)
                            result["albums"] = len(albums)
                            result["album_items"] += len(items)
                        for item in items:
                            result["types"][item.kind] = result["types"].get(item.kind, 0) + 1
                            known = self.store.one(
                                "SELECT 1 FROM sources WHERE chat_id=? AND message_id=? AND part=?",
                                (chat["id"], msg.id, item.part),
                            )
                            result["known" if known else "new"] += 1
                            if not known:
                                result["bytes"] += item.size or 0
                                result["unknown_sizes"] += int(item.size is None)
                    result["complete"] = not self.stop
                except errors.FloodWaitError as exc:
                    self.store.set("telegram_retry_after", datetime.now(UTC).timestamp() + exc.seconds)
                    result["error"] = safe_error(exc)
                    raise
                except Exception as exc:
                    result["error"] = safe_error(exc)
                    self.error = "Не все чаты удалось проверить. Результат частичный."
            self.report["complete"] = not self.stop and all(r["complete"] for r in self.report["chats"])
        finally:
            self.store.set("last_report", self.report)

    async def download(self, upload_to_immich=False):
        client = await self.authorized()
        for chat in self.store.rows("SELECT * FROM chats WHERE selected=1 ORDER BY id"):
            if self.stop:
                return
            self.phase = f"Скачивание: {chat['title']}"
            self.store.execute("UPDATE chats SET error=NULL WHERE id=?", (chat["id"],))
            try:
                entity = await client.get_entity(chat["id"])
                if getattr(entity, "noforwards", False):
                    raise ValueError("В этом чате запрещено сохранение содержимого.")
                while not self.stop:
                    count = await self.download_chat(client, entity, chat)
                    if not self.stop and upload_to_immich:
                        await self.upload()
                    if count < 1000:
                        break
                    chat = self.store.one("SELECT * FROM chats WHERE id=?", (chat["id"],))
            except errors.FloodWaitError as exc:
                self.store.set("telegram_retry_after", datetime.now(UTC).timestamp() + exc.seconds)
                raise
            except Exception as exc:
                self.store.execute(
                    "UPDATE chats SET error=? WHERE id=?", (safe_error(exc), chat["id"])
                )
                self.error = "Не все чаты обработаны. Подробности рядом с чатом."

    async def download_chat(self, client, entity, chat):
        since = boundary(chat["since"])
        until = boundary(chat["until_date"]) + timedelta(days=1) if chat["until_date"] else None
        topic_ids = selected_topics(chat)
        args = {"reverse": True, "limit": 1000, "min_id": chat["cursor"], "wait_time": 1}
        if not chat["cursor"]:
            args["offset_date"] = since - timedelta(seconds=1)
        count = 0
        async for msg in client.iter_messages(entity, **args):
            if self.stop:
                break
            if until and msg.date >= until:
                break
            in_topic = topic_ids is None or message_topic_id(msg) in topic_ids
            if msg.date >= since and in_topic:
                items, skipped = attachments(msg)
                if skipped == "pending_video" or (items and skipped == "unavailable"):
                    raise ValueError("Telegram ещё не подготовил вложение. Повторите загрузку позже.")
                for item in items:
                    if self.stop:
                        return count
                    known = self.store.one(
                        "SELECT 1 FROM sources WHERE chat_id=? AND message_id=? AND part=?",
                        (chat["id"], msg.id, item.part),
                    )
                    if not known:
                        await self.download_message(client, chat["id"], msg, item)
            self.store.execute("UPDATE chats SET cursor=? WHERE id=?", (msg.id, chat["id"]))
            count += 1
        return count

    async def download_message(self, client, chat_id, msg, item):
        if isinstance(item, str):
            item = Attachment("main", item, "photo", msg)
        temp_dir = self.photos / ".partial"
        temp_dir.mkdir(exist_ok=True)
        temp = temp_dir / f"{chat_id}_{msg.id}_{item.part}{item.extension}"
        try:
            output = await client.download_media(item.target, file=str(temp))
            if not output or not temp.is_file() or not temp.stat().st_size:
                raise ValueError("Telegram не вернул файл; сообщение будет повторено.")
            with temp.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            existing = self.store.one("SELECT path FROM media WHERE id=?", (digest,))
            if existing:
                # Imported photos stay processed even if later deleted in Immich.
                target = self.photos / existing["path"]
            else:
                target = self.photos / "inbox" / digest[:2] / f"{digest}{item.extension}"
                target.parent.mkdir(parents=True, exist_ok=True)
                os.replace(temp, target)
                os.utime(target, (msg.date.timestamp(), msg.date.timestamp()))
            self.store.record(
                chat_id, msg.id, digest, str(target.relative_to(self.photos)), msg.date.isoformat(),
                part=item.part, kind=item.kind, grouped_id=getattr(msg, "grouped_id", None),
                advance_cursor=False,
            )
        finally:
            temp.unlink(missing_ok=True)

    async def upload(self):
        immich = self.immich()
        try:
            for media in self.store.rows("SELECT * FROM media WHERE asset_id IS NULL"):
                if self.stop:
                    return
                self.phase = "Загрузка в Immich"
                try:
                    result = await immich.upload(media, self.photos / media["path"])
                    self.store.execute(
                        "UPDATE media SET asset_id=?, error=NULL WHERE id=?", (result["id"], media["id"])
                    )
                except Exception as exc:
                    self.store.execute("UPDATE media SET error=? WHERE id=?", (safe_error(exc), media["id"]))
                    self.error = "Не все файлы загружены. Повторная синхронизация продолжит загрузку."
            for src in self.store.rows("""
                SELECT s.*, m.asset_id, c.title, c.album_id FROM sources s
                JOIN media m ON m.id=s.media_id JOIN chats c ON c.id=s.chat_id
                WHERE s.album_added=0 AND m.asset_id IS NOT NULL
            """):
                if self.stop:
                    return
                chat = self.store.one("SELECT * FROM chats WHERE id=?", (src["chat_id"],))
                try:
                    album_id = chat["album_id"] or await immich.album(src["title"], src["chat_id"])
                    self.store.execute("UPDATE chats SET album_id=? WHERE id=?", (album_id, src["chat_id"]))
                    await immich.add_to_album(album_id, src["asset_id"])
                    self.store.execute(
                        "UPDATE sources SET album_added=1 WHERE chat_id=? AND message_id=? AND part=?",
                        (src["chat_id"], src["message_id"], src["part"]),
                    )
                except Exception as exc:
                    self.store.execute(
                        "UPDATE chats SET error=? WHERE id=?", (safe_error(exc), src["chat_id"])
                    )
                    self.error = "Часть файлов ещё не добавлена в альбомы. Повторите синхронизацию."
        finally:
            await immich.close()

    async def close(self):
        if self.task and not self.task.done():
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
        if self.client:
            await self.client.disconnect()
