"""Схема SQLite и доступ к данным."""

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime

from . import config

_local = threading.local()

SCHEMA = """
CREATE TABLE IF NOT EXISTS contacts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    phone       TEXT NOT NULL UNIQUE,
    name        TEXT DEFAULT '',
    fields_json TEXT DEFAULT '{}',
    consent     INTEGER NOT NULL DEFAULT 1,
    source      TEXT DEFAULT '',
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS optout (
    phone      TEXT PRIMARY KEY,
    reason     TEXT DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS campaigns (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT NOT NULL,
    template     TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'draft',
    delay_sec    REAL NOT NULL DEFAULT 4,
    jitter_sec   REAL NOT NULL DEFAULT 2,
    hourly_limit INTEGER NOT NULL DEFAULT 100,
    sim_number   INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL,
    started_at   TEXT,
    finished_at  TEXT
);

CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id INTEGER NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    contact_id  INTEGER,
    phone       TEXT NOT NULL,
    text        TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'queued',
    gateway_id  TEXT,
    error       TEXT,
    attempts    INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL,
    sent_at     TEXT,
    updated_at  TEXT
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE INDEX IF NOT EXISTS idx_messages_campaign ON messages(campaign_id, status);
CREATE INDEX IF NOT EXISTS idx_messages_gateway  ON messages(gateway_id);
CREATE INDEX IF NOT EXISTS idx_contacts_phone    ON contacts(phone);
"""


def _connect():
    conn = sqlite3.connect(config.DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def get_conn():
    """Отдельное соединение на поток: воркер рассылки живёт параллельно с UI."""
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = _connect()
        _local.conn = conn
    return conn


@contextmanager
def tx():
    conn = get_conn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def now():
    return datetime.now().isoformat(timespec="seconds")


def init_db():
    conn = get_conn()
    conn.executescript(SCHEMA)
    for key, value in config.DEFAULT_SETTINGS.items():
        conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (key, value))
    conn.commit()


# --- настройки ---------------------------------------------------------------

def get_settings():
    rows = get_conn().execute("SELECT key, value FROM settings").fetchall()
    data = dict(config.DEFAULT_SETTINGS)
    data.update({r["key"]: r["value"] for r in rows})
    return data


def save_settings(values):
    with tx() as conn:
        for key, value in values.items():
            conn.execute(
                "INSERT INTO settings(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, str(value)),
            )


# --- контакты ----------------------------------------------------------------

def upsert_contact(phone, name="", fields=None, consent=1, source=""):
    """Возвращает 'added' или 'updated'. Номер — естественный ключ."""
    fields_json = json.dumps(fields or {}, ensure_ascii=False)
    with tx() as conn:
        cur = conn.execute("SELECT id, fields_json FROM contacts WHERE phone = ?", (phone,))
        row = cur.fetchone()
        if row:
            merged = json.loads(row["fields_json"] or "{}")
            merged.update(fields or {})
            conn.execute(
                "UPDATE contacts SET name = COALESCE(NULLIF(?, ''), name), "
                "fields_json = ?, consent = ? WHERE id = ?",
                (name, json.dumps(merged, ensure_ascii=False), consent, row["id"]),
            )
            return "updated"
        conn.execute(
            "INSERT INTO contacts(phone, name, fields_json, consent, source, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (phone, name, fields_json, consent, source, now()),
        )
        return "added"


def list_contacts(search="", only_consent=False, limit=100, offset=0):
    sql = "SELECT * FROM contacts WHERE 1=1"
    args = []
    if search:
        sql += " AND (phone LIKE ? OR name LIKE ?)"
        args += ["%" + search + "%", "%" + search + "%"]
    if only_consent:
        sql += " AND consent = 1"
    sql += " ORDER BY id DESC LIMIT ? OFFSET ?"
    args += [limit, offset]
    return get_conn().execute(sql, args).fetchall()


