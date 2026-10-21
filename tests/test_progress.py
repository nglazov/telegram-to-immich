import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telethon import TelegramClient, errors, functions, types

from telegram_photos.calendar import day_map
from telegram_photos.config import Config
from telegram_photos.store import Store
from telegram_photos.telegram import ImportStopped, TransferClient
from telegram_photos.worker import Worker


@pytest.fixture
def worker(tmp_path):
    store = Store(tmp_path / 'state')
    result = Worker(store, tmp_path / 'state', tmp_path / 'photos', Config(telegram_takeout=False, telegram_download_delay=0))
    store.execute("INSERT INTO chats(id,title,selected,since) VALUES(1,'Test',1,'2026-10-01')")
    yield result
    store.db.close()


@pytest.fixture
def clock(monkeypatch):
    import telegram_photos.worker as module

    class Clock(datetime):
        elapsed = 0
        @classmethod
        def now(cls, tz=None):
            return (datetime(2026, 10, 21, tzinfo=UTC) + timedelta(seconds=cls.elapsed)).astimezone(tz)

    async def sleep(seconds):
        Clock.elapsed += seconds

    monkeypatch.setattr(module, 'datetime', Clock)
    monkeypatch.setattr(module.asyncio, 'sleep', sleep)
    return Clock


async def test_download_retries_same_chunk_with_takeout_and_increasing_pause(worker, monkeypatch, clock):
    client = TransferClient(None, 123, 'a' * 32, receive_updates=False)
    client.transfer_worker = worker
    client.session.takeout_id = 42
    worker.retry_task = asyncio.current_task()
    worker.takeout_active = True
    attempts, progress = [], []

    async def call(self, sender, request, **kwargs):
        assert isinstance(request, functions.InvokeWithTakeoutRequest)
        assert request.takeout_id == 42
        offset = request.query.offset
        attempts.append((offset, clock.elapsed))
        if len(attempts) == 2:
            raise errors.FloodWaitError(request=request, capture=5)
        if len(attempts) == 3:
            raise errors.FloodPremiumWaitError(request=request, capture=7)
        return types.upload.File(types.storage.FileUnknown(), 0, b'a' * 4096 if offset < 8192 else b'')

    monkeypatch.setattr(TelegramClient, '_call', call)
    location = types.InputDocumentFileLocation(1, 0, b'private-reference', '')
    output = worker.photos / 'chunked.bin'
    await client.download_file(location, output, file_size=8192, part_size_kb=4,
                               progress_callback=lambda n, total: progress.append(n))
    assert attempts == [(0, 0), (4096, 0), (4096, 30), (4096, 90), (8192, 90)]
    assert progress[:2] == [4096, 8192] and progress[-1] == 8192
    assert output.read_bytes() == b'a' * 8192
    wait = worker.store.get('telegram_wait')
    assert wait['kind'] == 'FloodPremiumWaitError' and wait['request'] == 'GetFileRequest'
    assert wait['attempt'] == 2 and 'private-reference' not in str(wait)
    worker.transfer_progress()
    worker.record_wait(errors.FloodWaitError(None, capture=1200))
    assert worker.store.get('telegram_retry_after') == clock.now(UTC).timestamp() + 1200
    assert worker.store.get('telegram_wait_attempt') == 1
    await client.disconnect()


async def test_wait_stop_keeps_deadline_and_does_not_retry_rpc(worker, monkeypatch, clock):
    client = TransferClient(None, 123, 'a' * 32)
    client.transfer_worker = worker
    worker.retry_task = asyncio.current_task()
    call = AsyncMock(side_effect=errors.FloodWaitError(None, capture=3600))
    monkeypatch.setattr(TelegramClient, '_call', call)

    async def stop(seconds):
        worker.stop = True

    monkeypatch.setattr(asyncio, 'sleep', stop)
    with pytest.raises(ImportStopped):
        await client._call(None, functions.help.GetConfigRequest())
    assert call.await_count == 1
    assert worker.store.get('telegram_retry_after') > clock.now(UTC).timestamp()
    await client.disconnect()


