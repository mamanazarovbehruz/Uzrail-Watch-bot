# db.py (PostgreSQL version)
import os
import asyncpg
from datetime import datetime, timezone

_POOL: asyncpg.Pool | None = None

def now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def _db_url() -> str:
    url = (os.getenv("DATABASE_URL") or "").strip()
    if not url:
        raise RuntimeError("DATABASE_URL topilmadi. Railway Postgres -> Variables dan bot service ga DATABASE_URL bering.")
    # asyncpg 'postgres://' ni ham tushunadi, lekin Railway odatda 'postgresql://' beradi
    return url

async def get_pool() -> asyncpg.Pool:
    global _POOL
    if _POOL is None:
        _POOL = await asyncpg.create_pool(
            dsn=_db_url(),
            min_size=1,
            max_size=5,
            command_timeout=60,
        )
    return _POOL

CREATE_SQL = """
CREATE TABLE IF NOT EXISTS users (
  chat_id      BIGINT PRIMARY KEY,
  user_id      BIGINT,
  username     TEXT,
  first_name   TEXT,
  last_name    TEXT,
  phone        TEXT,
  registered   BOOLEAN DEFAULT FALSE,
  lang         TEXT DEFAULT 'uz',
  first_seen   TEXT,
  last_seen    TEXT,
  plan         TEXT DEFAULT 'free',
  plan_expires TEXT,
  blocked      BOOLEAN DEFAULT FALSE
);

CREATE INDEX IF NOT EXISTS idx_users_last_seen ON users(last_seen);

CREATE TABLE IF NOT EXISTS watches (
  chat_id        BIGINT PRIMARY KEY,
  enabled        BOOLEAN DEFAULT FALSE,
  dep_code       TEXT,
  arv_code       TEXT,
  dep_name       TEXT,
  arv_name       TEXT,
  date_from      TEXT,
  date_to        TEXT,
  snapshot_json  TEXT,
  snapshot_hash  TEXT,
  updated_at     TEXT,
  CONSTRAINT fk_watches_user FOREIGN KEY(chat_id) REFERENCES users(chat_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS feedbacks (
  id         BIGSERIAL PRIMARY KEY,
  chat_id    BIGINT NOT NULL,
  text       TEXT NOT NULL,
  created_at TEXT NOT NULL,
  CONSTRAINT fk_feedbacks_user FOREIGN KEY(chat_id) REFERENCES users(chat_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_feedbacks_chat_id ON feedbacks(chat_id);
"""

async def init_db(_db_path_ignored: str | None = None):
    pool = await get_pool()
    async with pool.acquire() as con:
        await con.execute(CREATE_SQL)

# =========================
# USERS
# =========================

async def get_lang(db_path: str, chat_id: int) -> str | None:
    pool = await get_pool()
    async with pool.acquire() as con:
        row = await con.fetchrow("SELECT lang FROM users WHERE chat_id=$1", chat_id)
        return row["lang"] if row and row["lang"] else None

async def set_lang(db_path: str, chat_id: int, lang: str):
    lang = (lang or "uz").lower()
    if lang not in ("uz", "ru", "en"):
        lang = "uz"
    pool = await get_pool()
    async with pool.acquire() as con:
        await con.execute(
            "UPDATE users SET lang=$1, last_seen=$2 WHERE chat_id=$3",
            lang, now_utc_iso(), chat_id
        )

async def upsert_user(db_path: str, chat_id: int, user_id: int | None, username: str | None,
                      first_name: str | None, last_name: str | None):
    now = now_utc_iso()
    pool = await get_pool()
    async with pool.acquire() as con:
        await con.execute(
            """
            INSERT INTO users(chat_id, user_id, username, first_name, last_name, first_seen, last_seen)
            VALUES($1,$2,$3,$4,$5,$6,$7)
            ON CONFLICT (chat_id) DO UPDATE SET
              user_id=COALESCE(EXCLUDED.user_id, users.user_id),
              username=COALESCE(EXCLUDED.username, users.username),
              first_name=COALESCE(EXCLUDED.first_name, users.first_name),
              last_name=COALESCE(EXCLUDED.last_name, users.last_name),
              last_seen=EXCLUDED.last_seen
            """,
            chat_id, user_id, username, first_name, last_name, now, now
        )

async def touch_last_seen(db_path: str, chat_id: int):
    pool = await get_pool()
    async with pool.acquire() as con:
        await con.execute(
            "UPDATE users SET last_seen=$1 WHERE chat_id=$2",
            now_utc_iso(), chat_id
        )

async def set_phone(db_path: str, chat_id: int, phone: str):
    pool = await get_pool()
    async with pool.acquire() as con:
        await con.execute(
            "UPDATE users SET phone=$1, registered=TRUE, last_seen=$2 WHERE chat_id=$3",
            phone, now_utc_iso(), chat_id
        )

