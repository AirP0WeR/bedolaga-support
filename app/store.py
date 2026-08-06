"""Состояние и аудит в SQLite.

Состояние — вспомогательное, а не единственный источник правды: факт эскалации
дублируется приоритетом самого тикета, а факт участия человека — полем `user_id`
в сообщении. Потеря базы приводит к молчанию, но не к вредным действиям.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from .gate import TicketState
from .llm import Usage


@dataclass(frozen=True)
class Digest:
    """Что сервис сделал за период. Пустой — тоже осмысленный ответ."""

    since: datetime
    answered: int = 0
    answered_shadow: int = 0
    escalated: int = 0
    handover: int = 0
    llm_errors: int = 0
    avg_confidence: float | None = None
    topics: tuple[tuple[str, int], ...] = field(default_factory=tuple)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0


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

# Колонки, добавленные после первого релиза. База переживает пересоздание
# контейнера, поэтому у людей на серверах она старая — досыпаем на открытии.
MIGRATIONS = (
    ('audit', 'prompt_tokens', 'INTEGER NOT NULL DEFAULT 0'),
    ('audit', 'completion_tokens', 'INTEGER NOT NULL DEFAULT 0'),
    ('audit', 'cached_tokens', 'INTEGER NOT NULL DEFAULT 0'),
)


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
        self._migrate()

    def _migrate(self) -> None:
        for table, column, definition in MIGRATIONS:
            existing = {row['name'] for row in self._db.execute(f'PRAGMA table_info({table})')}
            if column not in existing:
                self._db.execute(f'ALTER TABLE {table} ADD COLUMN {column} {definition}')

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
        usage: Usage | None = None,
    ) -> None:
        usage = usage or Usage()
        self._db.execute(
            'INSERT INTO audit (ts, ticket_id, action, question, reply, reason, confidence, topic, model, shadow, '
            'prompt_tokens, completion_tokens, cached_tokens) '
            'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (
                _now(),
                ticket_id,
                action,
                question,
                reply,
                reason,
                confidence,
                topic,
                model,
                int(shadow),
                usage.prompt_tokens,
                usage.completion_tokens,
                usage.cached_tokens,
            ),
        )

    def counters(self, since: datetime) -> Digest:
        """Сводка по аудиту за период — то, из чего складывается дайджест."""
        moment = since.astimezone(UTC).isoformat()

        counts: dict[tuple[str, int], int] = {}
        for row in self._db.execute(
            'SELECT action, shadow, COUNT(*) AS n FROM audit WHERE ts >= ? GROUP BY action, shadow',
            (moment,),
        ):
            counts[(row['action'], row['shadow'])] = row['n']

        confidence = self._db.execute(
            "SELECT AVG(confidence) AS avg FROM audit WHERE ts >= ? AND action = 'answer' AND confidence IS NOT NULL",
            (moment,),
        ).fetchone()['avg']

        tokens = self._db.execute(
            'SELECT COALESCE(SUM(prompt_tokens), 0) AS prompt, COALESCE(SUM(completion_tokens), 0) AS completion, '
            'COALESCE(SUM(cached_tokens), 0) AS cached FROM audit WHERE ts >= ?',
            (moment,),
        ).fetchone()

        topics = self._db.execute(
            "SELECT topic, COUNT(*) AS n FROM audit WHERE ts >= ? AND topic <> '' "
            'GROUP BY topic ORDER BY n DESC, topic LIMIT 5',
            (moment,),
        ).fetchall()

        return Digest(
            since=since,
            answered=counts.get(('answer', 0), 0),
            answered_shadow=counts.get(('answer', 1), 0),
            escalated=counts.get(('escalate', 0), 0),
            handover=counts.get(('handover', 0), 0),
            llm_errors=counts.get(('llm_error', 0), 0),
            avg_confidence=confidence,
            topics=tuple((row['topic'], row['n']) for row in topics),
            prompt_tokens=tokens['prompt'],
            completion_tokens=tokens['completion'],
            cached_tokens=tokens['cached'],
        )
