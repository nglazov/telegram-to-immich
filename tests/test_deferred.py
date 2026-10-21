import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from telethon import TelegramClient, errors, functions

from telegram_photos.calendar import day_map
from telegram_photos.config import Config
from telegram_photos.store import Store
from telegram_photos.telegram import ImportStopped, TransferClient
import test_importer as fixtures
from test_importer import TelegramFake, add_chat, message, sign_in

worker = fixtures.worker
web = fixtures.web


def defer(worker, message_id=1, part='main'):
    worker.store.defer(1, message_id, part=part, day='2026-10-20', kind='photo', reason='Повторить позже')
    return worker.store.one('SELECT * FROM deferred')['id']


async def test_deferred_skipped_after_restart_and_rescan_but_not_counted_complete(worker, monkeypatch):
    chat = add_chat(worker)
    defer(worker)
    telegram = TelegramFake([message(1), message(2, payload=b'next')])
    await worker.download_chat(telegram, 1, chat)
    assert telegram.downloaded == [2]
    assert worker.store.one('SELECT cursor FROM chats')['cursor'] == 2
    worker.store.db.close()
    worker.store = Store(worker.state)
    worker.store.execute('UPDATE chats SET cursor=0')
    await worker.download_chat(telegram, 1, worker.store.one('SELECT * FROM chats'))
    assert telegram.downloaded == [2]
    days = day_map(worker.store, today='2026-10-21')['chats'][0]['days']
    day = next(d for d in days if d['day'] == '2026-10-20')
    assert day['covered'] and day['state'] == 'pending' and day['deferred'] == 1
    assert worker.store.deferred_files()['total'] == 1
    worker.store.db.close()
    worker.store = Store(worker.state)


async def test_retry_fetches_fresh_message_and_resolves_queue_without_rewinding_cursor(worker, monkeypatch):
    add_chat(worker)
    row_id = defer(worker)
    worker.store.execute('UPDATE chats SET cursor=100')
    telegram = TelegramFake([])
    telegram.get_messages = AsyncMock(return_value=message(1))
    monkeypatch.setattr(worker, 'authorized', AsyncMock(return_value=telegram))
    await worker.run('download', deferred_id=row_id)
    assert telegram.get_messages.call_args.kwargs == {'ids': 1}
    assert telegram.downloaded == [1] and not worker.error
    assert worker.store.one('SELECT cursor FROM chats')['cursor'] == 100
    assert not worker.store.rows('SELECT * FROM deferred')
    assert worker.store.one('SELECT message_id FROM sources')['message_id'] == 1


async def test_failed_retry_is_bounded_and_keeps_reason_deadline_and_cursor(worker, monkeypatch):
    add_chat(worker)
    row_id = defer(worker)
    worker.store.execute('UPDATE chats SET cursor=100')
    telegram = TelegramFake([])
    telegram.get_messages = AsyncMock(return_value=message(1))
    telegram.download_media = AsyncMock(side_effect=errors.FloodWaitError(None, capture=60))
    monkeypatch.setattr(worker, 'authorized', AsyncMock(return_value=telegram))
    await worker.run('download', deferred_id=row_id)
    row = worker.store.one('SELECT * FROM deferred')
    assert row['attempts'] == 1 and row['last_attempt_at'] and '60' in row['reason']
    assert worker.store.get('telegram_retry_after') >= datetime.now(UTC).timestamp() + 59
    assert worker.store.one('SELECT cursor FROM chats')['cursor'] == 100
    assert not worker.store.rows('SELECT * FROM sources')
    telegram.download_media.assert_awaited_once()
    assert not list((worker.photos / '.partial').iterdir())


async def test_managed_deferred_rpc_does_not_loop_forever(worker, monkeypatch):
    client = TransferClient(None, 123, 'a' * 32)
    client.transfer_worker = worker
    worker.retry_task = asyncio.current_task()
    worker.retrying_deferred = True
    call = AsyncMock(side_effect=errors.FloodWaitError(None, capture=5))
    monkeypatch.setattr(TelegramClient, '_call', call)
    with pytest.raises(errors.FloodWaitError):
        await client._call(None, functions.help.GetConfigRequest())
    call.assert_awaited_once()
    await client.disconnect()


@pytest.mark.parametrize('case', ['missing', 'protected', 'outside_dates', 'unselected'])
async def test_retry_preserves_unavailable_or_out_of_scope_file(worker, monkeypatch, case):
    add_chat(worker)
    row_id = defer(worker)
    msg = message(1)
    if case == 'missing':
        msg = None
    if case == 'protected':
        msg.noforwards = True
    if case == 'outside_dates':
        worker.store.execute("UPDATE chats SET since='2026-10-21'")
    if case == 'unselected':
        worker.store.execute('UPDATE chats SET selected=0')
    telegram = TelegramFake([])
    telegram.get_messages = AsyncMock(return_value=msg)
    monkeypatch.setattr(worker, 'authorized', AsyncMock(return_value=telegram))
    await worker.run('download', deferred_id=row_id)
    assert worker.error and worker.store.one('SELECT * FROM deferred')
    assert not telegram.downloaded


