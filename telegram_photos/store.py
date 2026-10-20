import json
import sqlite3
from pathlib import Path


class Store:
    """Single-process durable queue. State is local; large media can live on NAS."""

    def __init__(self, root: Path):
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(root / "photos.sqlite3")
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
          PRAGMA journal_mode=WAL;
          PRAGMA foreign_keys=ON;
          CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS chats(
            id INTEGER PRIMARY KEY, title TEXT NOT NULL, selected INTEGER NOT NULL DEFAULT 0,
            since TEXT NOT NULL, until_date TEXT, cursor INTEGER NOT NULL DEFAULT 0,
            album_id TEXT, error TEXT
          );
          CREATE TABLE IF NOT EXISTS media(
            id TEXT PRIMARY KEY, path TEXT NOT NULL, created_at TEXT NOT NULL,
            asset_id TEXT, error TEXT
          );
          CREATE TABLE IF NOT EXISTS sources(
            chat_id INTEGER NOT NULL REFERENCES chats(id),
            message_id INTEGER NOT NULL, media_id TEXT NOT NULL REFERENCES media(id),
            sent_at TEXT NOT NULL, album_added INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(chat_id, message_id)
          );
          CREATE INDEX IF NOT EXISTS sources_media ON sources(media_id);
        """)
        # Additive migration: existing chat choices, cursors and media stay intact.
        columns = {row["name"] for row in self.db.execute("PRAGMA table_info(chats)")}
        with self.db:
            for name, definition in {
                "is_forum": "INTEGER NOT NULL DEFAULT 0",
                "topics": "TEXT NOT NULL DEFAULT '[]'",
                "topic_ids": "TEXT",
                "topics_error": "TEXT",
            }.items():
                if name not in columns:
                    self.db.execute(f"ALTER TABLE chats ADD COLUMN {name} {definition}")
        columns = {row["name"] for row in self.db.execute("PRAGMA table_info(sources)")}
        with self.db:
            if "part" not in columns:
                self.db.execute("""
                    CREATE TABLE sources_v2(
                        chat_id INTEGER NOT NULL REFERENCES chats(id),
                        message_id INTEGER NOT NULL, part TEXT NOT NULL DEFAULT 'main',
                        media_id TEXT NOT NULL REFERENCES media(id), sent_at TEXT NOT NULL,
                        album_added INTEGER NOT NULL DEFAULT 0,
                        kind TEXT NOT NULL DEFAULT 'photo', grouped_id TEXT,
                        PRIMARY KEY(chat_id, message_id, part)
                    )
                """)
                self.db.execute("""
                    INSERT INTO sources_v2(chat_id,message_id,media_id,sent_at,album_added)
                    SELECT chat_id,message_id,media_id,sent_at,album_added FROM sources
                """)
                self.db.execute("DROP TABLE sources")
                self.db.execute("ALTER TABLE sources_v2 RENAME TO sources")
                self.db.execute("CREATE INDEX sources_media ON sources(media_id)")
            if self.get("media_scan_version", 0) < 2:
                # Older runs skipped videos. Revisit history once, retaining deduplication.
                self.db.execute("UPDATE chats SET cursor=0")
                for key, value in {"media_scan_version": 2, "auto_sync": False, "last_report": None}.items():
                    self.db.execute(
                        "INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (key, json.dumps(value))
                    )

    def rows(self, sql, args=()):
        return [dict(r) for r in self.db.execute(sql, args).fetchall()]

    def one(self, sql, args=()):
        r = self.db.execute(sql, args).fetchone()
        return dict(r) if r else None

    def execute(self, sql, args=()):
        with self.db:
            self.db.execute(sql, args)

    def get(self, key, default=None):
        row = self.one("SELECT value FROM settings WHERE key=?", (key,))
        return json.loads(row["value"]) if row else default

    def set(self, key, value):
        self.execute(
            "INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value)),
        )

    def record(self, chat_id, message_id, media_id, path, sent_at, *,
               part="main", kind="photo", grouped_id=None, advance_cursor=True):
        with self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO media(id,path,created_at) VALUES(?,?,?)",
                (media_id, path, sent_at),
            )
            self.db.execute(
                """INSERT OR IGNORE INTO sources(chat_id,message_id,part,media_id,sent_at,kind,grouped_id)
                   VALUES(?,?,?,?,?,?,?)""",
                (chat_id, message_id, part, media_id, sent_at, kind,
                 str(grouped_id) if grouped_id is not None else None),
            )
            if advance_cursor:
                self.db.execute("UPDATE chats SET cursor=? WHERE id=?", (message_id, chat_id))
