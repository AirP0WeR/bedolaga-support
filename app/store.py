"""Состояние и аудит в SQLite.

Состояние — вспомогательное, а не единственный источник правды: факт эскалации
дублируется приоритетом самого тикета, а факт участия человека — полем `user_id`
в сообщении. Потеря базы приводит к молчанию, но не к вредным действиям.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from .gate import TicketState

SCHEMA = """
CREATE TABLE IF NOT EXISTS tickets (
    ticket_id               INTEGER PRIMARY KEY,
    human_seen              INTEGER NOT NULL DEFAULT 0,
    escalated               INTEGER NOT NULL DEFAULT 0,
    our_message_ids         TEXT    NOT NULL DEFAULT '[]',
    last_decided_message_id INTEGER,
    reply_intent_at         TEXT,
    updated_at              TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS audit (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT    NOT NULL,
    ticket_id   INTEGER NOT NULL,
    action      TEXT    NOT NULL,
    question    TEXT,
    reply       TEXT,
    reason      TEXT,
    confidence  REAL,
    topic       TEXT,
    model       TEXT,
    shadow      INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS audit_ts ON audit (ts);
CREATE INDEX IF NOT EXISTS audit_ticket ON audit (ticket_id);
"""


def _now() -> str:
    return datetime.now(UTC).isoformat()


class Store:
    def __init__(self, path: str):
        if path != ':memory:':
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute('PRAGMA journal_mode=WAL')
        self._db.executescript(SCHEMA)

    def close(self) -> None:
        self._db.close()

    # --- состояние тикета -----------------------------------------------

    def _row(self, ticket_id: int) -> sqlite3.Row | None:
        return self._db.execute('SELECT * FROM tickets WHERE ticket_id = ?', (ticket_id,)).fetchone()

    def _ensure(self, ticket_id: int) -> None:
        self._db.execute(
            'INSERT OR IGNORE INTO tickets (ticket_id, updated_at) VALUES (?, ?)',
            (ticket_id, _now()),
        )

    def state(self, ticket_id: int) -> TicketState:
        row = self._row(ticket_id)
        if row is None:
            return TicketState()
        return TicketState(
            our_message_ids=json.loads(row['our_message_ids']),
            escalated=bool(row['escalated']),
            human_seen=bool(row['human_seen']),
        )

    def last_decided_message_id(self, ticket_id: int) -> int | None:
        row = self._row(ticket_id)
        return row['last_decided_message_id'] if row else None

    def set_last_decided(self, ticket_id: int, message_id: int) -> None:
        self._ensure(ticket_id)
        self._db.execute(
            'UPDATE tickets SET last_decided_message_id = ?, updated_at = ? WHERE ticket_id = ?',
            (message_id, _now(), ticket_id),
        )

    def mark_human(self, ticket_id: int) -> None:
        self._ensure(ticket_id)
        self._db.execute(
            'UPDATE tickets SET human_seen = 1, updated_at = ? WHERE ticket_id = ?',
            (_now(), ticket_id),
        )

    def mark_escalated(self, ticket_id: int) -> None:
        self._ensure(ticket_id)
        self._db.execute(
            'UPDATE tickets SET escalated = 1, updated_at = ? WHERE ticket_id = ?',
            (_now(), ticket_id),
        )

    # --- намерение ответить ---------------------------------------------

    def begin_reply(self, ticket_id: int) -> None:
        """Записать намерение до отправки.

        Если процесс умрёт между этим вызовом и ответом, на следующем цикле
        будет видно, что попытка была, — и тикет не получит второй ответ.
        """
        self._ensure(ticket_id)
        self._db.execute(
            'UPDATE tickets SET reply_intent_at = ?, updated_at = ? WHERE ticket_id = ?',
            (_now(), _now(), ticket_id),
        )

    def pending_reply(self, ticket_id: int) -> bool:
        row = self._row(ticket_id)
        return bool(row and row['reply_intent_at'])

    def finish_reply(self, ticket_id: int, message_id: int) -> None:
        self._ensure(ticket_id)
        row = self._row(ticket_id)
        ids = json.loads(row['our_message_ids']) if row else []
        if message_id not in ids:
            ids.append(message_id)
        self._db.execute(
            'UPDATE tickets SET our_message_ids = ?, reply_intent_at = NULL, updated_at = ? WHERE ticket_id = ?',
            (json.dumps(ids), _now(), ticket_id),
        )

    def clear_reply_intent(self, ticket_id: int) -> None:
        self._db.execute(
            'UPDATE tickets SET reply_intent_at = NULL, updated_at = ? WHERE ticket_id = ?',
            (_now(), ticket_id),
        )

    # --- аудит ------------------------------------------------------------

    def audit(
        self,
        ticket_id: int,
        action: str,
        *,
        question: str = '',
        reply: str = '',
        reason: str = '',
        confidence: float | None = None,
        topic: str = '',
        model: str = '',
        shadow: bool = False,
    ) -> None:
        self._db.execute(
            'INSERT INTO audit (ts, ticket_id, action, question, reply, reason, confidence, topic, model, shadow) '
            'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (_now(), ticket_id, action, question, reply, reason, confidence, topic, model, int(shadow)),
        )

    def counters(self) -> dict[str, int]:
        rows = self._db.execute('SELECT action, COUNT(*) AS n FROM audit GROUP BY action').fetchall()
        return {row['action']: row['n'] for row in rows}
