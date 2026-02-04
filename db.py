# db.py
import aiosqlite
from datetime import datetime, timezone

def now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

CREATE_SQL = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS users (
  chat_id      INTEGER PRIMARY KEY,
  user_id      INTEGER,
  username     TEXT,
  first_name   TEXT,
  last_name    TEXT,
  phone        TEXT,
  registered   INTEGER DEFAULT 0,
  lang         TEXT DEFAULT 'uz',
  first_seen   TEXT,
  last_seen    TEXT,
  plan         TEXT DEFAULT 'free',
  plan_expires TEXT,
  blocked      INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_users_last_seen ON users(last_seen);

CREATE TABLE IF NOT EXISTS watches (
  chat_id    INTEGER PRIMARY KEY,
  enabled    INTEGER DEFAULT 0,
  dep_code   TEXT,
  arv_code   TEXT,
  dep_name   TEXT,
  arv_name   TEXT,
  date_from  TEXT,
  date_to    TEXT,
  snapshot_json TEXT,
  snapshot_hash TEXT,
  updated_at TEXT,
  FOREIGN KEY(chat_id) REFERENCES users(chat_id)
);

CREATE TABLE IF NOT EXISTS feedbacks (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  chat_id    INTEGER NOT NULL,
  text       TEXT NOT NULL,
  created_at TEXT NOT NULL,
  FOREIGN KEY(chat_id) REFERENCES users(chat_id)
);

CREATE INDEX IF NOT EXISTS idx_feedbacks_chat_id ON feedbacks(chat_id);
"""

async def _ensure_column(db, table: str, column: str, ddl: str):
    cur = await db.execute(f"PRAGMA table_info({table})")
    cols = [r[1] for r in await cur.fetchall()]
    if column not in cols:
        await db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")

async def init_db(db_path: str):
    async with aiosqlite.connect(db_path) as db:
        await db.executescript(CREATE_SQL)

        # ✅ eski DB bo‘lsa ham yangi ustun qo‘shib oladi
        await _ensure_column(db, "users", "lang", "TEXT DEFAULT 'uz'")

        # ✅ feedbacks jadvali yo‘q bo‘lsa yaratib oladi
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS feedbacks (
              id         INTEGER PRIMARY KEY AUTOINCREMENT,
              chat_id    INTEGER NOT NULL,
              text       TEXT NOT NULL,
              created_at TEXT NOT NULL,
              FOREIGN KEY(chat_id) REFERENCES users(chat_id)
            )
            """
        )
        await db.execute("CREATE INDEX IF NOT EXISTS idx_feedbacks_chat_id ON feedbacks(chat_id)")
        await db.commit()

async def set_lang(db_path: str, chat_id: int, lang: str):
    lang = (lang or "uz").lower()
    if lang not in ("uz", "ru", "en"):
        lang = "uz"
    async with aiosqlite.connect(db_path) as db:
        await db.execute("UPDATE users SET lang=?, last_seen=? WHERE chat_id=?", (lang, now_utc_iso(), chat_id))
        await db.commit()

async def get_lang(db_path: str, chat_id: int) -> str | None:
    async with aiosqlite.connect(db_path) as db:
        cur = await db.execute("SELECT lang FROM users WHERE chat_id=?", (chat_id,))
        row = await cur.fetchone()
        return row[0] if row and row[0] else None

async def upsert_user(db_path: str, chat_id: int, user_id: int | None, username: str | None,
                      first_name: str | None, last_name: str | None):
    now = now_utc_iso()
    async with aiosqlite.connect(db_path) as db:
        # mavjud bo'lsa yangilaydi, yo'q bo'lsa yaratadi
        await db.execute(
            """
            INSERT INTO users(chat_id, user_id, username, first_name, last_name, first_seen, last_seen)
            VALUES(?,?,?,?,?,?,?)
            ON CONFLICT(chat_id) DO UPDATE SET
              user_id=COALESCE(excluded.user_id, users.user_id),
              username=COALESCE(excluded.username, users.username),
              first_name=COALESCE(excluded.first_name, users.first_name),
              last_name=COALESCE(excluded.last_name, users.last_name),
              last_seen=excluded.last_seen
            """,
            (chat_id, user_id, username, first_name, last_name, now, now),
        )
        await db.commit()

async def touch_last_seen(db_path: str, chat_id: int):
    async with aiosqlite.connect(db_path) as db:
        await db.execute("UPDATE users SET last_seen=? WHERE chat_id=?", (now_utc_iso(), chat_id))
        await db.commit()

async def set_phone(db_path: str, chat_id: int, phone: str):
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "UPDATE users SET phone=?, registered=1, last_seen=? WHERE chat_id=?",
            (phone, now_utc_iso(), chat_id),
        )
        await db.commit()

