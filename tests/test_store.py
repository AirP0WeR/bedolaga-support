"""Тесты состояния: что переживает перезапуск, а что нет."""

from __future__ import annotations

from app.store import Store


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
    assert store.counters() == {'answer': 1, 'escalate': 2}
