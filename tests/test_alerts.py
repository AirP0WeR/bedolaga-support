"""Алерты: зовём человека на повторяющихся сбоях, но не на каждом."""

from __future__ import annotations

import pytest

from app.alerts import LLM, Alerts


class FakeNotifier:
    def __init__(self):
        self.problems: list[str] = []
        self.recoveries: list[str] = []

    def problem(self, text: str) -> None:
        self.problems.append(text)

    def recovered(self, text: str) -> None:
        self.recoveries.append(text)


@pytest.fixture
def notifier() -> FakeNotifier:
    return FakeNotifier()


def alerts(notifier, *, after=3, cooldown=3600) -> Alerts:
    return Alerts(notifier, after_failures=after, cooldown_sec=cooldown)


def test_разовый_сбой_молчит(notifier):
    alerts(notifier).failure(LLM, 'таймаут', now=0.0)
    assert notifier.problems == []


def test_алерт_на_пороге(notifier):
    a = alerts(notifier)
    for i in range(3):
        a.failure(LLM, 'таймаут', now=float(i))
    assert len(notifier.problems) == 1
    assert 'обращение к модели' in notifier.problems[0]


def test_дедуп_в_пределах_кулдауна(notifier):
    a = alerts(notifier, after=1, cooldown=3600)
    a.failure(LLM, 'таймаут', now=0.0)
    a.failure(LLM, 'таймаут', now=3599.0)
    assert len(notifier.problems) == 1


def test_после_кулдауна_напоминаем(notifier):
    a = alerts(notifier, after=1, cooldown=3600)
    a.failure(LLM, 'таймаут', now=0.0)
    a.failure(LLM, 'таймаут', now=3601.0)
    assert len(notifier.problems) == 2


def test_удачный_цикл_обнуляет_счётчик(notifier):
    a = alerts(notifier)
    a.failure(LLM, 'таймаут', now=0.0)
    a.failure(LLM, 'таймаут', now=1.0)
    a.success(LLM, now=2.0)
    a.failure(LLM, 'таймаут', now=3.0)
    assert notifier.problems == []


def test_возврат_в_норму_сообщается_один_раз(notifier):
    a = alerts(notifier, after=1)
    a.failure(LLM, 'таймаут', now=0.0)
    a.success(LLM, now=1.0)
    a.success(LLM, now=2.0)
    assert len(notifier.recoveries) == 1
    assert 'снова работает' in notifier.recoveries[0]


def test_норма_без_предшествующего_алерта_молчит(notifier):
    a = alerts(notifier)
    a.failure(LLM, 'таймаут', now=0.0)
    a.success(LLM, now=1.0)
    assert notifier.recoveries == []


def test_виды_сбоев_считаются_раздельно(notifier):
    a = alerts(notifier, after=2)
    a.failure('cycle', 'API недоступен', now=0.0)
    a.failure(LLM, 'таймаут', now=1.0)
    assert notifier.problems == []
    a.failure(LLM, 'таймаут', now=2.0)
    assert len(notifier.problems) == 1
    assert 'обращение к модели' in notifier.problems[0]