async def get_phone(db_path: str, chat_id: int) -> str | None:
    async with aiosqlite.connect(db_path) as db:
        cur = await db.execute("SELECT phone FROM users WHERE chat_id=?", (chat_id,))
        row = await cur.fetchone()
        return row[0] if row and row[0] else None

async def save_watch(db_path: str, chat_id: int, data: dict):
    # data: enabled, dep_code, arv_code, dep_name, arv_name, date_from, date_to, snapshot_json, snapshot_hash
    now = now_utc_iso()
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """
            INSERT INTO watches(chat_id, enabled, dep_code, arv_code, dep_name, arv_name, date_from, date_to,
                                snapshot_json, snapshot_hash, updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(chat_id) DO UPDATE SET
              enabled=excluded.enabled,
              dep_code=excluded.dep_code,
              arv_code=excluded.arv_code,
              dep_name=excluded.dep_name,
              arv_name=excluded.arv_name,
              date_from=excluded.date_from,
              date_to=excluded.date_to,
              snapshot_json=excluded.snapshot_json,
              snapshot_hash=excluded.snapshot_hash,
              updated_at=excluded.updated_at
            """,
            (
                chat_id,
                int(bool(data.get("enabled"))),
                data.get("dep_code"),
                data.get("arv_code"),
                data.get("dep_name"),
                data.get("arv_name"),
                data.get("date_from"),
                data.get("date_to"),
                data.get("snapshot_json"),
                data.get("snapshot_hash"),
                now,
            ),
        )
        await db.commit()

async def get_watch(db_path: str, chat_id: int) -> dict | None:
    async with aiosqlite.connect(db_path) as db:
        cur = await db.execute(
            """
            SELECT enabled, dep_code, arv_code, dep_name, arv_name, date_from, date_to, snapshot_json, snapshot_hash
            FROM watches WHERE chat_id=?
            """,
            (chat_id,),
        )
        row = await cur.fetchone()
        if not row:
            return None
        return {
            "enabled": bool(row[0]),
            "dep_code": row[1],
            "arv_code": row[2],
            "dep_name": row[3],
            "arv_name": row[4],
            "date_from": row[5],
            "date_to": row[6],
            "snapshot_json": row[7],
            "snapshot_hash": row[8],
        }

async def set_watch_enabled(db_path: str, chat_id: int, enabled: bool):
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "INSERT INTO watches(chat_id, enabled, updated_at) VALUES(?,?,?) "
            "ON CONFLICT(chat_id) DO UPDATE SET enabled=?, updated_at=?",
            (chat_id, int(enabled), now_utc_iso(), int(enabled), now_utc_iso()),
        )
        await db.commit()

async def list_all_users(db_path: str) -> list[int]:
    async with aiosqlite.connect(db_path) as db:
        cur = await db.execute("SELECT chat_id FROM users WHERE blocked=0")
        rows = await cur.fetchall()
        return [r[0] for r in rows]

async def list_enabled_watches(db_path: str) -> list[dict]:
    async with aiosqlite.connect(db_path) as db:
        cur = await db.execute(
            """
            SELECT chat_id, enabled, dep_code, arv_code, dep_name, arv_name, date_from, date_to, snapshot_json, snapshot_hash
            FROM watches
            WHERE enabled=1
            """
        )
        rows = await cur.fetchall()
        out = []
        for r in rows:
            out.append({
                "chat_id": r[0],
                "enabled": bool(r[1]),
                "dep_code": r[2],
                "arv_code": r[3],
                "dep_name": r[4],
                "arv_name": r[5],
                "date_from": r[6],
                "date_to": r[7],
                "snapshot_json": r[8],
                "snapshot_hash": r[9],
            })
        return out

# =========================
# LANG + FEEDBACK FUNCTIONS
# =========================

async def get_user_lang(db_path: str, chat_id: int) -> str:
    async with aiosqlite.connect(db_path) as db:
        cur = await db.execute(
            "SELECT lang FROM users WHERE chat_id=?",
            (chat_id,)
        )
        row = await cur.fetchone()
        lang = (row[0] if row and row[0] else "uz").lower()
        return lang if lang in ("uz", "ru", "en") else "uz"

async def set_user_lang(db_path: str, chat_id: int, lang: str):
    lang = (lang or "uz").lower()
    if lang not in ("uz", "ru", "en"):
        lang = "uz"

    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "UPDATE users SET lang=?, last_seen=? WHERE chat_id=?",
            (lang, now_utc_iso(), chat_id)
        )
        await db.commit()

async def add_feedback(db_path: str, chat_id: int, text: str):
    text = (text or "").strip()
    if not text:
        return

    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "INSERT INTO feedbacks(chat_id, text, created_at) VALUES(?,?,?)",
            (chat_id, text, now_utc_iso()),
        )
        await db.execute(
            "UPDATE users SET last_seen=? WHERE chat_id=?",
            (now_utc_iso(), chat_id),
        )
        await db.commit()