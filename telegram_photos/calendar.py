"""Day coverage and saved media counts, in the import's Moscow timezone."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

MOSCOW = ZoneInfo("Europe/Moscow")


def day_map(store, current=None, today=None):
    today = today or datetime.now(MOSCOW).date().isoformat()
    chats = store.rows("""SELECT id,title,since,until_date,scan_start_day,scanned_before,checked_at,error
                          FROM chats WHERE selected=1 ORDER BY id""")
    output = []
    for chat in chats:
        # These are occurrences in Telegram, not global unique-file counts.
        counts = {row["day"]: row for row in store.rows("""
            SELECT date(s.sent_at,'+3 hours') day, COUNT(*) downloaded,
              SUM(m.asset_id IS NOT NULL) uploaded,
              SUM(m.asset_id IS NOT NULL AND s.album_added=1) in_album,
              SUM(m.error IS NOT NULL) errors
            FROM sources s JOIN media m ON m.id=s.media_id WHERE s.chat_id=?
            GROUP BY day
        """, (chat["id"],))}
        deferred = {row["day"]: row["n"] for row in store.rows(
            "SELECT day,COUNT(*) n FROM deferred WHERE chat_id=? GROUP BY day", (chat["id"],))}
        day = datetime.fromisoformat(chat["since"]).date()
        end = min(chat["until_date"] or today, today)
        days = []
        while day.isoformat() <= end:
            key = day.isoformat()
            item = {"day": key, "downloaded": 0, "uploaded": 0, "in_album": 0, "errors": 0,
                    "deferred": deferred.get(key, 0), **counts.get(key, {})}
            covered = bool(chat["scan_start_day"] and chat["scanned_before"] and
                           chat["scan_start_day"] <= key < chat["scanned_before"])
            active = bool(current and current["chat_id"] == chat["id"] and current["day"] == key)
            item["state"] = (
                "active" if active else "error" if item["errors"] else
                "pending" if item["deferred"] or item["downloaded"] > item["in_album"] else
                "complete" if covered and item["downloaded"] else "empty" if covered else
                "saved" if item["downloaded"] else "unknown"
            )
            item["covered"] = covered
            days.append(item)
            day += timedelta(days=1)
        output.append({**chat, "days": days})
    return {"timezone": "Europe/Moscow", "today": today, "chats": output}
