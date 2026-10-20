import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from telethon import errors
from telethon.tl import types as tg
from telethon.tl.types import MessageMediaDocument, MessageMediaPhoto, MessageReplyHeader, Photo

from telegram_photos.app import make_app
from telegram_photos.config import Config, migrate_credentials
from telegram_photos.immich import Immich, api_url
from telegram_photos.media import attachments
from telegram_photos.store import Store
from telegram_photos.worker import Worker, boundary, message_topic_id

HEADERS = {"X-Requested-With": "telegram-photos"}
TEST_PASSWORD = "local-test-password-only"


@pytest.fixture
def worker(tmp_path):
    store = Store(tmp_path / "state")
    result = Worker(store, tmp_path / "state", tmp_path / "photos")
    yield result
    store.db.close()


def add_chat(worker, chat_id=1, since="2026-10-01", until=None):
    worker.store.execute(
        "INSERT INTO chats(id,title,selected,since,until_date) VALUES(?,?,1,?,?)",
        (chat_id, f"Чат {chat_id}", since, until),
    )
    return worker.store.one("SELECT * FROM chats WHERE id=?", (chat_id,))


def message(number, sent_at="2026-10-20T10:00:00+00:00", payload=b"same-photo"):
    return SimpleNamespace(
        id=number, date=datetime.fromisoformat(sent_at),
        media=MessageMediaPhoto(photo=Photo(
            id=number, access_hash=0, file_reference=b"", date=datetime.fromisoformat(sent_at),
            sizes=[], dc_id=1,
        )),
        document=None, sticker=None, noforwards=False, payload=payload,
    )


class TelegramFake:
    def __init__(self, messages, fail_id=None):
        self.messages, self.fail_id = messages, fail_id
        self.downloaded = []
        self.calls = []

    async def iter_messages(self, entity, **kwargs):
        self.calls.append(kwargs)
        count = 0
        for msg in self.messages:
            if msg.id > kwargs.get("min_id", 0):
                yield msg
                count += 1
                if count == kwargs.get("limit"):
                    break

    async def download_media(self, msg, file):
        self.downloaded.append(msg.id)
        Path(file).write_bytes(msg.payload)
        if msg.id == self.fail_id:
            raise ConnectionError("interrupted")
        return file

    async def get_entity(self, entity):
        return SimpleNamespace(noforwards=False)


async def test_failed_download_does_not_advance_cursor_and_retries(worker):
    chat = add_chat(worker)
    client = TelegramFake([message(1, payload=b"one"), message(2, payload=b"two")], fail_id=2)
    with pytest.raises(ConnectionError):
        await worker.download_chat(client, 1, chat)
    assert worker.store.one("SELECT cursor FROM chats")["cursor"] == 1
    assert len(worker.store.rows("SELECT * FROM media")) == 1
    assert not list((worker.photos / ".partial").iterdir())
    client.fail_id = None
    await worker.download_chat(client, 1, worker.store.one("SELECT * FROM chats"))
    assert client.downloaded == [1, 2, 2]
    assert worker.store.one("SELECT cursor FROM chats")["cursor"] == 2


async def test_duplicates_across_chats_share_one_file_and_keep_provenance(worker):
    add_chat(worker, 1)
    add_chat(worker, 2)
    client = TelegramFake([])
    await worker.download_message(client, 1, message(10), ".jpg")
    await worker.download_message(client, 2, message(20), ".jpg")
    assert len(worker.store.rows("SELECT * FROM media")) == 1
    assert len(worker.store.rows("SELECT * FROM sources")) == 2
    assert len(list((worker.photos / "inbox").rglob("*.jpg"))) == 1
    # Changing a date range and rescanning does not redownload known messages.
    client.messages = [message(10)]
    chat = worker.store.one("SELECT * FROM chats WHERE id=1")
    chat["cursor"] = 0
    await worker.download_chat(client, 1, chat)
    assert client.downloaded == [10, 20]


async def test_date_boundaries_include_entire_moscow_day(worker):
    chat = add_chat(worker, since="2026-10-20", until="2026-10-20")
    client = TelegramFake([
        message(1, "2026-10-19T20:59:59+00:00", b"before"),
        message(2, "2026-10-19T21:00:00+00:00", b"first"),
        message(3, "2026-10-20T20:59:59+00:00", b"last"),
        message(4, "2026-10-20T21:00:00+00:00", b"after"),
    ])
    await worker.download_chat(client, 1, chat)
    assert client.downloaded == [2, 3]
    assert worker.store.one("SELECT cursor FROM chats")["cursor"] == 3
    assert boundary("2026-10-20").hour == 21


async def test_image_documents_supported_stickers_and_previews_skipped(worker):
    chat = add_chat(worker)
    doc = message(1, payload=b"png")
    doc.media = MessageMediaDocument()
    doc.document = SimpleNamespace(mime_type="image/png")
    sticker = message(2)
    sticker.media = MessageMediaDocument()
    sticker.document = SimpleNamespace(mime_type="image/webp")
    sticker.sticker = True
    preview = message(3)
    preview.media = SimpleNamespace(photo=True)
    protected = message(4)
    protected.noforwards = True
    client = TelegramFake([doc, sticker, preview, protected])
    await worker.download_chat(client, 1, chat)
    assert client.downloaded == [1]
    assert worker.store.one("SELECT path FROM media")["path"].endswith(".png")


