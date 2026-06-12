"""
persistence.py — Supabase-backed persistence for AURACLE_XBOT
=============================================================
Uses Supabase free tier (no Railway Volume needed).

SETUP (one time):
  1. Go to https://supabase.com → create free account → New Project
  2. Project → SQL Editor → run this SQL:

       CREATE TABLE IF NOT EXISTS bot_users (
           uid     BIGINT PRIMARY KEY,
           data    TEXT    NOT NULL,
           updated BIGINT  NOT NULL
       );
       CREATE TABLE IF NOT EXISTS bot_trade_log (
           uid     BIGINT PRIMARY KEY,
           data    TEXT    NOT NULL,
           updated BIGINT  NOT NULL
       );

  3. Project → Settings → API
     Copy "Project URL" and "anon public" key
  4. Railway → your service → Variables:
       SUPABASE_URL  = https://xxxx.supabase.co
       SUPABASE_KEY  = your-anon-public-key

No Volume needed.
"""

import json
import logging
import os
import time
from datetime import datetime, date

import httpx

logger = logging.getLogger(__name__)

_SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
_SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")
_HEADERS = {
    "apikey":        _SUPABASE_KEY,
    "Authorization": f"Bearer {_SUPABASE_KEY}",
    "Content-Type":  "application/json",
}

_USE_SQLITE = not (_SUPABASE_URL and _SUPABASE_KEY)

if _USE_SQLITE:
    import sqlite3
    _DATA_DIR = "/data" if os.path.isdir("/data") else os.path.dirname(os.path.abspath(__file__))
    DB_PATH   = os.path.join(_DATA_DIR, "auracle_bot.db")
    _SCHEMA = """
    CREATE TABLE IF NOT EXISTS users (
        uid     INTEGER PRIMARY KEY,
        data    TEXT    NOT NULL,
        updated REAL    NOT NULL
    );
    CREATE TABLE IF NOT EXISTS trade_log (
        uid     INTEGER NOT NULL,
        data    TEXT    NOT NULL,
        updated REAL    NOT NULL,
        PRIMARY KEY (uid)
    );
    """
else:
    DB_PATH = None


class _BotEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, datetime):
            return {"__datetime__": obj.isoformat()}
        if isinstance(obj, date):
            return {"__date__": obj.isoformat()}
        if isinstance(obj, set):
            return {"__set__": list(obj)}
        return super().default(obj)


def _bot_decoder(d: dict):
    if "__datetime__" in d:
        return datetime.fromisoformat(d["__datetime__"])
    if "__date__" in d:
        return date.fromisoformat(d["__date__"])
    if "__set__" in d:
        return set(d["__set__"])
    return d


def _dumps(obj) -> str:
    return json.dumps(obj, cls=_BotEncoder)


def _loads(s: str):
    return json.loads(s, object_hook=_bot_decoder)


def _sb_upsert(table: str, uid: int, data_str: str):
    try:
        r = httpx.post(
            f"{_SUPABASE_URL}/rest/v1/{table}",
            headers={**_HEADERS, "Prefer": "resolution=merge-duplicates"},
            content=json.dumps({"uid": uid, "data": data_str, "updated": int(time.time())}),
            timeout=10,
        )
        if r.status_code not in (200, 201):
            logger.error("Supabase UPSERT %s/%s: %s %s", table, uid, r.status_code, r.text[:200])
    except Exception as e:
        logger.error("Supabase UPSERT %s/%s error: %s", table, uid, e)


def _sb_all(table: str) -> list:
    """
    Fetch ALL rows from a Supabase table using Range-based pagination.
    Supabase REST API returns max 1000 rows per request without a Range header.
    Without pagination, bots with >1000 users silently lose data on restart.
    """
    results = []
    page_size = 1000
    offset = 0
    while True:
        try:
            r = httpx.get(
                f"{_SUPABASE_URL}/rest/v1/{table}",
                headers={
                    **_HEADERS,
                    "Range-Unit": "items",
                    "Range": f"{offset}-{offset + page_size - 1}",
                    "Prefer": "count=none",
                },
                params={"select": "uid,data"},
                timeout=15,
            )
            if r.status_code not in (200, 206):
                logger.error("Supabase ALL %s (offset %d): %s %s", table, offset, r.status_code, r.text[:200])
                break
            batch = r.json()
            if not isinstance(batch, list) or not batch:
                break
            results.extend(batch)
            if len(batch) < page_size:
                break   # last page
            offset += page_size
        except Exception as e:
            logger.error("Supabase ALL %s (offset %d): %s", table, offset, e)
            break
    return results


