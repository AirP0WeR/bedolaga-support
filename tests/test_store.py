"""Тесты состояния: что переживает перезапуск, а что нет."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from app.llm import Usage
from app.store import Store

LONG_AGO = datetime(2000, 1, 1, tzinfo=UTC)


def test_пустое_состояние_безопасно():
    store = Store(':memory:')
    state = store.state(1)
    assert state.our_message_ids == []
    assert state.escalated is False
    assert state.human_seen is False


def test_состояние_переживает_переоткрытие(tmp_path):
    path = str(tmp_path / 'state.db')
    store = Store(path)
    store.finish_reply(5, 100)
    store.mark_escalated(5)
    store.close()

    reopened = Store(path)
    state = reopened.state(5)
    assert state.our_message_ids == [100]
    assert state.escalated is True


def test_наши_ответы_копятся_без_дублей():
    store = Store(':memory:')
    store.finish_reply(1, 10)
    store.finish_reply(1, 11)
    store.finish_reply(1, 11)
    assert store.state(1).our_message_ids == [10, 11]


def test_намерение_ответить_видно_до_подтверждения():
    store = Store(':memory:')
    assert store.pending_reply(1) is False
    store.begin_reply(1)
    assert store.pending_reply(1) is True
    store.finish_reply(1, 42)
    assert store.pending_reply(1) is False
    assert store.state(1).our_message_ids == [42]


def test_намерение_можно_снять_без_ответа():
    store = Store(':memory:')
    store.begin_reply(1)
    store.clear_reply_intent(1)
    assert store.pending_reply(1) is False
    assert store.state(1).our_message_ids == []


def test_отметка_человека_сохраняется():
    store = Store(':memory:')
    store.mark_human(7)
    assert store.state(7).human_seen is True


def test_последнее_решённое_сообщение_запоминается():
    store = Store(':memory:')
    assert store.last_decided_message_id(3) is None
    store.set_last_decided(3, 99)
    assert store.last_decided_message_id(3) == 99


def test_аудит_считает_действия():
    store = Store(':memory:')
    store.audit(1, 'answer', question='как подключить', reply='вот так', confidence=0.9)
    store.audit(2, 'escalate', reason='деньги')
    store.audit(3, 'escalate', reason='нет ответа в базе')

    digest = store.counters(LONG_AGO)

    assert (digest.answered, digest.escalated) == (1, 2)


def test_черновики_считаются_отдельно_от_ответов():
    """В теневом режиме важно не спутать «ответили» с «ответили бы»."""
    store = Store(':memory:')
    store.audit(1, 'answer', reply='вот так', confidence=0.9, shadow=True)
    store.audit(2, 'answer', reply='и так', confidence=0.8)

    digest = store.counters(LONG_AGO)

    assert (digest.answered, digest.answered_shadow) == (1, 1)


def test_сводка_считает_только_свой_период():
    store = Store(':memory:')
    store.audit(1, 'answer', confidence=0.9)

    assert store.counters(datetime.now(UTC) + timedelta(seconds=1)).answered == 0
    assert store.counters(LONG_AGO).answered == 1


def test_средняя_уверенность_по_ответам():
    store = Store(':memory:')
    store.audit(1, 'answer', confidence=1.0)
    store.audit(2, 'answer', confidence=0.5)
    store.audit(3, 'escalate', confidence=0.1)  # эскалации в среднее не входят

    assert store.counters(LONG_AGO).avg_confidence == pytest.approx(0.75)


def test_средняя_уверенность_без_ответов_пуста():
    assert Store(':memory:').counters(LONG_AGO).avg_confidence is None


def test_темы_в_порядке_убывания_и_не_больше_пяти():
    store = Store(':memory:')
    for topic in ['оплата'] * 3 + ['подключение'] * 2 + ['устройства', 'скорость', 'возврат', 'бан']:
        store.audit(1, 'answer', topic=topic)
    store.audit(1, 'answer')  # без темы — в сводку не попадает

    topics = store.counters(LONG_AGO).topics

    assert len(topics) == 5
    assert topics[0] == ('оплата', 3)
    assert topics[1] == ('подключение', 2)
    assert all(topic for topic, _ in topics)


def test_ошибки_модели_видны_в_сводке():
    store = Store(':memory:')
    store.audit(1, 'llm_error', reason='модель недоступна')

    assert store.counters(LONG_AGO).llm_errors == 1


def test_токены_копятся_в_сводке():
    store = Store(':memory:')
    store.audit(1, 'answer', usage=Usage(prompt_tokens=1000, completion_tokens=120, cached_tokens=900))
    store.audit(2, 'escalate', usage=Usage(prompt_tokens=1000, completion_tokens=30, cached_tokens=980))

    digest = store.counters(LONG_AGO)

    assert (digest.prompt_tokens, digest.completion_tokens, digest.cached_tokens) == (2000, 150, 1880)


def test_аудит_без_расхода_токенов_не_ломается():
    store = Store(':memory:')
    store.audit(1, 'handover', reason='человек в тикете')
    assert store.counters(LONG_AGO).prompt_tokens == 0


def test_старая_база_дополняется_колонками(tmp_path):
    """У людей на серверах база от прошлой версии — она должна открыться."""
    path = str(tmp_path / 'state.db')
    old = sqlite3.connect(path)
    old.executescript(
        'CREATE TABLE audit (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, ticket_id INTEGER NOT NULL, '
        'action TEXT NOT NULL, question TEXT, reply TEXT, reason TEXT, confidence REAL, topic TEXT, model TEXT, '
        "shadow INTEGER NOT NULL DEFAULT 0); INSERT INTO audit (ts, ticket_id, action) VALUES ('2000-01-02', 1, "
        "'answer');"
    )
    old.commit()
    old.close()

    store = Store(path)
    store.audit(2, 'answer', usage=Usage(prompt_tokens=10, completion_tokens=5))

    digest = store.counters(LONG_AGO)
    assert digest.answered == 2  # старая запись никуда не делась
    assert digest.prompt_tokens == 10


def _audit_at(store: Store, ts: str, action: str = 'answer') -> None:
    store._db.execute('INSERT INTO audit (ts, ticket_id, action) VALUES (?, 1, ?)', (ts, action))


def test_чистка_убирает_старое_и_бережёт_свежее(tmp_path):
    store = Store(str(tmp_path / 'state.db'))
    _audit_at(store, '2026-01-01T00:00:00+00:00')
    _audit_at(store, '2026-08-01T00:00:00+00:00')

    deleted = store.purge_audit(datetime(2026, 6, 1, tzinfo=UTC))

    assert deleted == 1
    assert store.counters(LONG_AGO).answered == 1


def test_чистка_на_пустой_базе_ничего_не_делает():
    assert Store(':memory:').purge_audit(datetime(2026, 6, 1, tzinfo=UTC)) == 0


def test_чистка_не_трогает_состояние_тикетов(tmp_path):
    """Аудит — история, а состояние решает, ответим ли мы второй раз."""
    store = Store(str(tmp_path / 'state.db'))
    store.finish_reply(5, 100)
    _audit_at(store, '2026-01-01T00:00:00+00:00')

    store.purge_audit(datetime(2026, 6, 1, tzinfo=UTC))

    assert store.state(5).our_message_ids == [100]