async def test_history_continues_past_one_batch(worker, monkeypatch):
    add_chat(worker)
    client = TelegramFake([message(n, payload=str(n).encode()) for n in range(1, 1003)])
    for msg in client.messages:
        msg.media = None  # Keep test fast; exercise paging through text history.
    monkeypatch.setattr(worker, "authorized", AsyncMock(return_value=client))
    monkeypatch.setattr(worker, "upload", AsyncMock())
    await worker.download()
    assert worker.store.one("SELECT cursor FROM chats")["cursor"] == 1002
    assert [c["min_id"] for c in client.calls] == [0, 1000]


@pytest.mark.parametrize("upload_status", ["created", "duplicate"])
async def test_upload_album_retry_never_reuploads_deleted_or_existing_asset(worker, monkeypatch, upload_status):
    add_chat(worker)
    await worker.download_message(TelegramFake([]), 1, message(1), ".jpg")
    requests = []
    album_fails = True

    def handle(request):
        requests.append((request.method, request.url.path))
        if request.url.path == "/api/assets":
            assert request.headers["x-immich-checksum"]
            assert b'name="fileCreatedAt"' in request.content
            assert b"same-photo" in request.content
            return httpx.Response(201, json={"id": "asset-1", "status": upload_status})
        if request.url.path == "/api/albums" and request.method == "GET":
            return httpx.Response(200, json=[])
        if request.url.path == "/api/albums" and request.method == "POST":
            return httpx.Response(201, json={"id": "album-1"})
        if request.url.path == "/api/albums/album-1/assets":
            if album_fails:
                return httpx.Response(503, json={"error": "temporary"})
            return httpx.Response(200, json=[{"id": "asset-1", "success": True}])
        raise AssertionError(request.url)

    monkeypatch.setattr(worker, "immich", lambda: Immich(
        "http://immich.test", "not-a-real-key", httpx.MockTransport(handle)
    ))
    await worker.upload()
    assert worker.store.one("SELECT asset_id FROM media")["asset_id"] == "asset-1"
    assert worker.store.one("SELECT album_added FROM sources")["album_added"] == 0
    album_fails = False
    await worker.upload()
    assert worker.store.one("SELECT album_added FROM sources")["album_added"] == 1
    # Once imported, no probing/recreation on subsequent syncs, even if the
    # user later removes the asset in Immich.
    await worker.upload()
    assert requests.count(("POST", "/api/assets")) == 1
    assert requests.count(("POST", "/api/albums")) == 1
    assert all(method != "DELETE" and "/people" not in path for method, path in requests)


async def test_telegram_outage_does_not_block_pending_uploads(worker, monkeypatch):
    monkeypatch.setattr(worker, "download", AsyncMock(side_effect=ConnectionError()))
    upload = AsyncMock()
    monkeypatch.setattr(worker, "upload", upload)
    monkeypatch.setattr(worker, "check_immich", AsyncMock())
    await worker.run("import")
    upload.assert_awaited_once()
    assert worker.error
    assert worker.last_finished


async def test_flood_wait_preserves_cursor_and_blocks_immediate_retry(worker, monkeypatch):
    add_chat(worker)
    fake = TelegramFake([])
    fake.get_entity = AsyncMock(side_effect=errors.FloodWaitError(request=None, capture=120))
    monkeypatch.setattr(worker, "authorized", AsyncMock(return_value=fake))
    with pytest.raises(errors.FloodWaitError):
        await worker.download()
    assert worker.store.one("SELECT cursor FROM chats")["cursor"] == 0
    assert worker.store.get("telegram_retry_after") > datetime.now(UTC).timestamp()
    with pytest.raises(ValueError, match="ограничил"):
        worker.start()


@pytest.fixture
async def web(tmp_path):
    config = Config(
        app_password=TEST_PASSWORD, telegram_api_id=123, telegram_api_hash="a" * 32,
        immich_url="http://immich.test/api", immich_api_key="dummy-api-secret",
    )
    env_file = tmp_path / ".env"
    env_file.write_text(f"APP_PASSWORD={TEST_PASSWORD}\nTELEGRAM_API_ID=123\nTELEGRAM_API_HASH={'a' * 32}\n")
    app = make_app(tmp_path / "state", tmp_path / "photos", config=config, env_file=env_file)
    app.state.test_env_file = env_file
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://testserver", headers=HEADERS,
        ) as client:
            yield app, client


async def sign_in(client):
    result = await client.post("/api/login", json={"password": TEST_PASSWORD})
    assert result.status_code == 200