async def test_takeout_delay_visible_and_existing_session_reused(worker, monkeypatch, clock):
    worker.config = Config()
    client = TransferClient(None, 123, 'a' * 32)
    client.transfer_worker = worker
    worker.retry_task = asyncio.current_task()
    calls = []

    async def call(self, sender, request, **kwargs):
        calls.append(request)
        if len(calls) == 1:
            raise errors.TakeoutInitDelayError(request, capture=61)
        assert request.message_megagroups and not request.message_users and not request.contacts
        return SimpleNamespace(id=99)

    monkeypatch.setattr(TelegramClient, '_call', call)
    entities = [SimpleNamespace(megagroup=True)]
    await worker.prepare_takeout(client, entities)
    assert clock.elapsed == 0 and worker.store.get('takeout_wait')['kind'] == 'TakeoutInitDelayError'
    assert not worker.takeout_active and client.session.takeout_id is None
    assert worker.store.get('telegram_retry_after', 0) == 0
    assert await worker.wait_for_telegram()
    await worker.prepare_takeout(client, entities)
    assert len(calls) == 1  # Another file must not retry export initialization early.
    clock.elapsed = 61
    await worker.prepare_takeout(client, entities)
    assert worker.takeout_active and client.session.takeout_id == 99
    assert worker.store.get('takeout_retry_after') == 0
    await worker.prepare_takeout(client, entities)
    assert len(calls) == 2
    await client.disconnect()


def test_calendar_moscow_boundaries_coverage_empty_and_legacy(worker):
    worker.store.record(1, 5, 'hash', 'image.jpg', '2026-10-01T21:00:00+00:00')
    worker.store.execute("UPDATE media SET asset_id='immich'")
    worker.store.execute('UPDATE sources SET album_added=1')
    data = day_map(worker.store, today='2026-10-05')['chats'][0]['days']
    assert data[0]['state'] == 'unknown'
    assert data[1]['state'] == 'saved' and data[1]['downloaded'] == 1
    chat = worker.store.one('SELECT * FROM chats')
    worker.scan_before(chat, '2026-10-03')  # Legacy resumed cursor: earlier days stay unknown.
    worker.scan_before(chat, '2026-10-05')
    data = day_map(worker.store, {'chat_id': 1, 'day': '2026-10-05'}, today='2026-10-05')['chats'][0]['days']
    assert [d['state'] for d in data] == ['unknown', 'saved', 'empty', 'empty', 'active']


async def test_calendar_failed_file_cannot_complete_current_day(worker):
    class Fake:
        async def iter_messages(self, *args, **kwargs):
            yield SimpleNamespace(id=1, date=datetime(2026, 10, 2, tzinfo=UTC))
        async def download_media(self, *args, **kwargs):
            raise ConnectionError()

    from telegram_photos.media import Attachment
    from unittest.mock import patch
    with patch('telegram_photos.worker.attachments', return_value=([Attachment('main', '.jpg', 'photo', None)], None)):
        with pytest.raises(ConnectionError):
            await worker.download_chat(Fake(), 1, worker.store.one('SELECT * FROM chats'))
    days = day_map(worker.store, today='2026-10-03')['chats'][0]['days']
    assert [day['covered'] for day in days] == [True, False, False]
    assert worker.store.one('SELECT cursor FROM chats')['cursor'] == 0


async def test_idle_client_does_not_wait_during_login(worker, monkeypatch):
    client = TransferClient(None, 123, 'a' * 32)
    client.transfer_worker = worker
    monkeypatch.setattr(TelegramClient, '_call', AsyncMock(side_effect=errors.FloodWaitError(None, capture=500)))
    with pytest.raises(errors.FloodWaitError):
        await client._call(None, functions.help.GetConfigRequest())
    assert worker.store.get('telegram_wait') is None
    await client.disconnect()


@pytest.mark.parametrize('kind', ['TakeoutInitDelayError', 'FloodWaitError', 'FloodPremiumWaitError'])
def test_reopening_store_separates_only_takeout_deadline(tmp_path, kind):
    state = tmp_path / 'state'
    store = Store(state)
    store.execute("INSERT INTO chats(id,title,since,cursor) VALUES(1,'Chat','2025-09-01',3803)")
    store.set('telegram_retry_after', 2000000000)
    detail = {'kind': kind, 'retry_at': 2000000000}
    store.set('telegram_wait', detail)
    store.set('telegram_wait_attempt', 4)
    store.db.close()
    store = Store(state)
    if kind == 'TakeoutInitDelayError':
        assert store.get('takeout_retry_after') == 2000000000
        assert store.get('takeout_wait') == detail
        assert store.get('telegram_retry_after') == 0
        assert store.get('telegram_wait') is None
        # Reopening again must preserve a subsequent ordinary rate limit.
        store.set('telegram_wait', {'kind': 'FloodWaitError'})
        store.set('telegram_retry_after', 1900000000)
        store.db.close()
        store = Store(state)
        assert store.get('takeout_retry_after') == 2000000000
        assert store.get('telegram_retry_after') == 1900000000
    else:
        assert store.get('telegram_retry_after') == 2000000000
        assert store.get('telegram_wait') == detail
        assert store.get('telegram_wait_attempt') == 4
        assert store.get('takeout_retry_after') is None
    assert store.one('SELECT cursor FROM chats')['cursor'] == 3803
    store.db.close()


