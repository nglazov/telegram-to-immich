import json
import sqlite3
from datetime import UTC, datetime
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
          CREATE TABLE IF NOT EXISTS deferred(
            id INTEGER PRIMARY KEY, chat_id INTEGER NOT NULL REFERENCES chats(id),
            message_id INTEGER NOT NULL, part TEXT NOT NULL, day TEXT NOT NULL,
            kind TEXT NOT NULL, size_bytes INTEGER, reason TEXT NOT NULL,
            deferred_at TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
            last_attempt_at TEXT, UNIQUE(chat_id,message_id,part)
          );
        """)
        # Additive migration: existing chat choices, cursors and media stay intact.
        columns = {row["name"] for row in self.db.execute("PRAGMA table_info(media)")}
        with self.db:
            for name, definition in {
                "size_bytes": "INTEGER", "downloaded_at": "TEXT", "uploaded_at": "TEXT",
            }.items():
                if name not in columns:
                    self.db.execute(f"ALTER TABLE media ADD COLUMN {name} {definition}")
        columns = {row["name"] for row in self.db.execute("PRAGMA table_info(chats)")}
        with self.db:
            for name, definition in {
                "is_forum": "INTEGER NOT NULL DEFAULT 0",
                "topics": "TEXT NOT NULL DEFAULT '[]'",
                "topic_ids": "TEXT",
                "topics_error": "TEXT",
                "scan_start_day": "TEXT",
                "scanned_before": "TEXT",
                "checked_at": "TEXT",
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

        legacy = self.get("telegram_wait") or {}
        if legacy.get("kind") == "TakeoutInitDelayError":
            # retry_at in the detail is historical. A cleared active deadline
            # may mean the user already confirmed the export in Telegram.
            deadline = max(self.get("takeout_retry_after", 0), self.get("telegram_retry_after", 0))
            # One transaction: never lose the export deadline or clear a real FloodWait.
            with self.db:
                for key, value in {"takeout_retry_after": deadline, "takeout_wait": legacy,
                                   "telegram_retry_after": 0, "telegram_wait": None,
                                   "telegram_wait_attempt": 0}.items():
                    self.db.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)",
                                    (key, json.dumps(value)))

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
               part="main", kind="photo", grouped_id=None, advance_cursor=True, size_bytes=None):
        with self.db:
            self.db.execute(
                """INSERT OR IGNORE INTO media(id,path,created_at,size_bytes,downloaded_at)
                   VALUES(?,?,?,?,?)""",
                (media_id, path, sent_at, size_bytes, datetime.now(UTC).isoformat()),
            )
            self.db.execute(
                """INSERT OR IGNORE INTO sources(chat_id,message_id,part,media_id,sent_at,kind,grouped_id)
                   VALUES(?,?,?,?,?,?,?)""",
                (chat_id, message_id, part, media_id, sent_at, kind,
                 str(grouped_id) if grouped_id is not None else None),
            )
            if advance_cursor:
                self.db.execute("UPDATE chats SET cursor=? WHERE id=?", (message_id, chat_id))
            # Recording the source and resolving its deferred entry are atomic.
            self.db.execute("DELETE FROM deferred WHERE chat_id=? AND message_id=? AND part=?",
                            (chat_id, message_id, part))

    def defer(self, chat_id, message_id, *, part="main", day, kind, size_bytes=None, reason):
        if self.one("SELECT 1 FROM sources WHERE chat_id=? AND message_id=? AND part=?",
                    (chat_id, message_id, part)):
            raise ValueError("Это вложение уже скачано.")
        self.execute("""INSERT INTO deferred(chat_id,message_id,part,day,kind,size_bytes,reason,deferred_at)
                        VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(chat_id,message_id,part)
                        DO UPDATE SET reason=excluded.reason""",
                     (chat_id, message_id, part, day, kind, size_bytes, reason, datetime.now(UTC).isoformat()))

    def deferred_files(self):
        return {"total": self.one("SELECT COUNT(*) n FROM deferred")["n"],
                "items": self.rows("""SELECT d.*,c.title FROM deferred d JOIN chats c ON c.id=d.chat_id
                                      ORDER BY d.deferred_at,d.id LIMIT 100""")}

    def inventory(self, *, status="all", query="", offset=0, limit=50):
        # One row per unique file; all Telegram occurrences remain in sources.
        cte = """WITH files AS (
            SELECT m.*,
              CASE WHEN m.error IS NOT NULL THEN 'error'
                WHEN m.asset_id IS NULL THEN 'pending'
                WHEN EXISTS(SELECT 1 FROM sources s WHERE s.media_id=m.id AND s.album_added=0)
                  THEN 'album_pending'
                ELSE 'uploaded' END AS status
            FROM media m
        ) """
        summary = self.one(cte + """
            SELECT COUNT(*) total, COALESCE(SUM(size_bytes),0) bytes,
              COUNT(*)-COUNT(size_bytes) unknown_sizes,
              COALESCE(SUM(status='pending'),0) pending,
              COALESCE(SUM(status='uploaded'),0) uploaded,
              COALESCE(SUM(status='album_pending'),0) album_pending,
              COALESCE(SUM(status='error'),0) errors
            FROM files
        """)
        summary["sources"] = self.one("SELECT COUNT(*) count FROM sources")["count"]
        conditions, args = [], []
        if status != "all":
            conditions.append("status=?")
            args.append(status)
        if query:
            # Literal substring search; '%' and '_' in names are not wildcards.
            conditions.append("""(instr(lower(id),lower(?))>0 OR instr(lower(path),lower(?))>0
                OR EXISTS(SELECT 1 FROM sources s JOIN chats c ON c.id=s.chat_id
                  WHERE s.media_id=files.id AND (
                    instr(casefold(c.title),casefold(?))>0 OR instr(CAST(s.message_id AS TEXT),?)>0)))""")
            args.extend([query] * 4)
        self.db.create_function("casefold", 1, lambda s: s.casefold(), deterministic=True)
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        total = self.one(cte + "SELECT COUNT(*) count FROM files" + where, args)["count"]
        items = self.rows(
            cte + "SELECT * FROM files" + where +
            " ORDER BY COALESCE(downloaded_at,created_at) DESC,id DESC LIMIT ? OFFSET ?",
            [*args, limit, offset],
        )
        for item in items:
            item["filename"] = Path(item.pop("path")).name
            item["sources"] = self.rows("""
                SELECT s.chat_id,s.message_id,s.part,s.kind,s.grouped_id,s.sent_at,
                       s.album_added,c.title,c.error AS album_error
                FROM sources s JOIN chats c ON c.id=s.chat_id WHERE s.media_id=?
                ORDER BY s.sent_at,s.chat_id,s.message_id,s.part
            """, (item["id"],))
        return {"items": items, "total": total, "offset": offset, "limit": limit, "summary": summary}