def count_contacts(search="", only_consent=False):
    sql = "SELECT COUNT(*) AS n FROM contacts WHERE 1=1"
    args = []
    if search:
        sql += " AND (phone LIKE ? OR name LIKE ?)"
        args += ["%" + search + "%", "%" + search + "%"]
    if only_consent:
        sql += " AND consent = 1"
    return get_conn().execute(sql, args).fetchone()["n"]


def list_audience(only_consent=False, limit=1000000):
    """Кому реально можно писать: контакты минус стоп-лист."""
    sql = "SELECT * FROM contacts WHERE phone NOT IN (SELECT phone FROM optout)"
    if only_consent:
        sql += " AND consent = 1"
    sql += " ORDER BY id LIMIT ?"
    return get_conn().execute(sql, (limit,)).fetchall()


def count_audience(only_consent=False):
    sql = ("SELECT COUNT(*) AS n FROM contacts "
           "WHERE phone NOT IN (SELECT phone FROM optout)")
    if only_consent:
        sql += " AND consent = 1"
    return get_conn().execute(sql).fetchone()["n"]


def delete_contact(contact_id):
    with tx() as conn:
        conn.execute("DELETE FROM contacts WHERE id = ?", (contact_id,))


def delete_all_contacts():
    """Очищает базу контактов и возвращает число удалённых.

    Стоп-лист и история кампаний намеренно не трогаются: отписавшиеся должны
    пережить любую перезагрузку базы, а отчёты по отправленным сообщениям
    хранят телефон и текст отдельно от контакта.
    """
    with tx() as conn:
        removed = conn.execute("SELECT COUNT(*) AS n FROM contacts").fetchone()["n"]
        conn.execute("DELETE FROM contacts")
        return removed


def contact_fields(row):
    """Плоский словарь полей контакта для подстановки в шаблон."""
    data = json.loads(row["fields_json"] or "{}")
    data.setdefault("имя", row["name"])
    data.setdefault("name", row["name"])
    data.setdefault("телефон", row["phone"])
    data.setdefault("phone", row["phone"])
    return data


def known_field_names(limit=200):
    names = set()
    rows = get_conn().execute(
        "SELECT fields_json FROM contacts ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    for row in rows:
        try:
            names.update(json.loads(row["fields_json"] or "{}").keys())
        except ValueError:
            continue
    names.update(["имя", "телефон"])
    return sorted(names)


# --- стоп-лист ---------------------------------------------------------------

def add_optout(phone, reason=""):
    with tx() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO optout(phone, reason, created_at) VALUES (?, ?, ?)",
            (phone, reason, now()),
        )


def remove_optout(phone):
    with tx() as conn:
        conn.execute("DELETE FROM optout WHERE phone = ?", (phone,))


def list_optout():
    return get_conn().execute("SELECT * FROM optout ORDER BY created_at DESC").fetchall()


def is_optout(phone):
    return get_conn().execute(
        "SELECT 1 FROM optout WHERE phone = ?", (phone,)
    ).fetchone() is not None


# --- кампании ----------------------------------------------------------------

def create_campaign(name, template, delay_sec, jitter_sec, hourly_limit, sim_number):
    with tx() as conn:
        cur = conn.execute(
            "INSERT INTO campaigns(name, template, status, delay_sec, jitter_sec, "
            "hourly_limit, sim_number, created_at) VALUES (?, ?, 'draft', ?, ?, ?, ?, ?)",
            (name, template, delay_sec, jitter_sec, hourly_limit, sim_number, now()),
        )
        return cur.lastrowid


def queue_messages(campaign_id, rows):
    """rows: список (contact_id, phone, text)."""
    with tx() as conn:
        conn.executemany(
            "INSERT INTO messages(campaign_id, contact_id, phone, text, status, created_at) "
            "VALUES (?, ?, ?, ?, 'queued', ?)",
            [(campaign_id, cid, phone, text, now()) for cid, phone, text in rows],
        )


def get_campaign(campaign_id):
    return get_conn().execute(
        "SELECT * FROM campaigns WHERE id = ?", (campaign_id,)
    ).fetchone()