async def test_web_auth_csrf_and_secret_redaction(web):
    app, client = web
    assert (await client.get("/health")).status_code == 200
    assert (await client.get("/api/status")).status_code == 401
    assert (await client.post("/api/login", json={"password": TEST_PASSWORD},
                              headers={"Origin": "https://evil.test"})).status_code == 403
    assert (await client.post("/api/login", json={"password": TEST_PASSWORD},
                              headers={"X-Requested-With": ""})).status_code == 403
    await sign_in(client)
    bad = await client.post("/api/telegram/send-code", json={"phone": "SECRET"})
    assert bad.status_code == 422
    assert "SECRET" not in bad.text
    assert (await client.post("/api/settings/telegram", json={"api_id": 1, "api_hash": "a" * 32})).status_code == 404
    assert (await client.post("/api/settings/immich", json={"api_key": "secret"})).status_code == 404
    status = await client.get("/api/status")
    assert status.json()["telegram_configured"]
    assert "a" * 32 not in status.text
    assert "api_hash" not in status.text
    assert "dummy-api-secret" not in status.text
    assert not any(r["key"] in {"api_id", "api_hash", "immich_key", "cookie_secret"}
                   for r in app.state.store.rows("SELECT * FROM settings"))
    assert "frame-ancestors 'none'" in status.headers["content-security-policy"]
    await client.post("/api/logout", json={})
    assert (await client.get("/api/status")).status_code == 401


async def test_web_login_rate_limit(web):
    _, client = web
    for _ in range(10):
        assert (await client.post("/api/login", json={"password": "wrong"})).status_code == 401
    assert (await client.post("/api/login", json={"password": TEST_PASSWORD})).status_code == 429


async def test_2fa_flow_does_not_store_code_or_password(web, monkeypatch):
    app, client = web
    await sign_in(client)
    telegram = SimpleNamespace(
        is_user_authorized=AsyncMock(return_value=False),
        send_code_request=AsyncMock(return_value=SimpleNamespace(phone_code_hash="transient-hash")),
        sign_in=AsyncMock(side_effect=[errors.SessionPasswordNeededError(None), None]),
        get_me=AsyncMock(return_value=SimpleNamespace(id=123)),
    )
    monkeypatch.setattr(app.state.worker, "telegram", AsyncMock(return_value=telegram))
    assert (await client.post("/api/telegram/send-code", json={"phone": "+79991234567"})).json()["status"] == "code"
    assert (await client.post("/api/telegram/code", json={"code": "12345"})).json()["status"] == "password"
    assert (await client.post("/api/telegram/password", json={"password": "private-2fa"})).json()["status"] == "connected"
    data = json.dumps(app.state.store.rows("SELECT * FROM settings"))
    assert "private-2fa" not in data
    assert "transient-hash" not in data
    assert "+79991234567" not in data
    assert app.state.worker.phone is None
    assert app.state.store.get("telegram_connected")


async def test_destination_account_cannot_change_after_upload(web, monkeypatch):
    app, client = web
    await sign_in(client)
    app.state.store.set("immich_identity", {"url": "http://immich.test/api", "user_id": "owner-1"})
    app.state.store.execute(
        "INSERT INTO media(id,path,created_at,asset_id) VALUES('hash','photo.jpg','2026-10-20','asset-1')"
    )
    monkeypatch.setattr(Immich, "request", AsyncMock(side_effect=[{"id": "owner-2"}, []]))
    result = await client.post("/api/immich/check", json={})
    assert result.status_code == 400
    assert "другой аккаунт" in result.text
    assert app.state.store.get("immich_key") is None


async def test_invalid_chat_dates_are_atomic_and_changed_range_rescans(web):
    app, client = web
    await sign_in(client)
    add_chat(app.state.worker, 1)
    add_chat(app.state.worker, 2)
    app.state.store.execute("UPDATE chats SET cursor=25")
    selection = [
        {"id": 1, "selected": True, "since": "2026-09-01", "until": None},
        {"id": 2, "selected": True, "since": "2026-10-20", "until": "2026-10-01"},
    ]
    assert (await client.put("/api/chats", json=selection)).status_code == 400
    assert all(row["cursor"] == 25 for row in app.state.store.rows("SELECT cursor FROM chats"))
    selection[1]["until"] = None
    assert (await client.put("/api/chats", json=selection)).status_code == 200
    assert all(row["cursor"] == 0 for row in app.state.store.rows("SELECT cursor FROM chats"))


@pytest.mark.parametrize("url", ["file:///tmp/x", "https://user:secret@host", "http://host?api=key"])
def test_reject_credential_bearing_or_invalid_immich_urls(url):
    with pytest.raises(ValueError):
        api_url(url)


async def test_default_dry_run_changes_no_files_cursors_or_uploads(worker, monkeypatch):
    add_chat(worker, since="2026-10-20", until="2026-10-20")
    worker.store.record(1, 2, "known", "old.jpg", "2026-10-20T01:00:00+00:00")
    before = worker.store.rows("SELECT * FROM chats")
    candidate = message(3, "2026-10-20T10:00:00+00:00")
    candidate.file = SimpleNamespace(size=123456)
    client = TelegramFake([
        message(1, "2026-10-19T20:59:59+00:00"),
        message(2, "2026-10-19T21:00:00+00:00"),
        candidate,
        message(4, "2026-10-20T21:00:00+00:00"),
    ])
    worker.config = Config(immich_url="http://immich.test/api", immich_api_key="configured-but-unused")
    monkeypatch.setattr(worker, "authorized", AsyncMock(return_value=client))
    immich = Mock(side_effect=AssertionError("dry run must not contact Immich"))
    monkeypatch.setattr(worker, "immich", immich)
    worker.start()  # Omitted mode must always mean preview.
    await worker.task
    assert not worker.error
    assert worker.report["complete"]
    result = worker.report["chats"][0]
    assert (result["new"], result["known"], result["bytes"]) == (1, 1, 123456)
    assert not client.downloaded
    immich.assert_not_called()
    assert worker.store.rows("SELECT * FROM chats") == before
    assert len(worker.store.rows("SELECT * FROM sources")) == 1
    assert len(worker.store.rows("SELECT * FROM media")) == 1
    assert list(worker.photos.rglob("*")) == []