def _sqlite_conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    # WAL mode: allows concurrent reads + one writer without "database is locked"
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db():
    if _USE_SQLITE:
        try:
            with _sqlite_conn() as conn:
                conn.executescript(_SCHEMA)
            logger.info("SQLite DB ready at %s", DB_PATH)
        except Exception as e:
            logger.error("SQLite init failed: %s", e)
            raise
    else:
        logger.info("Supabase persistence active: %s", _SUPABASE_URL)


def load_all(users: dict, trade_log: dict):
    init_db()
    loaded = 0
    if _USE_SQLITE:
        try:
            with _sqlite_conn() as conn:
                for row in conn.execute("SELECT uid, data FROM users"):
                    try:
                        users[row["uid"]] = _loads(row["data"])
                        loaded += 1
                    except Exception as e:
                        logger.error("Load user %s: %s", row["uid"], e)
                for row in conn.execute("SELECT uid, data FROM trade_log"):
                    try:
                        trade_log[row["uid"]] = _loads(row["data"])
                    except Exception as e:
                        logger.error("Load trade_log %s: %s", row["uid"], e)
        except Exception as e:
            logger.error("load_all sqlite: %s", e)
    else:
        for row in _sb_all("bot_users"):
            try:
                users[row["uid"]] = _loads(row["data"])
                loaded += 1
            except Exception as e:
                logger.error("Load user %s: %s", row["uid"], e)
        for row in _sb_all("bot_trade_log"):
            try:
                trade_log[row["uid"]] = _loads(row["data"])
            except Exception as e:
                logger.error("Load trade_log %s: %s", row["uid"], e)
    logger.info("Loaded %d users (%s)", loaded, "sqlite" if _USE_SQLITE else "supabase")


def _save_user_sync(uid: int, ud: dict):
    """Synchronous save — call via asyncio.to_thread from async context."""
    try:
        data_str = _dumps(ud)
        if _USE_SQLITE:
            with _sqlite_conn() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO users (uid, data, updated) VALUES (?, ?, ?)",
                    (uid, data_str, time.time())
                )
        else:
            _sb_upsert("bot_users", uid, data_str)
    except Exception as e:
        logger.error("save_user(%s): %s", uid, e)


def save_user(uid: int, ud: dict):
    """
    Non-blocking save. Schedules the sync write on a thread pool so the
    async event loop is never blocked by network I/O to Supabase.
    Falls back to direct sync call if no running loop (e.g. at startup).
    The returned future is intentionally not awaited — fire-and-forget is
    acceptable here because autosave_job also persists all users every 60s.
    Errors are surfaced in _save_user_sync via logger.error.
    """
    import asyncio
    try:
        loop = asyncio.get_running_loop()
        loop.run_in_executor(None, _save_user_sync, uid, ud)
    except RuntimeError:
        # No running loop — called from sync context (e.g. load_all at boot)
        _save_user_sync(uid, ud)


def _save_trade_log_sync(uid: int, log: list):
    try:
        data_str = _dumps(log)
        if _USE_SQLITE:
            with _sqlite_conn() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO trade_log (uid, data, updated) VALUES (?, ?, ?)",
                    (uid, data_str, time.time())
                )
        else:
            _sb_upsert("bot_trade_log", uid, data_str)
    except Exception as e:
        logger.error("save_trade_log(%s): %s", uid, e)


def save_trade_log(uid: int, log: list):
    import asyncio
    try:
        loop = asyncio.get_running_loop()
        loop.run_in_executor(None, _save_trade_log_sync, uid, log)
    except RuntimeError:
        _save_trade_log_sync(uid, log)


def save_all(uid: int, ud: dict, log: list):
    save_user(uid, ud)
    save_trade_log(uid, log)


async def autosave_job(context):
    import sol_trading_bot as _bot
    saved = 0
    for uid, ud in list(_bot.users.items()):
        try:
            # ── Snapshot runtime dicts into ud so they survive restarts ───────
            # These are in-memory-only by default; writing them to ud means
            # load_all can restore them on the next boot without any extra table.
            if uid in _bot._apex_watchlist:
                ud["_persisted_watchlist"] = _bot._apex_watchlist[uid]
            if uid in _bot._apex_post_exit:
                ud["_persisted_post_exit"] = _bot._apex_post_exit[uid]
            if uid in _bot._channel_calls:
                ud["_persisted_channel_calls"] = _bot._channel_calls[uid]
            if uid in _bot._apex_rejected:
                ud["_persisted_rejected"] = _bot._apex_rejected[uid]
            save_user(uid, ud)
            if uid in _bot.trade_log:
                save_trade_log(uid, _bot.trade_log[uid])
            saved += 1
        except Exception as e:
            logger.error("autosave uid %s: %s", uid, e)
    logger.debug("Autosave: %d users saved", saved)
