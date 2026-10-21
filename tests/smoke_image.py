"""Run with: docker run --rm -i --network none IMAGE python - < tests/smoke_image.py"""

import asyncio
import os
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx

from telegram_photos.app import make_app
from telegram_photos.config import Config


async def main():
    assert os.geteuid() != 0, "Check the image as its non-root application user"
    with TemporaryDirectory() as directory:
        root = Path(directory)
        app = make_app(
            state_dir=root / "state", photos_dir=root / "photos",
            config=Config(app_password="image-smoke-test-only"), env_file=root / ".env",
        )
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://testserver",
            ) as client:
                for path, content_type in [
                    ("/", "text/html"), ("/static/app.js", "javascript"),
                    ("/static/style.css", "text/css"), ("/health", "application/json"),
                ]:
                    response = await client.get(path)
                    assert response.status_code == 200, f"{path}: HTTP {response.status_code}"
                    assert content_type in response.headers["content-type"], path
                    assert response.content, path
                    print(f"{path}: HTTP 200", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