async def test_explicit_download_works_without_immich(worker, monkeypatch):
    add_chat(worker)
    client = TelegramFake([message(1)])
    monkeypatch.setattr(worker, "authorized", AsyncMock(return_value=client))
    immich = Mock(side_effect=AssertionError("local mode must not contact Immich"))
    monkeypatch.setattr(worker, "immich", immich)
    await worker.run("download")
    assert not worker.error
    assert client.downloaded == [1]
    assert len(list(worker.photos.rglob("*.jpg"))) == 1
    assert worker.store.one("SELECT asset_id FROM media")["asset_id"] is None
    immich.assert_not_called()
    # A later dry run sees it as known and still changes nothing.
    await worker.run("preview")
    assert worker.report["chats"][0]["known"] == 1
    assert worker.report["chats"][0]["new"] == 0
    assert client.downloaded == [1]


async def test_api_empty_body_defaults_to_preview_without_immich(web, monkeypatch):
    app, client = web
    await sign_in(client)
    worker = app.state.worker
    worker.config = Config()
    app.state.store.set("telegram_connected", True)
    add_chat(worker)
    run = AsyncMock()
    monkeypatch.setattr(worker, "run", run)
    result = await client.post("/api/sync", json={})
    await worker.task
    assert result.json()["mode"] == "preview"
    run.assert_awaited_once_with("preview")
    assert (await client.post("/api/sync", json={"mode": "import"})).status_code == 400
    assert (await client.post("/api/schedule", json={"enabled": True, "interval": 30})).status_code == 200
    assert worker.store.get("schedule_mode") == "preview"
    assert (await client.post("/api/schedule", json={"enabled": True, "interval": 30, "mode": "import"})).status_code == 400


async def test_reload_reads_env_without_exposing_or_storing_credentials(web):
    app, client = web
    await sign_in(client)
    app.state.test_env_file.write_text(
        f"APP_PASSWORD={TEST_PASSWORD}\nTELEGRAM_API_ID=456\nTELEGRAM_API_HASH={'b' * 32}\n"
        "IMMICH_URL=\nIMMICH_API_KEY=\nDRY_RUN=true\n"
    )
    result = await client.post("/api/config/reload", json={})
    assert result.status_code == 200
    assert app.state.worker.config.telegram_api_id == 456
    assert app.state.worker.config.telegram_api_hash == "b" * 32
    status = await client.get("/api/status")
    assert not status.json()["immich_configured"]
    assert status.json()["default_mode"] == "preview"
    assert not status.json()["auto_sync"]
    assert "b" * 32 not in status.text
    assert "b" * 32 not in json.dumps(app.state.store.rows("SELECT * FROM settings"))


def test_migration_preserves_legacy_keys_in_private_env(worker, tmp_path):
    worker.store.set("api_id", 123)
    worker.store.set("api_hash", "c" * 32)
    worker.store.set("immich_key", "legacy-key")
    worker.store.set("cookie_secret", "old-secret")
    env_file = tmp_path / ".env"
    migrate_credentials(worker.store, env_file)
    assert env_file.stat().st_mode & 0o777 == 0o600
    assert "c" * 32 in env_file.read_text()
    assert "legacy-key" in env_file.read_text()
    assert worker.store.get("api_hash") is None
    assert worker.store.get("immich_key") is None
    assert worker.store.get("cookie_secret") is None
    assert Config.load(env_file).telegram_api_id == 123


async def test_dry_run_failure_is_partial_and_does_not_consume_history(worker, monkeypatch):
    add_chat(worker)
    client = TelegramFake([])
    client.get_entity = AsyncMock(side_effect=ConnectionError())
    monkeypatch.setattr(worker, "authorized", AsyncMock(return_value=client))
    await worker.run()
    assert not worker.report["complete"]
    assert worker.report["chats"][0]["error"]
    assert worker.store.one("SELECT cursor FROM chats")["cursor"] == 0
    assert not client.downloaded


def topic(number, title=None, **kwargs):
    return SimpleNamespace(
        id=number, title=title or f"Тема {number}", closed=False, hidden=False,
        date=datetime(2026, 10, 1, tzinfo=UTC), top_message=number + 1000, **kwargs,
    )


@pytest.mark.parametrize("by_creation", [False, True])
async def test_topic_pagination_uses_last_topic_offsets_and_keeps_general(worker, by_creation):
    first = [topic(i) for i in range(1, 101)]
    last_date = datetime(2026, 10, 19, tzinfo=UTC)
    client = AsyncMock(side_effect=[
        SimpleNamespace(topics=first, count=101, order_by_create_date=by_creation,
                        messages=[SimpleNamespace(id=1100, date=last_date)]),
        SimpleNamespace(topics=[topic(101)], count=101, order_by_create_date=by_creation, messages=[]),
    ])
    result = await worker.fetch_topics(client, "forum")
    assert len(result) == 101
    assert result[0]["id"] == 1
    request = client.call_args_list[1].args[0]
    assert request.offset_id == 1100
    assert request.offset_topic == 100
    assert request.offset_date == (first[-1].date if by_creation else last_date)


