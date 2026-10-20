import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import dotenv_values, set_key

from .immich import api_url

PROJECT = Path(__file__).resolve().parent.parent
ENV_FILE = PROJECT / ".env"


@dataclass(frozen=True)
class Config:
    app_password: str = field(default="", repr=False)
    app_origin: str = ""
    telegram_api_id: int | None = None
    telegram_api_hash: str = field(default="", repr=False)
    immich_url: str = ""
    immich_api_key: str = field(default="", repr=False)
    dry_run: bool = True

    @property
    def telegram_configured(self):
        return bool(self.telegram_api_id and self.telegram_api_hash)

    @property
    def immich_configured(self):
        return bool(self.immich_url and self.immich_api_key)

    @classmethod
    def load(cls, env_file=ENV_FILE):
        # Environment supplied by Docker/systemd takes precedence over the local file.
        values = {**dotenv_values(env_file, interpolate=False), **os.environ}
        app_id = (values.get("TELEGRAM_API_ID") or "").strip()
        app_hash = (values.get("TELEGRAM_API_HASH") or "").strip()
        if app_id and (not app_id.isdigit() or int(app_id) < 1):
            raise ValueError("TELEGRAM_API_ID должен быть положительным числом.")
        if app_hash and not re.fullmatch(r"[a-fA-F0-9]{32}", app_hash):
            raise ValueError("TELEGRAM_API_HASH должен содержать 32 шестнадцатеричных символа.")
        url = (values.get("IMMICH_URL") or "").strip()
        dry_run = (values.get("DRY_RUN") or "true").lower()
        if dry_run not in {"true", "false", "1", "0"}:
            raise ValueError("DRY_RUN должен быть true или false.")
        return cls(
            app_password=values.get("APP_PASSWORD") or "",
            app_origin=(values.get("APP_ORIGIN") or "").rstrip("/"),
            telegram_api_id=int(app_id) if app_id else None,
            telegram_api_hash=app_hash,
            immich_url=api_url(url) if url else "",
            immich_api_key=values.get("IMMICH_API_KEY") or "",
            dry_run=dry_run in {"true", "1"},
        )


def migrate_credentials(store, env_file=ENV_FILE):
    """Preserve legacy credentials in the private env file before removing SQLite copies."""
    mapping = {
        "api_id": "TELEGRAM_API_ID", "api_hash": "TELEGRAM_API_HASH",
        "immich_url": "IMMICH_URL", "immich_key": "IMMICH_API_KEY",
    }
    legacy = {old: store.get(old) for old in mapping}
    if not any(legacy.values()) and not store.get("cookie_secret"):
        return
    values = {**dotenv_values(env_file, interpolate=False), **os.environ}
    missing = [(mapping[key], str(value)) for key, value in legacy.items() if value and not values.get(mapping[key])]
    if missing:
        env_file = Path(env_file)
        env_file.touch(mode=0o600, exist_ok=True)
        env_file.chmod(0o600)
        for key, value in missing:
            set_key(str(env_file), key, value)
    store.db.execute("PRAGMA secure_delete=ON")
    store.execute(
        "DELETE FROM settings WHERE key IN ('api_id','api_hash','immich_url','immich_key','cookie_secret')"
    )
    store.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    store.db.execute("VACUUM")