async def test_takeout_delay_allows_normal_download_but_flood_wait_is_respected(worker, monkeypatch, clock):
    worker.config = Config()
    client = TransferClient(None, 123, 'a' * 32)
    client.transfer_worker = worker
    worker.retry_task = asyncio.current_task()
    requests = []

    async def call(self, sender, request, **kwargs):
        requests.append((type(request).__name__, clock.elapsed))
        if isinstance(request, functions.account.InitTakeoutSessionRequest):
            raise errors.TakeoutInitDelayError(request, capture=86400)
        assert isinstance(request, functions.upload.GetFileRequest)  # No Takeout wrapper.
        if len(requests) == 2:
            raise errors.FloodWaitError(request, capture=47)
        return types.upload.File(types.storage.FileUnknown(), 0, b'complete')

    monkeypatch.setattr(TelegramClient, '_call', call)
    await worker.prepare_takeout(client, [SimpleNamespace(megagroup=True)])
    location = types.InputDocumentFileLocation(1, 0, b'', '')
    output = worker.photos / 'ordinary.bin'
    await client.download_file(location, output, part_size_kb=4)
    assert output.read_bytes() == b'complete'
    assert requests == [('InitTakeoutSessionRequest', 0), ('GetFileRequest', 0), ('GetFileRequest', 47)]
    assert worker.store.get('takeout_retry_after') > clock.now(UTC).timestamp()
    assert worker.store.get('telegram_retry_after') == 0
    await client.disconnect()


async def test_authorization_reads_premium_without_saving_identity(worker, monkeypatch):
    client = SimpleNamespace(is_user_authorized=AsyncMock(return_value=True),
                             get_me=AsyncMock(return_value=SimpleNamespace(premium=True, phone='private')))
    monkeypatch.setattr(worker, 'telegram', AsyncMock(return_value=client))
    assert await worker.authorized() is client
    assert worker.store.get('telegram_premium') is True
    assert 'private' not in str(worker.store.rows('SELECT * FROM settings'))


async def test_takeout_server_failure_retries_same_chunk_once_without_export(worker, monkeypatch):
    worker.config = Config()
    client = TransferClient(None, 123, 'a' * 32)
    client.transfer_worker = worker
    client.session.takeout_id = 99
    worker.retry_task = asyncio.current_task()
    worker.takeout_active = True
    calls = []
    original = functions.upload.GetFileRequest(types.InputDocumentFileLocation(1, 0, b'', ''), 4096, 4096)

    async def call(self, sender, request, **kwargs):
        calls.append(request)
        if isinstance(request, functions.InvokeWithTakeoutRequest):
            assert request.query is original
            raise errors.RpcCallFailError(request)
        assert request is original and request.offset == 4096
        return 'file-chunk'

    monkeypatch.setattr(TelegramClient, '_call', call)
    assert await client._call(None, original) == 'file-chunk'
    assert len(calls) == 2 and not worker.takeout_active
    await worker.prepare_takeout(client, [SimpleNamespace(megagroup=True)])
    assert len(calls) == 2 and worker.takeout_disabled_for_run
    assert client.session.takeout_id == 99  # Preserve approved export for a later run.
    assert worker.store.get('takeout_error')['kind'] == 'RpcCallFailError'
    monkeypatch.setattr(TelegramClient, '_call', AsyncMock(side_effect=errors.RpcCallFailError(None)))
    with pytest.raises(errors.RpcCallFailError):
        await client._call(None, original)  # Normal API errors must not spin forever.
    await client.disconnect()


def test_confirmed_takeout_does_not_restore_historical_wait(tmp_path):
    store = Store(tmp_path)
    store.set('telegram_wait', {'kind': 'TakeoutInitDelayError', 'retry_at': 2000000000})
    store.set('telegram_retry_after', 0)  # User confirmed and the retry succeeded.
    store.db.close()
    store = Store(tmp_path)
    assert store.get('takeout_retry_after') == 0
    assert store.get('telegram_retry_after') == 0
    store.db.close()
