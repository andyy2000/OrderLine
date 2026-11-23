"""SQLite storage for orders and per-restaurant settings.

One file shared by the voice bot (writes orders) and the kitchen server
(reads them, updates status). SQLite in WAL mode handles that fine at the
volume of a few restaurants; move to Postgres when there are many.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

STATUSES = ["confirmed", "printed", "ready", "picked_up", "cancelled"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    restaurant_id TEXT NOT NULL,
    order_number INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'confirmed',
    customer_name TEXT NOT NULL,
    phone TEXT NOT NULL,
    lines_json TEXT NOT NULL,
    subtotal_cents INTEGER NOT NULL,
    tax_cents INTEGER NOT NULL,
    total_cents INTEGER NOT NULL,
    pickup_at TEXT NOT NULL,
    call_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS orders_restaurant ON orders (restaurant_id, created_at);
CREATE TABLE IF NOT EXISTS settings (
    restaurant_id TEXT PRIMARY KEY,
    ai_enabled INTEGER NOT NULL DEFAULT 1,
    pickup_minutes INTEGER,
    sold_out_json TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS calls (
    call_id TEXT PRIMARY KEY,
    restaurant_id TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    outcome TEXT,
    caller_phone TEXT,
    transfer_reason TEXT,
    cart_json TEXT,
    transcript_json TEXT,
    tool_log_json TEXT,
    metrics_json TEXT,
    flagged INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS calls_restaurant ON calls (restaurant_id, started_at);
CREATE TABLE IF NOT EXISTS questions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    restaurant_id TEXT NOT NULL,
    call_id TEXT,
    question TEXT NOT NULL,
    answer TEXT,
    asked_at TEXT NOT NULL,
    answered_at TEXT,
    expires_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS questions_open ON questions (restaurant_id, answered_at, expires_at);
CREATE INDEX IF NOT EXISTS orders_phone ON orders (restaurant_id, phone, id);
"""