async def test_topic_pagination_does_not_silently_accept_repeated_partial_page(worker):
    client = AsyncMock(return_value=SimpleNamespace(
        topics=[topic(1)], count=3, order_by_create_date=True, messages=[],
    ))
    with pytest.raises(ValueError, match="повторил"):
        await worker.fetch_topics(client, "forum")


def forum_messages():
    messages = [message(i, payload=f"photo-{i}".encode()) for i in range(1, 6)]
    messages[1].reply_to = MessageReplyHeader(reply_to_msg_id=50, forum_topic=True)
    messages[2].reply_to = MessageReplyHeader(reply_to_msg_id=2, reply_to_top_id=50, forum_topic=True)
    messages[3].reply_to = MessageReplyHeader(reply_to_msg_id=60, forum_topic=True)
    messages[4].reply_to = MessageReplyHeader(reply_to_msg_id=1)  # Ordinary reply in General.
    return messages


@pytest.mark.parametrize("ids, expected", [([50], [2, 3]), ([1], [1, 5]), ([50, 60], [2, 3, 4])])
async def test_topic_filter_applies_identically_to_preview_and_download(worker, monkeypatch, ids, expected):
    add_chat(worker)
    worker.store.execute(
        "UPDATE chats SET is_forum=1,topic_ids=?,topics=?",
        (json.dumps(ids), json.dumps([{"id": i, "title": f"Тема {i}"} for i in [1, 50, 60]])),
    )
    client = TelegramFake(forum_messages())
    monkeypatch.setattr(worker, "authorized", AsyncMock(return_value=client))
    monkeypatch.setattr(worker, "immich", Mock(side_effect=AssertionError("no Immich")))
    await worker.run("preview")
    assert worker.report["complete"]
    assert worker.report["chats"][0]["new"] == len(expected)
    assert worker.report["chats"][0]["scanned"] == len(expected)
    assert worker.store.one("SELECT cursor FROM chats")["cursor"] == 0
    assert list(worker.photos.rglob("*")) == []
    await worker.run("download")
    assert not worker.error
    assert client.downloaded == expected
    assert worker.store.one("SELECT cursor FROM chats")["cursor"] == 5
    await worker.run("preview")
    assert worker.report["chats"][0]["known"] == len(expected)
    assert worker.report["chats"][0]["new"] == 0


def test_missing_topic_id_is_not_treated_as_general():
    msg = message(1)
    msg.reply_to = MessageReplyHeader(forum_topic=True)
    assert message_topic_id(msg) is None


async def test_topic_selection_rescans_and_validates_atomically(web, monkeypatch):
    app, client = web
    await sign_in(client)
    worker = app.state.worker
    add_chat(worker)
    add_chat(worker, 2)
    worker.store.execute(
        "UPDATE chats SET is_forum=1,topics=?,cursor=5",
        (json.dumps([{"id": 1, "title": "Общее"}, {"id": 50, "title": "Фото"}, {"id": 60, "title": "Объявления"}]),),
    )
    selection = {"id": 1, "selected": True, "since": "2026-10-01", "topic_ids": [50]}
    assert (await client.put("/api/chats", json=[selection])).status_code == 200
    telegram = TelegramFake(forum_messages())
    await worker.download_chat(telegram, 1, worker.store.one("SELECT * FROM chats WHERE id=1"))
    assert telegram.downloaded == [2, 3]
    selection["topic_ids"] = [60]
    assert (await client.put("/api/chats", json=[selection])).status_code == 200
    assert worker.store.one("SELECT cursor FROM chats WHERE id=1")["cursor"] == 0
    assert worker.store.one("SELECT cursor FROM chats WHERE id=2")["cursor"] == 5
    await worker.download_chat(telegram, 1, worker.store.one("SELECT * FROM chats WHERE id=1"))
    assert telegram.downloaded == [2, 3, 4]
    for invalid in [[], [999]]:
        other = {"id": 2, "selected": False, "since": "2026-09-01"}
        assert (await client.put("/api/chats", json=[other, {**selection, "topic_ids": invalid}])).status_code == 400
        assert worker.store.one("SELECT cursor FROM chats WHERE id=2")["cursor"] == 5
    selection["topic_ids"] = None
    assert (await client.put("/api/chats", json=[selection])).status_code == 200
    await worker.download_chat(telegram, 1, worker.store.one("SELECT * FROM chats WHERE id=1"))
    assert telegram.downloaded == [2, 3, 4, 1, 5]
    result = (await client.get("/api/chats")).json()
    assert result[0]["topic_ids"] is None
    assert result[0]["topics"][0]["title"] == "Общее"


