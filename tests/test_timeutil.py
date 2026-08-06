"""Разбор дат и то, ради чего он существует: дебаунс на naive-датах."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app import gate, timeutil
from app.gate import TicketState

MSK = 'Europe/Moscow'  # UTC+3 круглый год


@pytest.fixture(autouse=True)
def _reset_tz():
    """Таймзона глобальная — возвращаем UTC, чтобы тесты не влияли друг на друга."""
    timeutil.set_timezone('UTC')
    yield
    timeutil.set_timezone('UTC')


def test_дата_с_таймзоной_разбирается_как_есть():
    assert timeutil.parse_dt('2026-08-06T10:00:00+03:00').utcoffset() == timedelta(hours=3)


def test_z_на_конце_считается_utc():
    assert timeutil.parse_dt('2026-08-06T10:00:00Z') == datetime(2026, 8, 6, 10, tzinfo=UTC)


def test_naive_дата_по_умолчанию_utc():
    assert timeutil.parse_dt('2026-08-06T10:00:00') == datetime(2026, 8, 6, 10, tzinfo=UTC)


def test_naive_дата_читается_в_заданной_зоне():
    timeutil.set_timezone(MSK)
    assert timeutil.parse_dt('2026-08-06T10:00:00') == datetime(2026, 8, 6, 7, tzinfo=UTC)


def test_неизвестная_зона_не_роняет_сервис():
    timeutil.set_timezone('Мордор/Барад-Дур')
    assert timeutil.parse_dt('2026-08-06T10:00:00') == datetime(2026, 8, 6, 10, tzinfo=UTC)


def test_мусор_и_пустое_дают_none():
    assert timeutil.parse_dt('позавчера') is None
    assert timeutil.parse_dt('') is None
    assert timeutil.parse_dt(None) is None


def test_предупреждение_о_naive_дате_пишется_один_раз(caplog):
    with caplog.at_level('WARNING', logger='app.timeutil'):
        timeutil.parse_dt('2026-08-06T10:00:00')
        timeutil.parse_dt('2026-08-06T11:00:00')
    assert len([r for r in caplog.records if 'без таймзоны' in r.message]) == 1


def _ticket(created_at: str) -> dict:
    return {
        'id': 1,
        'user_id': 42,
        'status': 'open',
        'messages': [{'id': 1, 'user_id': 42, 'is_from_admin': False, 'created_at': created_at}],
    }


def test_naive_дата_в_своей_зоне_даёт_верный_дебаунс():
    """Сообщение написано 10 секунд назад по московским часам — ждём.

    С трактовкой той же строки как UTC сообщение выглядело бы трёхчасовым, и
    дебаунс не сработал бы вовсе.
    """
    timeutil.set_timezone(MSK)
    now = datetime(2026, 8, 6, 7, 0, 10, tzinfo=UTC)  # 10:00:10 по Москве

    verdict = gate.decide(_ticket('2026-08-06T10:00:00'), TicketState(), now=now, debounce_sec=45)

    assert verdict == gate.DEFER


def test_отлежавшаяся_naive_дата_в_своей_зоне_идёт_в_модель():
    timeutil.set_timezone(MSK)
    now = datetime(2026, 8, 6, 7, 1, 0, tzinfo=UTC)  # 10:01:00 по Москве

    verdict = gate.decide(_ticket('2026-08-06T10:00:00'), TicketState(), now=now, debounce_sec=45)

    assert verdict == gate.ASK_LLM