def list_campaigns():
    return get_conn().execute(
        "SELECT c.*, "
        "(SELECT COUNT(*) FROM messages m WHERE m.campaign_id = c.id) AS total, "
        "(SELECT COUNT(*) FROM messages m WHERE m.campaign_id = c.id "
        "  AND m.status IN ('sent','delivered')) AS sent, "
        "(SELECT COUNT(*) FROM messages m WHERE m.campaign_id = c.id "
        "  AND m.status = 'failed') AS failed "
        "FROM campaigns c ORDER BY c.id DESC"
    ).fetchall()


def set_campaign_status(campaign_id, status):
    field = None
    if status == "running":
        field = "started_at"
    elif status in ("done", "stopped"):
        field = "finished_at"
    with tx() as conn:
        if field == "started_at":
            conn.execute(
                "UPDATE campaigns SET status = ?, started_at = COALESCE(started_at, ?) "
                "WHERE id = ?",
                (status, now(), campaign_id),
            )
        elif field:
            conn.execute(
                "UPDATE campaigns SET status = ?, finished_at = ? WHERE id = ?",
                (status, now(), campaign_id),
            )
        else:
            conn.execute("UPDATE campaigns SET status = ? WHERE id = ?", (status, campaign_id))


def campaign_stats(campaign_id):
    rows = get_conn().execute(
        "SELECT status, COUNT(*) AS n FROM messages WHERE campaign_id = ? GROUP BY status",
        (campaign_id,),
    ).fetchall()
    stats = {r["status"]: r["n"] for r in rows}
    stats["total"] = sum(stats.values())
    return stats


def next_queued(campaign_id):
    return get_conn().execute(
        "SELECT * FROM messages WHERE campaign_id = ? AND status = 'queued' "
        "ORDER BY id LIMIT 1",
        (campaign_id,),
    ).fetchone()


def sent_last_hour(campaign_id):
    return get_conn().execute(
        "SELECT COUNT(*) AS n FROM messages WHERE campaign_id = ? "
        "AND sent_at IS NOT NULL AND sent_at >= datetime('now', 'localtime', '-1 hour')",
        (campaign_id,),
    ).fetchone()["n"]


def mark_message(message_id, status, gateway_id=None, error=None, bump_attempt=False):
    with tx() as conn:
        conn.execute(
            "UPDATE messages SET status = ?, "
            "gateway_id = COALESCE(?, gateway_id), "
            "error = ?, "
            "attempts = attempts + ?, "
            "sent_at = CASE WHEN ? IN ('sent','delivered') AND sent_at IS NULL "
            "               THEN ? ELSE sent_at END, "
            "updated_at = ? WHERE id = ?",
            (status, gateway_id, error, 1 if bump_attempt else 0,
             status, now(), now(), message_id),
        )


def list_messages(campaign_id, status=None, limit=500):
    sql = "SELECT * FROM messages WHERE campaign_id = ?"
    args = [campaign_id]
    if status:
        sql += " AND status = ?"
        args.append(status)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    return get_conn().execute(sql, args).fetchall()


def pending_status_checks(limit=100):
    """Сообщения, ушедшие в шлюз, но ещё без финального статуса доставки."""
    return get_conn().execute(
        "SELECT * FROM messages WHERE status = 'sent' AND gateway_id IS NOT NULL "
        "AND created_at >= datetime('now', 'localtime', '-1 day') "
        "ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()


def running_campaign_id():
    row = get_conn().execute(
        "SELECT id FROM campaigns WHERE status = 'running' ORDER BY id LIMIT 1"
    ).fetchone()
    return row["id"] if row else None


def dashboard_totals():
    conn = get_conn()
    return {
        "contacts": conn.execute("SELECT COUNT(*) AS n FROM contacts").fetchone()["n"],
        "optout": conn.execute("SELECT COUNT(*) AS n FROM optout").fetchone()["n"],
        "campaigns": conn.execute("SELECT COUNT(*) AS n FROM campaigns").fetchone()["n"],
        "delivered": conn.execute(
            "SELECT COUNT(*) AS n FROM messages WHERE status = 'delivered'"
        ).fetchone()["n"],
    }