async def test_refresh_loads_forum_topics_preserving_saved_selection(web, monkeypatch):
    app, client = web
    await sign_in(client)
    worker = app.state.worker
    add_chat(worker)
    worker.store.execute("UPDATE chats SET topic_ids='[50]',cursor=99")

    async def dialogs():
        yield SimpleNamespace(id=1, title="Супергруппа", is_group=True, is_channel=True,
                              entity=SimpleNamespace(forum=True, noforwards=False))
        yield SimpleNamespace(id=2, title="Группа", is_group=True, is_channel=False,
                              entity=SimpleNamespace(noforwards=False))

    telegram = SimpleNamespace(
        iter_dialogs=dialogs, get_me=AsyncMock(return_value=SimpleNamespace(id=123)),
    )
    monkeypatch.setattr(worker, "authorized", AsyncMock(return_value=telegram))
    fetch = AsyncMock(return_value=[{"id": 50, "title": "Фотографии", "closed": False, "hidden": False}])
    monkeypatch.setattr(worker, "fetch_topics", fetch)
    assert (await client.post("/api/chats/refresh")).status_code == 200
    rows = (await client.get("/api/chats")).json()
    assert rows[0]["is_forum"]
    assert rows[0]["topic_ids"] == [50]
    assert rows[0]["cursor"] == 99
    assert rows[0]["topics"][0]["title"] == "Фотографии"
    assert not rows[1]["is_forum"]
    fetch.assert_awaited_once()
    fetch.side_effect = ConnectionError()
    assert (await client.post("/api/chats/refresh")).status_code == 200
    row = (await client.get("/api/chats")).json()[0]
    assert row["topics_error"]
    assert row["topics"] == rows[0]["topics"]
    assert row["topic_ids"] == [50]


def test_topics_migration_preserves_old_history_and_is_repeatable(tmp_path):
    root = tmp_path / "legacy"
    root.mkdir()
    with sqlite3.connect(root / "photos.sqlite3") as db:
        db.executescript("""
            CREATE TABLE chats(id INTEGER PRIMARY KEY, title TEXT NOT NULL, selected INTEGER DEFAULT 0,
                since TEXT NOT NULL, until_date TEXT, cursor INTEGER DEFAULT 0, album_id TEXT, error TEXT);
            INSERT INTO chats(id,title,selected,since,cursor) VALUES(-1001,'Группа',1,'2026-09-01',42);
        """)
    for iteration in range(2):
        store = Store(root)
        row = store.one("SELECT * FROM chats")
        assert row["cursor"] == (0 if iteration == 0 else 42) and row["selected"] == 1
        assert row["topic_ids"] is None and row["topics"] == "[]"
        store.execute("UPDATE chats SET cursor=42")
        store.db.close()


def document_message(number, mime="video/mp4", filename=None, attributes=(), payload=b"video"):
    msg = message(number, payload=payload)
    attrs = list(attributes)
    if filename:
        attrs.append(tg.DocumentAttributeFilename(filename))
    msg.document = tg.Document(
        id=number, access_hash=0, file_reference=b"", date=msg.date,
        mime_type=mime, size=len(payload), dc_id=1, attributes=attrs,
    )
    msg.media = MessageMediaDocument(document=msg.document)
    return msg


@pytest.mark.parametrize("mime,filename,attributes,expected", [
    ("video/mp4", None, [], (".mp4", "video")),
    ("video/quicktime", "IMG_0001.MOV", [], (".mov", "video")),
    ("application/octet-stream", "IMG_0001.HEIC", [], (".heic", "photo")),
    ("application/octet-stream", r"C:\camera\clip.MKV", [], (".mkv", "video")),
    ("application/octet-stream", "../../test.JPG", [], (".jpg", "photo")),
    ("application/octet-stream", "CAMERA.DNG", [], (".dng", "photo")),
    ("VIDEO/MP4; codecs=avc1", "clip.mp4", [], (".mp4", "video")),
    ("image/jpeg", "photo.png.exe", [], (".jpg", "photo")),
    ("video/mp4", "clip.jpg", [], (".mp4", "video")),
    ("image/gif", "motion.gif", [], (".gif", "animation")),
    ("video/mp4", None, [tg.DocumentAttributeAnimated()], (".mp4", "animation")),
    ("video/mp4", None, [tg.DocumentAttributeVideo(10, 240, 240, round_message=True)], (".mp4", "video_note")),
    ("application/octet-stream", None, [tg.DocumentAttributeVideo(5, 1280, 720)], (".mp4", "video")),
    ("image/jpeg", "scan.jpeg", [], (".jpeg", "photo")),
])
def test_media_document_formats(mime, filename, attributes, expected):
    msg = document_message(10, mime, filename, attributes)
    items, reason = attachments(msg)
    assert reason is None
    assert len(items) == 1
    assert (items[0].extension, items[0].kind) == expected
    assert items[0].target is msg


@pytest.mark.parametrize("mime,filename,attributes,reason", [
    ("application/pdf", "document.jpg", [], "unsupported_file"),
    ("application/zip", "album.zip", [], "unsupported_file"),
    ("application/octet-stream", "photo.jpg.exe", [], "unsupported_file"),
    ("image/svg+xml", "image.svg", [], "unsupported_file"),
    ("audio/mpeg", "music.mp3", [], "audio"),
    ("video/mp4", "voice.mp4", [tg.DocumentAttributeAudio(5, voice=True)], "audio"),
    ("video/webm", "sticker.webm", [tg.DocumentAttributeSticker("", tg.InputStickerSetEmpty())], "sticker"),
    ("image/webp", "emoji.webp", [tg.DocumentAttributeCustomEmoji("", tg.InputStickerSetEmpty())], "sticker"),
])
def test_non_visual_documents_have_explicit_skip_reason(mime, filename, attributes, reason):
    assert attachments(document_message(10, mime, filename, attributes)) == ([], reason)