async def test_pause_between_files_and_responsive_stop(worker):
    chat = add_chat(worker)
    worker.config = replace(worker.config, telegram_download_delay=0.03)
    telegram = TelegramFake([message(1), message(2, payload=b'two')])
    times = []
    original = telegram.download_media
    async def download(*args, **kwargs):
        times.append(asyncio.get_running_loop().time())
        return await original(*args, **kwargs)
    telegram.download_media = download
    await worker.download_chat(telegram, 1, chat)
    assert times[1] - times[0] >= 0.03
    worker.next_download_at = asyncio.get_running_loop().time() + 300
    worker.stop = True
    with pytest.raises(ImportStopped):
        await worker.download_message(telegram, 1, message(3), '.jpg')
    assert telegram.downloaded == [1, 2]


async def test_deferred_api_requires_login_explicit_mode_and_idle_worker(web, monkeypatch):
    app, client = web
    w = app.state.worker
    add_chat(w)
    row_id = defer(w)
    w.store.set('telegram_connected', True)
    path = f'/api/deferred/{row_id}/retry'
    assert (await client.get('/api/deferred')).status_code == 401
    assert (await client.post(path, json={'mode': 'download'})).status_code == 401
    await sign_in(client)
    assert (await client.get('/api/deferred')).json()['total'] == 1
    assert (await client.post(path, json={})).status_code == 400
    assert (await client.post(path, json={'mode': 'download'}, headers={'X-Requested-With': ''})).status_code == 403
    telegram = TelegramFake([])
    telegram.get_messages = AsyncMock(return_value=message(1))
    monkeypatch.setattr(w, 'authorized', AsyncMock(return_value=telegram))
    w.task = asyncio.create_task(asyncio.sleep(300))
    assert (await client.post(path, json={'mode': 'download'})).status_code == 409
    w.task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await w.task
    response = await client.post(path, json={'mode': 'download'})
    assert response.status_code == 200
    await w.task
    assert (await client.get('/api/deferred')).json()['total'] == 0
    assert (await client.post(path, json={'mode': 'download'})).status_code == 404


@pytest.mark.parametrize('value', ['-1', '301', 'nan', 'inf', 'bad'])
def test_invalid_pause_rejected(tmp_path, monkeypatch, value):
    monkeypatch.setenv('TELEGRAM_DOWNLOAD_DELAY_SECONDS', value)
    with pytest.raises(ValueError, match='TELEGRAM_DOWNLOAD_DELAY_SECONDS'):
        Config.load(tmp_path / '.env')


def test_pause_from_env_and_default(tmp_path, monkeypatch):
    monkeypatch.delenv('TELEGRAM_DOWNLOAD_DELAY_SECONDS', raising=False)
    assert Config.load(tmp_path / '.env').telegram_download_delay == 3
    monkeypatch.setenv('TELEGRAM_DOWNLOAD_DELAY_SECONDS', '2.5')
    assert Config.load(tmp_path / '.env').telegram_download_delay == 2.5


async def test_deferred_live_photo_part_does_not_skip_other_part(worker, monkeypatch):
    from telegram_photos.media import Attachment
    add_chat(worker)
    photo, clip = message(1, payload=b'photo'), message(1, payload=b'clip')
    items = [Attachment('main', '.jpg', 'photo', photo), Attachment('live_video', '.mp4', 'video', clip)]
    monkeypatch.setattr('telegram_photos.worker.attachments', lambda msg: (items, None))
    row_id = defer(worker, part='live_video')
    telegram = TelegramFake([photo])
    telegram.get_messages = AsyncMock(return_value=photo)
    monkeypatch.setattr(worker, 'authorized', AsyncMock(return_value=telegram))
    await worker.download_chat(telegram, 1, worker.store.one('SELECT * FROM chats'))
    assert worker.store.one('SELECT part FROM sources')['part'] == 'main'
    assert worker.store.one('SELECT part FROM deferred')['part'] == 'live_video'
    await worker.run('download', deferred_id=row_id)
    assert {s['part'] for s in worker.store.rows('SELECT part FROM sources')} == {'main', 'live_video'}
    assert len(worker.store.rows('SELECT * FROM media')) == 2
    assert not worker.store.rows('SELECT * FROM deferred')
    assert worker.store.one('SELECT cursor FROM chats')['cursor'] == 1


async def test_preview_reports_deferred_without_downloading_or_changing_queue(worker, monkeypatch):
    add_chat(worker)
    defer(worker)
    telegram = TelegramFake([message(1), message(2)])
    monkeypatch.setattr(worker, 'authorized', AsyncMock(return_value=telegram))
    before = worker.store.rows('SELECT * FROM deferred')
    await worker.run('preview')
    report = worker.report['chats'][0]
    assert report['deferred'] == 1 and report['new'] == 1 and report['known'] == 0
    assert worker.store.rows('SELECT * FROM deferred') == before
    assert not telegram.downloaded and worker.store.one('SELECT cursor FROM chats')['cursor'] == 0