def _migrate(conn: sqlite3.Connection) -> None:
    """Add columns introduced after the first release to databases created earlier."""
    have = {row[1] for row in conn.execute("PRAGMA table_info(calls)")}
    for column, decl in [("ended_at", "TEXT"), ("caller_phone", "TEXT"), ("transfer_reason", "TEXT"),
                         ("cart_json", "TEXT"), ("metrics_json", "TEXT"),
                         ("flagged", "INTEGER NOT NULL DEFAULT 0")]:
        if column not in have:
            conn.execute(f"ALTER TABLE calls ADD COLUMN {column} {decl}")
    have = {row[1] for row in conn.execute("PRAGMA table_info(settings)")}
    if "blocked_json" not in have:
        conn.execute("ALTER TABLE settings ADD COLUMN blocked_json TEXT NOT NULL DEFAULT '[]'")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class OrderStore:
    def __init__(self, path: str | Path = "orderline.db"):
        self.path = str(path)
        self._lock = threading.Lock()
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        if self.path != ":memory:":
            self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)
        with self._conn:
            _migrate(self._conn)

    # ---- orders -------------------------------------------------------
    def save_order(self, order: dict) -> int:
        """Insert a confirmed order; returns the restaurant's daily order number."""
        with self._lock, self._conn:
            # Numbers restart each day in the restaurant's local time. pickup_at is
            # an ISO time in local time, so its first 10 characters are the local date.
            local_day = order["pickup_at"][:10]
            row = self._conn.execute(
                "SELECT COALESCE(MAX(order_number), 0) FROM orders "
                "WHERE restaurant_id = ? AND substr(pickup_at, 1, 10) = ?",
                (order["restaurant_id"], local_day),
            ).fetchone()
            number = row[0] + 1
            stamp = _now()
            self._conn.execute(
                "INSERT INTO orders (restaurant_id, order_number, customer_name, phone, lines_json, "
                "subtotal_cents, tax_cents, total_cents, pickup_at, call_id, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (order["restaurant_id"], number, order["customer_name"], order["phone"],
                 json.dumps(order["lines"], ensure_ascii=False), order["subtotal_cents"],
                 order["tax_cents"], order["total_cents"], order["pickup_at"], order.get("call_id"),
                 stamp, stamp),
            )
            return number

    def list_orders(self, restaurant_id: str, statuses: list[str] | None = None,
                    limit: int = 100) -> list[dict]:
        sql = "SELECT * FROM orders WHERE restaurant_id = ?"
        params: list = [restaurant_id]
        if statuses:
            sql += f" AND status IN ({','.join('?' * len(statuses))})"
            params += statuses
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [self._row(r) for r in rows]

    def get_order(self, order_id: int) -> dict | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
        return self._row(row) if row else None

    def set_status(self, order_id: int, status: str) -> dict | None:
        if status not in STATUSES:
            raise ValueError(f"status must be one of {STATUSES}")
        with self._lock, self._conn:
            self._conn.execute("UPDATE orders SET status = ?, updated_at = ? WHERE id = ?",
                               (status, _now(), order_id))
        return self.get_order(order_id)

    def last_change(self, restaurant_id: str) -> str:
        """A value that changes whenever any order for the restaurant changes."""
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*), COALESCE(MAX(updated_at), ''), COALESCE(SUM(CASE status WHEN 'confirmed' THEN 1 "
                "WHEN 'printed' THEN 3 WHEN 'ready' THEN 7 WHEN 'picked_up' THEN 15 ELSE 31 END), 0) "
                "FROM orders WHERE restaurant_id = ?",
                (restaurant_id,),
            ).fetchone()
        return f"{row[0]}|{row[1]}|{row[2]}"

    # ---- settings -----------------------------------------------------
    def get_settings(self, restaurant_id: str) -> dict:
        with self._lock:
            row = self._conn.execute("SELECT * FROM settings WHERE restaurant_id = ?",
                                     (restaurant_id,)).fetchone()
        if row is None:
            return {"restaurant_id": restaurant_id, "ai_enabled": True, "pickup_minutes": None,
                    "sold_out": [], "blocked_numbers": []}
        return {"restaurant_id": restaurant_id, "ai_enabled": bool(row["ai_enabled"]),
                "pickup_minutes": row["pickup_minutes"], "sold_out": json.loads(row["sold_out_json"]),
                "blocked_numbers": json.loads(row["blocked_json"] or "[]")}

    def update_settings(self, restaurant_id: str, **changes) -> dict:
        current = self.get_settings(restaurant_id)
        current.update({k: v for k, v in changes.items() if k in current and k != "restaurant_id"})
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO settings (restaurant_id, ai_enabled, pickup_minutes, sold_out_json, blocked_json) "
                "VALUES (?,?,?,?,?) ON CONFLICT(restaurant_id) DO UPDATE SET "
                "ai_enabled = excluded.ai_enabled, pickup_minutes = excluded.pickup_minutes, "
                "sold_out_json = excluded.sold_out_json, blocked_json = excluded.blocked_json",
                (restaurant_id, int(bool(current["ai_enabled"])), current["pickup_minutes"],
                 json.dumps(sorted(set(current["sold_out"]))),
                 json.dumps(sorted({str(n) for n in current["blocked_numbers"] if n}))),
            )
        return self.get_settings(restaurant_id)

    def block_number(self, restaurant_id: str, phone: str, blocked: bool = True) -> dict:
        """Prank callers: a blocked number gets a short 'call the restaurant' message and a hang-up."""
        numbers = set(self.get_settings(restaurant_id)["blocked_numbers"])
        (numbers.add if blocked else numbers.discard)(phone)
        return self.update_settings(restaurant_id, blocked_numbers=sorted(numbers))

    def last_order_for_phone(self, restaurant_id: str, phone: str | None) -> dict | None:
        """The caller's most recent completed order, for 'the usual, please'."""
        if not phone:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM orders WHERE restaurant_id = ? AND phone = ? AND status != 'cancelled' "
                "ORDER BY id DESC LIMIT 1", (restaurant_id, phone),
            ).fetchone()
        return self._row(row) if row else None

    # ---- calls --------------------------------------------------------
    def start_call(self, call_id: str, restaurant_id: str, caller_phone: str | None = None) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR IGNORE INTO calls (call_id, restaurant_id, started_at, caller_phone) VALUES (?,?,?,?)",
                (call_id, restaurant_id, _now(), caller_phone),
            )

    def log_call(self, call_id: str, restaurant_id: str, outcome: str, transcript: list | None = None,
                 tool_log: list | None = None, cart: dict | None = None, transfer_reason: str | None = None,
                 caller_phone: str | None = None, metrics: dict | None = None) -> None:
        """Record how a call went. Safe to call more than once (e.g. at transfer, then at hangup)."""
        self.start_call(call_id, restaurant_id, caller_phone)
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE calls SET ended_at = ?, outcome = ?, transfer_reason = COALESCE(?, transfer_reason), "
                "cart_json = COALESCE(?, cart_json), transcript_json = COALESCE(?, transcript_json), "
                "tool_log_json = COALESCE(?, tool_log_json), metrics_json = COALESCE(?, metrics_json), "
                "caller_phone = COALESCE(?, caller_phone) WHERE call_id = ?",
                (_now(), outcome, transfer_reason,
                 json.dumps(cart, ensure_ascii=False, default=str) if cart is not None else None,
                 json.dumps(transcript, ensure_ascii=False, default=str) if transcript is not None else None,
                 json.dumps(tool_log, ensure_ascii=False, default=str) if tool_log is not None else None,
                 json.dumps(metrics, ensure_ascii=False, default=str) if metrics is not None else None,
                 caller_phone, call_id),
            )

    def list_calls(self, restaurant_id: str, limit: int = 50, since: str | None = None,
                   outcome: str | None = None) -> list[dict]:
        sql = "SELECT * FROM calls WHERE restaurant_id = ?"
        params: list = [restaurant_id]
        if since:
            sql += " AND started_at >= ?"
            params.append(since)
        if outcome:
            sql += " AND outcome = ?"
            params.append(outcome)
        sql += " ORDER BY started_at DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [self._call_row(r) for r in rows]

    def get_call(self, call_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM calls WHERE call_id = ?", (call_id,)).fetchone()
        return self._call_row(row) if row else None

    def flag_call(self, call_id: str, flagged: bool) -> None:
        with self._lock, self._conn:
            self._conn.execute("UPDATE calls SET flagged = ? WHERE call_id = ?", (int(flagged), call_id))

    def recent_transfers(self, restaurant_id: str, minutes: int = 15) -> list[dict]:
        """Calls handed to a person in the last few minutes, with what was already ordered."""
        since = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat(timespec="milliseconds")
        return [c for c in self.list_calls(restaurant_id, limit=10, since=since, outcome="transferred")]

    def stats(self, restaurant_id: str, days: int = 7) -> dict:
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="milliseconds")
        with self._lock:
            calls = self._conn.execute(
                "SELECT outcome, COUNT(*) FROM calls WHERE restaurant_id = ? AND started_at >= ? GROUP BY outcome",
                (restaurant_id, since)).fetchall()
            orders = self._conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(total_cents), 0), COALESCE(AVG(total_cents), 0) FROM orders "
                "WHERE restaurant_id = ? AND created_at >= ? AND status != 'cancelled'",
                (restaurant_id, since)).fetchone()
            durations = self._conn.execute(
                "SELECT started_at, ended_at FROM calls WHERE restaurant_id = ? AND started_at >= ? "
                "AND ended_at IS NOT NULL", (restaurant_id, since)).fetchall()
        by_outcome = {row[0] or "unknown": row[1] for row in calls}
        total_calls = sum(by_outcome.values())
        secs = []
        for started, ended in durations:
            try:
                secs.append((datetime.fromisoformat(ended) - datetime.fromisoformat(started)).total_seconds())
            except ValueError:
                pass
        return {
            "days": days,
            "calls": total_calls,
            "calls_by_outcome": by_outcome,
            "orders": orders[0],
            "revenue_cents": int(orders[1]),
            "average_order_cents": int(orders[2]),
            "transfer_rate": round(by_outcome.get("transferred", 0) / total_calls, 3) if total_calls else 0.0,
            "completion_rate": round(by_outcome.get("order", 0) / total_calls, 3) if total_calls else 0.0,
            "average_call_seconds": round(sum(secs) / len(secs)) if secs else None,
        }

    # ---- mid-call questions to the kitchen ----------------------------
    def ask_question(self, restaurant_id: str, call_id: str | None, question: str, timeout_s: int = 30) -> int:
        now = datetime.now(timezone.utc)
        with self._lock, self._conn:
            cur = self._conn.execute(
                "INSERT INTO questions (restaurant_id, call_id, question, asked_at, expires_at) VALUES (?,?,?,?,?)",
                (restaurant_id, call_id, question[:200], now.isoformat(timespec="milliseconds"),
                 (now + timedelta(seconds=timeout_s)).isoformat(timespec="milliseconds")),
            )
            return cur.lastrowid

    def answer_question(self, question_id: int, answer: str) -> bool:
        with self._lock, self._conn:
            cur = self._conn.execute(
                "UPDATE questions SET answer = ?, answered_at = ? WHERE id = ? AND answered_at IS NULL",
                (answer.strip()[:300], _now(), question_id))
            return cur.rowcount == 1

    def get_answer(self, question_id: int) -> str | None:
        with self._lock:
            row = self._conn.execute("SELECT answer FROM questions WHERE id = ?", (question_id,)).fetchone()
        return row["answer"] if row and row["answer"] else None

    def open_questions(self, restaurant_id: str) -> list[dict]:
        """Unanswered questions the kitchen can still answer in time."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, call_id, question, asked_at, expires_at FROM questions WHERE restaurant_id = ? "
                "AND answered_at IS NULL AND expires_at > ? ORDER BY id", (restaurant_id, _now())).fetchall()
        return [dict(r) for r in rows]

    @staticmethod
    def _call_row(row: sqlite3.Row) -> dict:
        d = dict(row)
        for key in ("cart", "transcript", "tool_log", "metrics"):
            raw = d.pop(f"{key}_json")
            d[key] = json.loads(raw) if raw else None
        d["flagged"] = bool(d["flagged"])
        return d

    @staticmethod
    def _row(row: sqlite3.Row) -> dict:
        d = dict(row)
        d["lines"] = json.loads(d.pop("lines_json"))
        return d