def test_other_message_types_spoilers_and_forwarded_media():
    msg = message(1)
    msg.media.spoiler = True
    msg.fwd_from = SimpleNamespace()
    assert attachments(msg)[0][0].kind == "photo"
    msg.media.ttl_seconds = 5
    assert attachments(msg) == ([], "self_destructing")
    msg.media = MessageMediaPhoto(photo=tg.PhotoEmpty(1))
    assert attachments(msg) == ([], "unavailable")
    msg.media = tg.MessageMediaWebPage(tg.WebPageEmpty(1))
    assert attachments(msg) == ([], "link_preview")
    msg.media = tg.MessageMediaPaidMedia(100, [tg.MessageExtendedMediaPreview()])
    assert attachments(msg) == ([], "paid_media")
    msg.media = tg.MessageMediaStory(tg.PeerChannel(1), 1)
    assert attachments(msg) == ([], "story")
    msg.media = tg.MessageMediaContact("123", "Test", "", "", 1)
    assert attachments(msg) == ([], "other")
    msg.media = None
    assert attachments(msg) == ([], "text")
    msg.action = tg.MessageActionChatDeletePhoto()
    assert attachments(msg) == ([], "service")


async def test_mixed_gallery_across_batches_downloads_every_item_and_retries(worker, monkeypatch):
    add_chat(worker)
    group = 9223372036854775000
    photo = message(1000, payload=b"photo")
    video = document_message(1001, payload=b"movie")
    photo.grouped_id = video.grouped_id = group
    before = [message(i) for i in range(1, 1000)]
    for msg in before:
        msg.media = None
    client = TelegramFake([*before, photo, video], fail_id=1001)
    monkeypatch.setattr(worker, "authorized", AsyncMock(return_value=client))
    await worker.run("preview")
    result = worker.report["chats"][0]
    assert (result["new"], result["albums"], result["album_items"]) == (2, 1, 2)
    assert result["types"] == {"photo": 1, "video": 1}
    assert result["skipped"] == {"text": 999}
    assert not client.downloaded
    await worker.run("download")
    assert worker.store.one("SELECT cursor FROM chats")["cursor"] == 1000
    client.fail_id = None
    await worker.run("download")
    assert client.downloaded == [1000, 1001, 1001]
    assert worker.store.one("SELECT cursor FROM chats")["cursor"] == 1001
    sources = worker.store.rows("SELECT * FROM sources ORDER BY message_id")
    assert [row["kind"] for row in sources] == ["photo", "video"]
    assert all(row["grouped_id"] == str(group) for row in sources)
    assert len(list(worker.photos.rglob("*.mp4"))) == 1


async def test_live_photo_second_part_failure_keeps_message_pending(worker):
    add_chat(worker)
    msg = message(1, payload=b"still-photo")
    msg.media.live_photo = True
    clip = document_message(20, payload=b"live-motion").document
    clip.payload = b"live-motion"
    msg.media.video = clip
    client = TelegramFake([msg], fail_id=20)
    items, reason = attachments(msg)
    assert reason is None
    assert [item.part for item in items] == ["main", "live_video"]
    with pytest.raises(ConnectionError):
        await worker.download_chat(client, 1, worker.store.one("SELECT * FROM chats"))
    assert worker.store.one("SELECT cursor FROM chats")["cursor"] == 0
    assert len(worker.store.rows("SELECT * FROM sources")) == 1
    client.fail_id = None
    await worker.download_chat(client, 1, worker.store.one("SELECT * FROM chats"))
    assert client.downloaded == [1, 20, 20]
    assert len(worker.store.rows("SELECT * FROM sources")) == 2
    assert worker.store.one("SELECT cursor FROM chats")["cursor"] == 1
    assert len(list(worker.photos.rglob("*.jpg"))) == 1
    assert len(list(worker.photos.rglob("*.mp4"))) == 1


async def test_video_processing_pending_does_not_advance_cursor(worker):
    chat = add_chat(worker)
    msg = document_message(1)
    msg.video_processing_pending = True
    client = TelegramFake([msg])
    with pytest.raises(ValueError, match="не подготовил"):
        await worker.download_chat(client, 1, chat)
    assert not client.downloaded
    assert worker.store.one("SELECT cursor FROM chats")["cursor"] == 0


async def test_stop_between_live_photo_parts_preserves_retry(worker):
    chat = add_chat(worker)
    msg = message(1)
    msg.media.live_photo = True
    msg.media.video = document_message(20).document
    client = TelegramFake([msg])
    download = client.download_media

    async def stop_after_file(*args, **kwargs):
        result = await download(*args, **kwargs)
        worker.stop = True
        return result

    client.download_media = stop_after_file
    await worker.download_chat(client, 1, chat)
    assert client.downloaded == [1]
    assert worker.store.one("SELECT cursor FROM chats")["cursor"] == 0
    assert worker.store.one("SELECT part FROM sources")["part"] == "main"