async def get_phone(db_path: str, chat_id: int) -> str | None:
    pool = await get_pool()
    async with pool.acquire() as con:
        row = await con.fetchrow("SELECT phone FROM users WHERE chat_id=$1", chat_id)
        return row["phone"] if row and row["phone"] else None

async def list_all_users(db_path: str):
    pool = await get_pool()
    async with pool.acquire() as con:
        rows = await con.fetch(
            "SELECT chat_id, phone, lang FROM users ORDER BY chat_id DESC LIMIT 200"
        )
        return [(int(r["chat_id"]), r["phone"], r["lang"]) for r in rows]

# =========================
# WATCHES
# =========================

async def save_watch(db_path: str, chat_id: int, data: dict):
    now = now_utc_iso()
    pool = await get_pool()
    async with pool.acquire() as con:
        await con.execute(
            """
            INSERT INTO watches(
              chat_id, enabled, dep_code, arv_code, dep_name, arv_name, date_from, date_to,
              snapshot_json, snapshot_hash, updated_at
            )
            VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
            ON CONFLICT (chat_id) DO UPDATE SET
              enabled=EXCLUDED.enabled,
              dep_code=EXCLUDED.dep_code,
              arv_code=EXCLUDED.arv_code,
              dep_name=EXCLUDED.dep_name,
              arv_name=EXCLUDED.arv_name,
              date_from=EXCLUDED.date_from,
              date_to=EXCLUDED.date_to,
              snapshot_json=EXCLUDED.snapshot_json,
              snapshot_hash=EXCLUDED.snapshot_hash,
              updated_at=EXCLUDED.updated_at
            """,
            chat_id,
            bool(data.get("enabled")),
            data.get("dep_code"),
            data.get("arv_code"),
            data.get("dep_name"),
            data.get("arv_name"),
            data.get("date_from"),
            data.get("date_to"),
            data.get("snapshot_json"),
            data.get("snapshot_hash"),
            now,
        )

async def get_watch(db_path: str, chat_id: int) -> dict | None:
    pool = await get_pool()
    async with pool.acquire() as con:
        row = await con.fetchrow(
            """
            SELECT chat_id, enabled, dep_code, arv_code, dep_name, arv_name,
                   date_from, date_to, snapshot_json, snapshot_hash
            FROM watches
            WHERE chat_id=$1
            """,
            chat_id
        )
        if not row:
            return None
        return {
            "chat_id": int(row["chat_id"]),
            "enabled": bool(row["enabled"]),
            "dep_code": row["dep_code"],
            "arv_code": row["arv_code"],
            "dep_name": row["dep_name"],
            "arv_name": row["arv_name"],
            "date_from": row["date_from"],
            "date_to": row["date_to"],
            "snapshot_json": row["snapshot_json"],
            "snapshot_hash": row["snapshot_hash"],
        }

async def set_watch_enabled(db_path: str, chat_id: int, enabled: bool):
    pool = await get_pool()
    async with pool.acquire() as con:
        await con.execute(
            "UPDATE watches SET enabled=$1, updated_at=$2 WHERE chat_id=$3",
            bool(enabled), now_utc_iso(), chat_id
        )

async def list_enabled_watches(db_path: str):
    pool = await get_pool()
    async with pool.acquire() as con:
        rows = await con.fetch(
            """
            SELECT chat_id, enabled, dep_code, arv_code, dep_name, arv_name,
                   date_from, date_to, snapshot_json, snapshot_hash
            FROM watches
            WHERE enabled=TRUE
            """
        )
        out = []
        for r in rows:
            out.append({
                "chat_id": int(r["chat_id"]),
                "enabled": bool(r["enabled"]),
                "dep_code": r["dep_code"],
                "arv_code": r["arv_code"],
                "dep_name": r["dep_name"],
                "arv_name": r["arv_name"],
                "date_from": r["date_from"],
                "date_to": r["date_to"],
                "snapshot_json": r["snapshot_json"],
                "snapshot_hash": r["snapshot_hash"],
            })
        return out

# =========================
# LANG + FEEDBACK FUNCTIONS
# =========================

async def get_user_lang(db_path: str, chat_id: int) -> str:
    lang = await get_lang(db_path, chat_id)
    lang = (lang or "uz").lower()
    return lang if lang in ("uz", "ru", "en") else "uz"

async def set_user_lang(db_path: str, chat_id: int, lang: str):
    await set_lang(db_path, chat_id, lang)

async def add_feedback(db_path: str, chat_id: int, text: str):
    text = (text or "").strip()
    if not text:
        return
    pool = await get_pool()
    async with pool.acquire() as con:
        await con.execute(
            "INSERT INTO feedbacks(chat_id, text, created_at) VALUES($1,$2,$3)",
            chat_id, text, now_utc_iso()
        )
        await con.execute(
            "UPDATE users SET last_seen=$1 WHERE chat_id=$2",
            now_utc_iso(), chat_id
        )
