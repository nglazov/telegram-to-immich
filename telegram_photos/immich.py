import hashlib
import mimetypes
from pathlib import Path
from urllib.parse import urlsplit

import httpx


def api_url(url: str) -> str:
    url = url.strip().rstrip("/")
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
        raise ValueError("Укажите HTTP(S)-адрес Immich без логина и пароля в ссылке.")
    if parts.query or parts.fragment or parts.path not in {"", "/api"}:
        raise ValueError("Адрес Immich должен оканчиваться на /api или не содержать путь.")
    return url if parts.path == "/api" else url + "/api"


class Immich:
    def __init__(self, url, key, transport=None):
        self.http = httpx.AsyncClient(
            base_url=api_url(url) + "/",
            headers={"x-api-key": key},
            timeout=httpx.Timeout(120, connect=10),
            follow_redirects=False,
            transport=transport,
        )

    async def close(self):
        await self.http.aclose()

    async def request(self, method, path, **kwargs):
        response = await self.http.request(method, path, **kwargs)
        response.raise_for_status()
        return response.json() if response.content else None

    async def upload(self, media, path: Path):
        # A byte-identical retry is idempotent in Immich, including when the
        # first response was lost after the server had accepted the upload.
        with path.open("rb") as stream:
            checksum = hashlib.file_digest(stream, "sha1").hexdigest()
            stream.seek(0)
            return await self.request(
                "POST", "assets",
                headers={"x-immich-checksum": checksum},
                data={"fileCreatedAt": media["created_at"], "fileModifiedAt": media["created_at"]},
                files={"assetData": (
                    path.name, stream, mimetypes.guess_type(path.name)[0] or "application/octet-stream"
                )},
            )

    async def album(self, title, chat_id):
        name = f"Telegram · {title} [{chat_id}]"
        albums = await self.request("GET", "albums", params={"isOwned": "true"})
        existing = next((a for a in albums if a["albumName"] == name), None)
        if existing:
            return existing["id"]
        result = await self.request("POST", "albums", json={"albumName": name})
        return result["id"]

    async def add_to_album(self, album_id, asset_id):
        result = await self.request("PUT", f"albums/{album_id}/assets", json={"ids": [asset_id]})
        if not result or any(not r["success"] and r.get("error") != "duplicate" for r in result):
            raise ValueError("Immich не подтвердил добавление в альбом. Повторите синхронизацию.")