async def test_new_media_respects_topic_filter(worker, monkeypatch):
    add_chat(worker)
    worker.store.execute("UPDATE chats SET topic_ids='[50]'")
    photo_file = document_message(1, "application/octet-stream", "photo.HEIC", payload=b"heic")
    video = document_message(2, payload=b"video")
    gif = document_message(3, "image/gif", "dance.gif", payload=b"gif")
    for msg in (photo_file, video):
        msg.reply_to = MessageReplyHeader(forum_topic=True, reply_to_msg_id=50)
    gif.reply_to = MessageReplyHeader(forum_topic=True, reply_to_msg_id=60)
    client = TelegramFake([photo_file, video, gif])
    monkeypatch.setattr(worker, "authorized", AsyncMock(return_value=client))
    await worker.run("preview")
    assert worker.report["chats"][0]["types"] == {"photo": 1, "video": 1}
    await worker.run("download")
    assert client.downloaded == [1, 2]


async def test_video_upload_stream_has_correct_type_and_each_part_retries_album(worker, monkeypatch):
    add_chat(worker)
    worker.store.record(1, 1, "photo", "photo.jpg", "2026-10-20", part="main")
    worker.store.record(1, 1, "video", "video.mp4", "2026-10-20", part="live_video", kind="video")
    (worker.photos / "photo.jpg").write_bytes(b"still")
    (worker.photos / "video.mp4").write_bytes(b"movie")
    uploaded, added = [], []
    fail_video = True

    def handle(request):
        if request.url.path == "/api/assets":
            data = request.read()
            kind = "video" if b"movie" in data else "photo"
            uploaded.append(kind)
            assert (b"Content-Type: video/mp4" if kind == "video" else b"Content-Type: image/jpeg") in data
            return httpx.Response(201, json={"id": kind})
        if request.url.path == "/api/albums":
            return httpx.Response(200, json=[{"id": "album", "albumName": "Telegram · Чат 1 [1]"}])
        if request.url.path == "/api/albums/album/assets":
            asset = json.loads(request.content)["ids"][0]
            added.append(asset)
            if fail_video and asset == "video":
                return httpx.Response(503)
            return httpx.Response(200, json=[{"id": asset, "success": True}])
        raise AssertionError(request.url)

    monkeypatch.setattr(worker, "immich", lambda: Immich(
        "http://immich.test", "test-key", httpx.MockTransport(handle)
    ))
    await worker.upload()
    assert worker.store.one("SELECT album_added FROM sources WHERE part='main'")["album_added"] == 1
    assert worker.store.one("SELECT album_added FROM sources WHERE part='live_video'")["album_added"] == 0
    fail_video = False
    await worker.upload()
    assert uploaded == ["photo", "video"]
    assert added == ["photo", "video", "video"]


async def test_local_video_supports_authenticated_range_requests(web):
    app, client = web
    await sign_in(client)
    add_chat(app.state.worker)
    path = app.state.worker.photos / "video.mp4"
    path.write_bytes(b"0123456789")
    app.state.store.record(1, 1, "video", "video.mp4", "2026-10-20", kind="video", grouped_id=2**63 - 1)
    response = await client.get("/api/media/video", headers={"Range": "bytes=2-5"})
    assert response.status_code == 206
    assert response.content == b"2345"
    assert response.headers["content-type"] == "video/mp4"
    assert response.headers["content-range"] == "bytes 2-5/10"
    assert "media-src 'self'" in response.headers["content-security-policy"]
    recent = (await client.get("/api/recent")).json()
    assert recent[0]["kind"] == "video"
    assert recent[0]["grouped_id"] == str(2**63 - 1)


def test_media_upgrade_preserves_upload_history_and_rescans_only_once(tmp_path):
    root = tmp_path / "old-media"
    root.mkdir()
    with sqlite3.connect(root / "photos.sqlite3") as db:
        db.executescript("""
            CREATE TABLE chats(id INTEGER PRIMARY KEY, title TEXT NOT NULL, selected INTEGER DEFAULT 0,
                since TEXT NOT NULL, until_date TEXT, cursor INTEGER DEFAULT 0, album_id TEXT, error TEXT);
            CREATE TABLE media(id TEXT PRIMARY KEY, path TEXT NOT NULL, created_at TEXT NOT NULL,
                asset_id TEXT, error TEXT);
            CREATE TABLE sources(chat_id INTEGER REFERENCES chats(id), message_id INTEGER,
                media_id TEXT REFERENCES media(id), sent_at TEXT, album_added INTEGER,
                PRIMARY KEY(chat_id,message_id));
            INSERT INTO chats(id,title,selected,since,cursor) VALUES(1,'Группа',1,'2026-10-01',123);
            INSERT INTO media VALUES('old','old.jpg','2026-10-01','immich-asset',NULL);
            INSERT INTO sources VALUES(1,2,'old','2026-10-01',1);
        """)
    store = Store(root)
    assert store.one("SELECT cursor FROM chats")["cursor"] == 0
    assert store.one("SELECT * FROM sources")["part"] == "main"
    assert store.one("SELECT * FROM sources")["album_added"] == 1
    assert store.one("SELECT * FROM media")["asset_id"] == "immich-asset"
    assert store.rows("PRAGMA foreign_key_check") == []
    store.execute("UPDATE chats SET cursor=777")
    store.db.close()
    store = Store(root)
    assert store.one("SELECT cursor FROM chats")["cursor"] == 777
    store.db.close()
