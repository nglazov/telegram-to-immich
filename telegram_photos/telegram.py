"""Keep Telegram rate-limit retries inside the current RPC/file chunk."""
import asyncio
import logging

from telethon import TelegramClient, errors, functions

FLOOD_ERRORS = (errors.FloodWaitError, errors.FloodPremiumWaitError)


class ImportStopped(Exception):
    """A requested stop while waiting, without reporting an import failure."""


def request_name(request):
    # Never stringify a request: it can contain access hashes and file references.
    while hasattr(request, "query"):
        request = request.query
    return type(request).__name__


class TransferClient(TelegramClient):
    transfer_worker = None

    async def _call(self, sender, request, ordered=False, flood_sleep_threshold=None):
        worker = self.transfer_worker
        managed = worker is not None and worker.retry_task is asyncio.current_task()
        # Telethon's download iterator calls _call directly, bypassing its
        # Takeout proxy. Wrap media RPCs here, including on an exported DC sender.
        original = request
        takeout = managed and worker.takeout_active and isinstance(request, functions.upload.GetFileRequest)
        if takeout:
            request = functions.InvokeWithTakeoutRequest(self.session.takeout_id, request)
        while True:
            try:
                return await TelegramClient._call(
                    self, sender, request, ordered=ordered, flood_sleep_threshold=0,
                )
            except FLOOD_ERRORS as exc:
                if not managed or worker.retrying_deferred:
                    raise
                await worker.telegram_wait(exc, request_name(request))
            except (errors.RpcCallFailError, errors.ServerError) as exc:
                if not takeout:
                    logging.getLogger(__name__).warning(
                        "Telegram RPC failed kind=%s request=%s", type(exc).__name__, request_name(request),
                    )
                    raise
                # Only fall back for a server failure after Telethon's retries.
                # A FloodWait never changes transport to evade the limit.
                worker.takeout_failed(exc, request_name(request))
                request, takeout = original, False
